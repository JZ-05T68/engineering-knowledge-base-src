"""Tests for durable SQLite metadata and FTS5 behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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

    assert {"documents", "pages", "page_search"} <= tables
    assert not any("user" in name.lower() or "account" in name.lower() for name in tables)
    assert migration_count == 2


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
        status=PageStatus.READY,
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

    reviewed_page = database.update_page_markdown(
        pending_page.id,
        "# 手写检修记录\n\n更换了密封件。",
        Path("data/markdown/1/page_0002.md"),
    )

    assert reviewed_page.status is PageStatus.MANUALLY_REVIEWED
    assert reviewed_page.markdown_path == Path("data/markdown/1/page_0002.md")
    assert database.list_pending_pages(document.id) == []
    assert database.search('"检修"')[0].page_id == pending_page.id

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
