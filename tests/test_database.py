"""Tests for durable SQLite metadata and FTS5 behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database, DuplicateDocumentError, RecordNotFoundError
from src.migrations import MigrationError
from src.models import ImportStatus, PageStatus


def _create_document(database: Database, suffix: str = "a"):
    return database.create_document(
        title="液压系统手册",
        filename="hydraulics.pdf",
        source_path=Path("data/raw/hydraulics.pdf"),
        sha256=suffix * 64,
    )


def _create_v2_database(database_path: Path, page_count: int = 78) -> None:
    """Create a representative v0.0.2 database without opening app user data."""

    timestamp = "2026-07-12T00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            )"""
        )
        migrations_module._apply_version_one(connection)
        migrations_module._apply_version_two(connection)
        connection.execute(
            """
            INSERT INTO documents(
                id, title, filename, source_path, sha256, page_count,
                created_at, updated_at, import_status, processed_page_count,
                text_page_count, review_page_count, import_error, imported_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, 0, ?, '', ?)
            """,
            (
                "v0.0.2 工程手册",
                "legacy-v2.pdf",
                "data/raw/legacy-v2.pdf",
                "d" * 64,
                page_count,
                timestamp,
                timestamp,
                page_count,
                page_count,
                timestamp,
            ),
        )
        for page_number in range(1, page_count + 1):
            status = "pending_review"
            markdown = ""
            if page_number == 2:
                markdown = "# 未确认草稿"
            elif page_number == 3:
                status = "manually_reviewed"
            elif page_number == 4:
                status = "manually_reviewed"
                markdown = "# v2 自动标记的笔记"
            elif page_number == 5:
                status = "failed"
            connection.execute(
                """
                INSERT INTO pages(
                    id, document_id, page_number, image_path, extracted_text,
                    ocr_text, markdown_content, markdown_path, status,
                    processing_error, search_extracted_text, search_ocr_text,
                    search_markdown_content, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, '', ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    page_number,
                    page_number,
                    f"data/pages/1/page_{page_number:04d}.png",
                    f"第 {page_number} 页保留文本",
                    markdown,
                    f"data/markdown/1/page_{page_number:04d}.md" if markdown else None,
                    status,
                    "旧失败" if status == "failed" else "",
                    f"第 {page_number} 页 保留 文本",
                    markdown,
                    timestamp,
                    timestamp,
                ),
            )
        connection.commit()


def _create_v3_database(database_path: Path, page_count: int = 8) -> None:
    """Create a schema v3 database to exercise the v0.0.5 upgrade path."""

    _create_v2_database(database_path, page_count=page_count)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        migrations_module._apply_version_three(connection)


def test_initialization_is_idempotent_and_schema_has_fts5(tmp_path: Path) -> None:
    database_path = tmp_path / "database" / "knowledge.db"

    Database(database_path)
    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    assert {
        "documents",
        "pages",
        "page_search",
        "evidence_baskets",
        "evidence_items",
    } <= tables
    assert not any("user" in name.lower() or "account" in name.lower() for name in tables)
    assert migration_count == 8


def test_document_sha256_is_unique_and_metadata_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    database = Database(database_path)
    document = _create_document(database)

    assert document.id > 0
    assert document.page_count == 0
    assert database.get_document_by_sha256("A" * 64) == document

    with pytest.raises(DuplicateDocumentError, match="SHA-256"):
        _create_document(database)

    updated = database.update_document_page_count(document.id, 2)
    reopened = Database(database_path)

    assert updated.page_count == 2
    assert reopened.get_document(document.id) == updated
    assert reopened.list_documents() == [updated]


def test_page_review_update_and_chinese_fts_search(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    database = Database(database_path)
    document = _create_document(database)
    first_page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=Path("data/pages/1/page_0001.png"),
        extracted_text="液压泵需要定期检查压力和温度。",
        status=PageStatus.REVIEWED,
    )
    pending_page = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=Path("data/pages/1/page_0002.png"),
        status=PageStatus.PENDING,
    )

    assert database.list_pages(document.id) == [first_page, pending_page]
    assert database.get_page_by_number(document.id, 2) == pending_page
    assert database.list_pending_pages() == [pending_page]

    text_matches = database.search('"液压泵"')
    assert [result.page_id for result in text_matches] == [first_page.id]
    assert text_matches[0].document_title == "液压系统手册"
    assert text_matches[0].page_number == 1
    assert "液压泵" in text_matches[0].content

    draft_page = database.update_page_markdown(
        pending_page.id,
        "# 手写检修记录\n\n更换了密封件。",
        Path("data/markdown/1/page_0002.md"),
    )

    assert draft_page.status is PageStatus.DRAFT
    assert draft_page.note_updated_at is not None
    assert draft_page.reviewed_at is None
    assert draft_page.markdown_path == Path("data/markdown/1/page_0002.md")
    assert database.list_pending_pages(document.id) == [draft_page]
    assert database.search('"检修"')[0].page_id == pending_page.id

    reviewed_page = database.update_page(pending_page.id, status=PageStatus.REVIEWED)
    assert reviewed_page.reviewed_at is not None
    assert database.list_pending_pages(document.id) == []
    stats = database.dashboard_stats()
    assert (stats.pending_pages, stats.draft_pages, stats.reviewed_pages) == (0, 0, 2)

    reopened = Database(database_path)
    assert reopened.get_page(pending_page.id) == reviewed_page
    assert reopened.search('"检修"')[0].content.startswith("# 手写检修记录")


def test_invalid_search_and_missing_record_are_handled(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    assert database.search("") == []
    assert database.search('"') == []

    with pytest.raises(RecordNotFoundError, match="999"):
        database.update_document_page_count(999, 1)

    with pytest.raises(RecordNotFoundError, match="999"):
        database.update_page(999, status=PageStatus.READY)


def test_v1_database_is_backed_up_and_migrated_without_losing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    timestamp = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE documents(
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                source_path TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                page_count INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE pages(
                id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL, image_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL, markdown_content TEXT NOT NULL,
                markdown_path TEXT, status TEXT NOT NULL,
                search_extracted_text TEXT NOT NULL,
                search_markdown_content TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO schema_migrations VALUES (1, ?)", (timestamp,))
        connection.execute(
            "INSERT INTO documents VALUES (1, ?, ?, ?, ?, 1, ?, ?)",
            ("旧手册", "legacy.pdf", "data/raw/legacy.pdf", "b" * 64, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO pages VALUES (1, 1, 1, ?, ?, '', NULL, 'ready', ?, '', ?, ?)",
            (
                "data/pages/1/page_0001.png",
                "旧版提取文本",
                "旧版 提取 文本",
                timestamp,
                timestamp,
            ),
        )

    database = Database(database_path)

    assert database.last_backup_path is not None
    assert database.last_backup_path.is_file()
    assert database.get_document(1).title == "旧手册"  # type: ignore[union-attr]
    page = database.get_page(1)
    assert page is not None
    assert page.extracted_text == "旧版提取文本"
    assert page.status is PageStatus.TEXT_EXTRACTED
    assert database.search('"旧版"')[0].page_id == 1


def test_v2_database_with_78_pages_is_backed_up_and_migrated_losslessly(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v2_database(database_path)

    database = Database(database_path)

    assert database.last_backup_path is not None
    assert database.last_backup_path.is_file()
    assert len(database.list_documents()) == 1
    assert len(database.list_pages(1)) == 78
    stats = database.dashboard_stats()
    assert (
        stats.pending_pages,
        stats.draft_pages,
        stats.reviewed_pages,
        stats.skipped_pages,
        stats.failed_pages,
    ) == (74, 2, 1, 0, 1)
    assert stats.review_pages == 77
    progress = database.review_progress(1)
    assert (progress.processed, progress.total, progress.remaining) == (1, 78, 77)
    assert database.get_page_by_number(1, 2).status is PageStatus.DRAFT  # type: ignore[union-attr]
    assert database.get_page_by_number(1, 3).status is PageStatus.REVIEWED  # type: ignore[union-attr]
    assert database.get_page_by_number(1, 4).status is PageStatus.DRAFT  # type: ignore[union-attr]
    assert database.search('"保留"')

    with sqlite3.connect(database.last_backup_path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 78


def test_tags_projects_import_records_and_metadata_search(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _create_document(database)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=Path("data/pages/1/page_0001.png"),
        extracted_text="控制回路",
        status=PageStatus.TEXT_EXTRACTED,
    )

    first_tag = database.create_tag(" STM32 ")
    assert database.create_tag("stm32").id == first_tag.id
    database.set_document_tags(document.id, [first_tag.id])
    database.set_page_tags(page.id, [first_tag.id])
    assert database.list_tags()[0].usage_count == 2

    project = database.create_project("电赛电源", "本地资料整理")
    database.set_document_projects(document.id, [project.id])
    database.set_page_projects(page.id, [project.id])
    assert database.list_projects()[0].document_count == 1
    assert database.list_projects()[0].page_count == 1

    tag_result = database.search('"STM32"')[0]
    project_result = database.search('"电赛电源"')[0]
    assert tag_result.match_type == "标签"
    assert tag_result.tags == ("STM32",)
    assert project_result.match_type == "项目"
    assert project_result.projects == ("电赛电源",)

    record = database.create_import_record("new.pdf", "新资料", "c" * 64)
    finished = database.update_import_record(
        record.id,
        status=ImportStatus.COMPLETED,
        document_id=document.id,
        total_pages=1,
        processed_pages=1,
        text_pages=1,
    )
    assert finished.finished_at is not None
    assert database.list_import_records()[0] == finished

    database.delete_tag(first_tag.id)
    database.delete_project(project.id)
    assert database.get_document(document.id) is not None
    assert database.get_page(page.id) is not None
    assert database.list_tags() == []
    assert database.list_projects() == []


def test_failed_migration_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00');
            CREATE TABLE documents(
                id INTEGER PRIMARY KEY, title TEXT, filename TEXT, source_path TEXT,
                sha256 TEXT, page_count INTEGER, created_at TEXT, updated_at TEXT
            );
            """
        )

    with pytest.raises(MigrationError, match="迁移失败"):
        Database(database_path)

    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
    assert "import_status" not in columns


def test_failed_v3_migration_restores_v2_schema_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v2_database(database_path, page_count=8)

    def fail_fts_rebuild(connection: sqlite3.Connection) -> None:
        del connection
        raise sqlite3.OperationalError("simulated v3 FTS rebuild failure")

    monkeypatch.setattr(migrations_module, "_create_v2_fts", fail_fts_rebuild)

    with pytest.raises(MigrationError, match="迁移失败"):
        Database(database_path)

    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pages)")}
        assert "review_status" not in columns
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 8
        assert connection.execute("SELECT COUNT(*) FROM page_search").fetchone()[0] == 8


def test_v3_to_v4_migration_preserves_core_data_and_adds_empty_evidence_tables(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v3_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = migrations_module._core_data_fingerprint(connection)

    database = Database(database_path)

    assert database.last_backup_path is not None
    with sqlite3.connect(database_path) as connection:
        after = migrations_module._core_data_fingerprint(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        basket_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_baskets"
        ).fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_items"
        ).fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert before == after
    assert version == 8
    assert (basket_count, item_count) == (0, 0)


def test_failed_v4_migration_rolls_back_new_tables_and_keeps_v3_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v3_database(database_path)
    original_fingerprint = migrations_module._core_data_fingerprint
    calls = 0

    def changed_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        result = original_fingerprint(connection)
        return result if calls == 1 else (*result, "simulated change")

    monkeypatch.setattr(migrations_module, "_core_data_fingerprint", changed_fingerprint)

    with pytest.raises(MigrationError, match="schema v4"):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 8
    assert version == 3
    assert "evidence_baskets" not in tables
    assert "evidence_items" not in tables
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
