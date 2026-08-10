"""Schema 6 (note importance) migration and model tests.

Covers the frozen v0.3.1 design (docs/design-v0.3.1.md R1): the additive
``notes.importance`` column with legacy default 'normal', the single-row
``note_display_preferences`` table, the importance index, rollback,
idempotence and DB-level constraints. All databases are temporary fixtures;
production data is never touched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database
from src.migrations import SCHEMA_VERSION, MigrationError
from src.models import Note, NoteDisplayPreferences, NoteImportance, NoteType

TS = "2026-07-30T00:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64

# v5 形态的 19 列（不含 importance；迁移前旧行只有这些列）
NOTE_COLUMNS_V5 = (
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


def _create_v5_database(database_path: Path) -> None:
    """Build a representative schema v5 database with four note types."""

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
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
        connection.commit()
        migrations_module._apply_version_five(connection)
        placeholders = ", ".join("?" for _ in NOTE_COLUMNS_V5)
        for row in VALID_ROWS.values():
            connection.execute(
                f"INSERT INTO notes({', '.join(NOTE_COLUMNS_V5)})"
                f" VALUES ({placeholders})",
                row,
            )
        connection.commit()


def _preserved_fingerprint(connection: sqlite3.Connection) -> dict[str, tuple]:
    fingerprint: dict[str, tuple] = {}
    for table in PRESERVED_TABLES:
        fingerprint[table] = tuple(
            connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    fingerprint["page_search"] = tuple(
        connection.execute(
            "SELECT rowid, search_extracted_text, search_ocr_text,"
            " search_markdown_content FROM page_search ORDER BY rowid"
        ).fetchall()
    )
    fingerprint["notes_v5_columns"] = tuple(
        connection.execute(
            f"SELECT {', '.join(NOTE_COLUMNS_V5)} FROM notes ORDER BY id"
        ).fetchall()
    )
    return fingerprint


def _fresh_v6_database(tmp_path: Path) -> Path:
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


# --- A. models -----------------------------------------------------------------


def test_importance_enum_values_and_labels() -> None:
    assert [item.value for item in NoteImportance] == ["primary", "secondary", "normal"]
    assert (
        NoteImportance.PRIMARY.label,
        NoteImportance.SECONDARY.label,
        NoteImportance.NORMAL.label,
    ) == ("重点", "次重点", "一般")
    assert NoteImportance("primary") is NoteImportance.PRIMARY
    with pytest.raises(ValueError):
        NoteImportance("key")  # 已废弃语义码不得复活


def _minimal_note(**overrides: object) -> Note:
    values: dict[str, object] = {
        "id": 1,
        "note_type": NoteType.PAGE,
        "document_id": None,
        "page_id": 1,
        "personal_note": "笔记",
        "created_at": datetime(2026, 8, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return Note(**values)  # type: ignore[arg-type]


def test_note_default_importance_is_normal() -> None:
    assert _minimal_note().importance == "normal"


def test_note_explicit_importance_and_frozen_behavior() -> None:
    note = _minimal_note(importance="primary")
    assert note.importance == "primary"
    with pytest.raises(FrozenInstanceError):
        note.importance = "secondary"  # type: ignore[misc]


def test_display_preferences_defaults_are_canonical_lowercase() -> None:
    preferences = NoteDisplayPreferences()
    for value in (
        preferences.color_primary,
        preferences.color_secondary,
        preferences.color_normal,
    ):
        assert value == value.lower()
        assert len(value) == 7 and value.startswith("#")
        int(value[1:], 16)


# --- B. migration v5 → v6 ---------------------------------------------------------


def test_v5_to_v6_upgrade_defaults_normal_and_preserves_data(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v5_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _preserved_fingerprint(connection)

    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        after = _preserved_fingerprint(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        levels = {
            row[0]
            for row in connection.execute("SELECT importance FROM notes")
        }
        note_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert before == after
    assert version == 6 == SCHEMA_VERSION
    assert note_count == 4
    assert levels == {"normal"}


def test_preference_default_row_index_and_version(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v5_database(database_path)
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, color_primary, color_secondary, color_normal, updated_at"
            " FROM note_display_preferences"
        ).fetchall()
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'notes'"
            )
        }
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == 1
    for color in row[1:4]:
        assert color == color.lower() and len(color) == 7 and color.startswith("#")
    assert row[4]
    assert "idx_notes_importance" in indexes


def test_v6_migration_keeps_pre_upgrade_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v5_database(database_path)
    database = Database(database_path)
    assert database.last_backup_path is not None
    assert database.last_backup_path.is_file()


# --- C. idempotence ---------------------------------------------------------------


def test_remigration_is_noop_and_preference_stays_single(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v5_database(database_path)
    Database(database_path)
    Database(database_path)
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        preference_rows = connection.execute(
            "SELECT COUNT(*) FROM note_display_preferences"
        ).fetchone()[0]
        note_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    assert versions == [1, 2, 3, 4, 5, 6]
    assert preference_rows == 1
    assert note_count == 4
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


# --- D. rollback / failure injection -----------------------------------------------


def test_failed_v6_migration_rolls_back_and_keeps_v5_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入点位于全部 DDL 之后的指纹校验——事务中途失败必须整体回滚。"""
    database_path = tmp_path / "knowledge.db"
    _create_v5_database(database_path)
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

    with pytest.raises(MigrationError, match="schema v6"):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        objects = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master")
        }
        notes_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(notes)")
        }
        after = _preserved_fingerprint(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == 5
    assert "importance" not in notes_columns  # 无半升级列状态
    assert "note_display_preferences" not in objects
    assert "idx_notes_importance" not in objects
    assert before == after
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


# --- E. DB-level constraints --------------------------------------------------------


def test_invalid_importance_rejected_by_check(tmp_path: Path) -> None:
    database_path = _fresh_v6_database(tmp_path)
    _seed_document_and_page(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        placeholders = ", ".join("?" for _ in NOTE_COLUMNS_V5)
        for bad in ("key", "high", "red", "", "PRIMARY"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO notes({', '.join(NOTE_COLUMNS_V5)}, importance)"
                    f" VALUES ({placeholders}, ?)",
                    (*VALID_ROWS["page"], bad),
                )
        connection.rollback()


def test_preference_color_and_single_row_constraints(tmp_path: Path) -> None:
    database_path = _fresh_v6_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        for bad_color in ("red", "#fff", "#gg0000", "#C0392B", "#c0392b "):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE note_display_preferences SET color_primary = ? WHERE id = 1",
                    (bad_color,),
                )
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO note_display_preferences(id, updated_at) VALUES (2, ?)",
                (TS,),
            )
        connection.rollback()
        # 合法小写颜色允许写入
        connection.execute(
            "UPDATE note_display_preferences SET color_primary = '#112233' WHERE id = 1"
        )
        connection.commit()
        assert connection.execute(
            "SELECT color_primary FROM note_display_preferences WHERE id = 1"
        ).fetchone()[0] == "#112233"
