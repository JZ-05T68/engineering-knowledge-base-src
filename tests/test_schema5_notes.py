"""Schema 5 (structured notes) migration tests.

Covers the frozen v0.3.0 design: unified notes table, ownership exclusivity,
type-exclusive anchors, cascades, data preservation, rollback, and upgrade
safety. All databases are temporary fixtures; production data is never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database
from src.migrations import SCHEMA_VERSION, MigrationError

TS = "2026-07-30T00:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64

NOTE_COLUMNS = (
    "note_type", "document_id", "page_id", "personal_note",
    "source_kind", "source_page_text_sha256", "source_excerpt_snapshot",
    "selection_start", "selection_end", "user_excerpt",
    "region_image_sha256", "region_image_width", "region_image_height",
    "region_x0", "region_y0", "region_x1", "region_y1",
    "created_at", "updated_at",
)

VALID_ROWS: dict[str, tuple] = {
    "document": (
        "document", 1, None, "整本手册的笔记",
        None, None, None, None, None, None,
        None, None, None, None, None, None, None, TS, TS,
    ),
    "page": (
        "page", None, 1, "本页笔记",
        None, None, None, None, None, None,
        None, None, None, None, None, None, None, TS, TS,
    ),
    "text_selection": (
        "text_selection", None, 1, "这段很关键",
        "pdf_text", HASH_A, "液压系统", 0, 4, "液压",
        None, None, None, None, None, None, None, TS, TS,
    ),
    "image_region": (
        "image_region", None, 1, "阀体区域",
        None, None, None, None, None, None,
        HASH_B, 800, 1200, 10, 20, 300, 400, TS, TS,
    ),
}

PRESERVED_TABLES = (
    "documents", "pages", "evidence_baskets", "evidence_items",
    "tags", "projects", "document_tags", "page_tags",
    "project_documents", "project_pages",
)

# schema v7 重建了 evidence_items 并新增列；历史数据保留校验只针对 v4 形态的 19 列
EVIDENCE_COLUMNS_V4 = (
    "id", "basket_id", "document_id", "page_id", "document_title", "filename",
    "page_number", "review_status", "projects_json", "tags_json",
    "evidence_text", "text_kind", "context", "context_kind", "user_note",
    "source_text_sha256", "source_locator", "selection_sha256",
    "added_at", "position",
)


def _insert_note(connection: sqlite3.Connection, row: tuple) -> None:
    placeholders = ", ".join("?" for _ in NOTE_COLUMNS)
    connection.execute(
        f"INSERT INTO notes({', '.join(NOTE_COLUMNS)}) VALUES ({placeholders})", row
    )


def _mutated(base: str, **changes: object) -> tuple:
    row = dict(zip(NOTE_COLUMNS, VALID_ROWS[base], strict=True))
    row.update(changes)
    return tuple(row[column] for column in NOTE_COLUMNS)


def _create_v4_database(database_path: Path) -> None:
    """Build a representative schema v4 database with data in every table."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        migrations_module._apply_version_one(connection)
        migrations_module._apply_version_two(connection)
        migrations_module._apply_version_three(connection)
        migrations_module._apply_version_four(connection)
        connection.execute(
            "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
            " created_at, updated_at)"
            " VALUES ('液压手册', 'hyd.pdf', 'data/raw/hyd.pdf', ?, 1, ?, ?)",
            (HASH_A, TS, TS),
        )
        connection.execute(
            "INSERT INTO pages(document_id, page_number, image_path, extracted_text,"
            " ocr_text, markdown_content, markdown_path, status, review_status,"
            " search_extracted_text, search_ocr_text, search_markdown_content,"
            " created_at, updated_at, note_updated_at)"
            " VALUES (1, 1, 'data/pages/1/page_0001.png', '液压系统 原文', 'OCR 初稿',"
            " '# 整理稿', 'data/markdown/1/page_0001.md', 'text_extracted', 'draft',"
            " '液压 系统 原文', 'ocr 初稿', '整理稿', ?, ?, ?)",
            (TS, TS, TS),
        )
        connection.execute(
            "INSERT INTO evidence_baskets(id, name, created_at, updated_at)"
            " VALUES (1, '默认证据篮', ?, ?)", (TS, TS),
        )
        connection.execute(
            "INSERT INTO evidence_items(basket_id, document_id, page_id, document_title,"
            " filename, page_number, review_status, projects_json, tags_json,"
            " evidence_text, text_kind, context, context_kind, user_note,"
            " source_text_sha256, source_locator, selection_sha256, added_at, position)"
            " VALUES (1, 1, 1, '液压手册', 'hyd.pdf', 1, 'draft', '[]', '[]',"
            " '液压系统', 'original_material', '', 'system_generated', '备注',"
            " ?, 'page:1', ?, ?, 1)",
            (HASH_A, HASH_B, TS),
        )
        connection.execute(
            "INSERT INTO tags(id, name, normalized_name, created_at)"
            " VALUES (1, '液压', '液压', ?)", (TS,),
        )
        connection.execute(
            "INSERT INTO projects(id, name, normalized_name, description, status,"
            " created_at, updated_at) VALUES (1, '项目甲', '项目甲', '', 'active', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO document_tags(document_id, tag_id, created_at) VALUES (1, 1, ?)",
            (TS,),
        )
        connection.execute(
            "INSERT INTO page_tags(page_id, tag_id, created_at) VALUES (1, 1, ?)", (TS,),
        )
        connection.execute(
            "INSERT INTO project_documents(project_id, document_id, created_at)"
            " VALUES (1, 1, ?)", (TS,),
        )
        connection.execute(
            "INSERT INTO project_pages(project_id, page_id, created_at) VALUES (1, 1, ?)",
            (TS,),
        )
        connection.commit()


def _preserved_fingerprint(connection: sqlite3.Connection) -> dict[str, tuple]:
    fingerprint: dict[str, tuple] = {}
    for table in PRESERVED_TABLES:
        if table == "evidence_items":
            fingerprint[table] = tuple(
                connection.execute(
                    f"SELECT {', '.join(EVIDENCE_COLUMNS_V4)} FROM evidence_items"
                    " ORDER BY 1"
                ).fetchall()
            )
            continue
        fingerprint[table] = tuple(
            connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    fingerprint["page_search"] = tuple(
        connection.execute(
            "SELECT rowid, search_extracted_text, search_ocr_text,"
            " search_markdown_content FROM page_search ORDER BY rowid"
        ).fetchall()
    )
    return fingerprint


def _fresh_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    return database_path


def _seed_document_and_page(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
            " created_at, updated_at) VALUES ('手册', 'a.pdf', 'data/raw/a.pdf', ?, 1, ?, ?)",
            (HASH_A, TS, TS),
        )
        connection.execute(
            "INSERT INTO pages(document_id, page_number, image_path, status,"
            " review_status, created_at, updated_at)"
            " VALUES (1, 1, 'data/pages/1/page_0001.png', 'text_extracted', 'pending', ?, ?)",
            (TS, TS),
        )
        connection.commit()


# --- A. structure ---------------------------------------------------------


def test_schema_version_and_notes_structure(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 9
    database_path = _fresh_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(notes)")}
        foreign_keys = connection.execute("PRAGMA foreign_key_list(notes)").fetchall()
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'notes'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        virtual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE sql LIKE '%fts5%'"
            )
        }

    expected = set(NOTE_COLUMNS) | {"id", "importance"}
    assert set(columns) == expected
    for required in ("note_type", "personal_note", "created_at", "updated_at"):
        assert columns[required][3] == 1, f"{required} 应为 NOT NULL"
    for nullable in ("document_id", "page_id", "source_kind", "region_x0"):
        assert columns[nullable][3] == 0, f"{nullable} 应允许 NULL"

    fk_map = {row[2]: (row[3], row[6]) for row in foreign_keys}
    assert fk_map["documents"] == ("document_id", "CASCADE")
    assert fk_map["pages"] == ("page_id", "CASCADE")

    assert {
        "idx_notes_document", "idx_notes_page", "idx_notes_type", "idx_notes_importance"
    } == indexes
    assert triggers == {"pages_fts_insert", "pages_fts_delete", "pages_fts_update"}
    assert virtual_tables == {"page_search"}


# --- B. valid inserts ------------------------------------------------------


def test_insert_four_valid_note_types(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for row in VALID_ROWS.values():
            _insert_note(connection, row)
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 4


# --- C/D/E. invalid combinations ------------------------------------------


def _ownership_violations() -> list[tuple]:
    return [
        _mutated("document", page_id=1),
        _mutated("document", document_id=None),
        _mutated("page", document_id=1),
        _mutated("page", page_id=None),
        _mutated("text_selection", document_id=1),
        _mutated("image_region", document_id=1),
    ]


def _text_selection_violations() -> list[tuple]:
    return [
        _mutated("text_selection", source_kind="markdown"),
        _mutated("text_selection", source_page_text_sha256=None),
        _mutated("text_selection", source_page_text_sha256="abc"),
        _mutated("text_selection", source_excerpt_snapshot=""),
        _mutated("text_selection", user_excerpt=""),
        _mutated("text_selection", selection_start=-1),
        _mutated("text_selection", selection_end=0),
        _mutated("text_selection", source_excerpt_snapshot="不符"),
        _mutated("text_selection", region_image_width=800),
        _mutated("page", source_kind="pdf_text", source_page_text_sha256=HASH_A,
                 source_excerpt_snapshot="片段", selection_start=0, selection_end=2,
                 user_excerpt="片段"),
    ]


def _image_region_violations() -> list[tuple]:
    return [
        _mutated("image_region", region_image_sha256=None),
        _mutated("image_region", region_image_sha256="xyz"),
        _mutated("image_region", region_image_width=0),
        _mutated("image_region", region_image_height=-1),
        _mutated("image_region", region_x0=-1),
        _mutated("image_region", region_y0=-5),
        _mutated("image_region", region_x1=10),
        _mutated("image_region", region_y1=20),
        _mutated("image_region", region_x1=801),
        _mutated("image_region", region_y1=1201),
        _mutated("image_region", source_kind="ocr_text"),
        _mutated("page", region_image_sha256=HASH_B, region_image_width=800,
                 region_image_height=1200, region_x0=0, region_y0=0,
                 region_x1=100, region_y1=100),
    ]


@pytest.mark.parametrize(
    "row",
    _ownership_violations() + _text_selection_violations() + _image_region_violations(),
)
def test_invalid_note_rows_are_rejected(tmp_path: Path, row: tuple) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_note(connection, row)


@pytest.mark.parametrize("row", [
    _mutated("document", document_id=999),
    _mutated("page", page_id=999),
    _mutated("text_selection", page_id=999),
    _mutated("image_region", page_id=999),
])
def test_foreign_key_violations_are_rejected(tmp_path: Path, row: tuple) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_note(connection, row)


def test_empty_and_overlong_personal_note_rejected(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_note(connection, _mutated("page", personal_note=""))
        with pytest.raises(sqlite3.IntegrityError):
            _insert_note(connection, _mutated("page", personal_note="x" * 20001))


# --- F. cascades ------------------------------------------------------------


def test_document_delete_cascades_document_notes(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_note(connection, VALID_ROWS["document"])
        connection.commit()
        connection.execute("DELETE FROM documents WHERE id = 1")
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("DELETE FROM documents WHERE id = 1")  # 重复删除无异常


def test_document_delete_cascades_two_levels_to_page_notes(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for note_type in ("page", "text_selection", "image_region"):
            _insert_note(connection, VALID_ROWS[note_type])
        connection.commit()
        connection.execute("DELETE FROM documents WHERE id = 1")
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- G. data preservation ---------------------------------------------------


def test_v4_to_v5_preserves_all_existing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v4_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _preserved_fingerprint(connection)

    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        after = _preserved_fingerprint(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        note_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert before == after
    assert version == 9
    assert note_count == 0


# --- H. rollback -------------------------------------------------------------


def test_failed_v5_migration_rolls_back_and_keeps_v4_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v4_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _preserved_fingerprint(connection)
    original_fingerprint = migrations_module._core_data_fingerprint
    calls = 0

    def changed_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        result = original_fingerprint(connection)
        return result if calls == 1 else (*result, "simulated change")

    monkeypatch.setattr(migrations_module, "_core_data_fingerprint", changed_fingerprint)

    with pytest.raises(MigrationError, match="schema v5"):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        objects = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master")
        }
        after = _preserved_fingerprint(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == 4
    assert "notes" not in objects
    assert not any(name.startswith("idx_notes_") for name in objects)
    assert before == after
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


# --- I. init / re-migrate / high version -------------------------------------


def test_fresh_database_initializes_at_schema_5(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path / "sub")
    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_remigration_is_noop(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v4_database(database_path)
    Database(database_path)
    Database(database_path)
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        migration_rows = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    assert version == 9
    assert migration_rows == 9
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_higher_version_database_is_rejected(tmp_path: Path) -> None:
    database_path = _fresh_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (10, ?)", (TS,)
        )
        connection.commit()
    with pytest.raises(MigrationError, match="高于程序支持"):
        Database(database_path)
