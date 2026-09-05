"""Schema 11 (Phase 3B knowledge FTS) migration and integrity tests.

Covers the v10 -> v11 additive migration: shadow tokenized columns on
``knowledge_objects`` / ``knowledge_memory_entries``, the two external-content
FTS5 tables, the six sync triggers, legacy-row backfill, rebuild, idempotency,
pre-migration backup uniqueness, failure-injection rollback and the v11
knowledge data-integrity fingerprint. All databases are temporary fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database, _tokenize_for_fts
from src.migrations import (
    SCHEMA_VERSION,
    MigrationError,
    _read_schema_version,
    migrate_database,
)

TS = "2026-08-01T00:00:00+00:00"

KNOWLEDGE_OBJECT_SEARCH_COLUMNS = {"search_title", "search_content"}
KNOWLEDGE_MEMORY_SEARCH_COLUMNS = {
    "search_title",
    "search_content",
    "search_root_cause",
    "search_lesson",
}
KNOWLEDGE_TRIGGERS = {
    "knowledge_objects_fts_insert",
    "knowledge_objects_fts_delete",
    "knowledge_objects_fts_update",
    "knowledge_memory_fts_insert",
    "knowledge_memory_fts_delete",
    "knowledge_memory_fts_update",
}
ALL_VIRTUAL_TABLES = {
    "page_search",
    "knowledge_object_search",
    "knowledge_memory_search",
}
V11_INJECTION_POINTS = [
    "v11_ko_columns",
    "v11_memory_columns",
    "v11_ko_backfill",
    "v11_memory_backfill",
    "v11_ko_fts",
    "v11_ko_triggers",
    "v11_memory_fts",
    "v11_memory_triggers",
    "v11_rebuild",
    "v11_version_record",
    "v11_before_commit",
]


def _build_v10_database(database_path: Path) -> None:
    """Build an empty schema-v10 database using the real migration chain."""
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        migrations_module._apply_version_one(connection)
        migrations_module._apply_version_two(connection)
        migrations_module._apply_version_three(connection)
        migrations_module._apply_version_four(connection)
        migrations_module._apply_version_five(connection)
        migrations_module._apply_version_six(connection)
        migrations_module._apply_version_seven(connection)
        migrations_module._apply_version_eight(connection)
        migrations_module._apply_version_nine(connection)
        migrations_module._apply_version_ten(connection)
        connection.commit()


def _seed_v10_knowledge_data(database_path: Path) -> None:
    """Insert representative v10 knowledge rows with no shadow columns."""
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO knowledge_objects(
                id, kind, authorship, epistemic_basis, title, content, importance,
                lifecycle, superseded_by_ko_id, confirmation_status, confirmed_at,
                confirmed_revision, current_revision, created_at, updated_at
            ) VALUES
                (1, 'concept', 'user', 'source_derived', '液压系统故障分析',
                 '齿轮泵压力脉动分析', 'normal', 'active', NULL, 'unconfirmed',
                 NULL, NULL, 1, ?, ?),
                (2, 'fact', 'user', 'personal_experience', 'Cavitation analysis',
                 'pump cavitation should be checked', 'primary', 'active', NULL,
                 'confirmed', ?, 1, 1, ?, ?),
                (3, 'principle', 'user', 'personal_judgment', '定时器 Timer 预分频器',
                 'PWM prescaler 配置与分频系数', 'secondary', 'archived', NULL,
                 'unconfirmed', NULL, NULL, 1, ?, ?)
            """,
            (TS, TS, TS, TS, TS, TS, TS),
        )
        connection.execute(
            """
            INSERT INTO knowledge_memory_entries(
                id, kind, title, content, root_cause, lesson,
                knowledge_object_id, document_id, page_id, status, created_at, updated_at
            ) VALUES
                (1, 'experience', '调试经验', '复位电路问题', '电源上电时序错误',
                 '增加去耦电容', 1, NULL, NULL, 'active', ?, ?),
                (2, 'decision', 'Empty optional fields', '', '', '', NULL, NULL,
                 NULL, 'active', ?, ?)
            """,
            (TS, TS, TS, TS),
        )
        connection.execute(
            """
            INSERT INTO knowledge_object_sources(
                id, knowledge_object_id, source_type, source_id, source_note,
                source_fingerprint, fingerprint_version, captured_at, created_at
            ) VALUES (1, 1, 'page', 1, '关键页', NULL, 1, ?, ?)
            """,
            (TS, TS),
        )
        connection.execute(
            """
            INSERT INTO knowledge_relations(
                id, source_ko_id, target_ko_id, relation_type, description, created_at
            ) VALUES (1, 1, 2, 'supports', '证据支持', ?)
            """,
            (TS,),
        )
        connection.execute(
            """
            INSERT INTO knowledge_object_revisions(
                id, knowledge_object_id, object_local_id_snapshot,
                object_stable_id_snapshot, object_title_snapshot,
                object_kind_snapshot, revision_number, event_type,
                before_title, after_title, before_content, after_content,
                payload_version, detail, created_at
            ) VALUES
                (1, 1, 1, 'kb:knowledge_object:1', '液压系统故障分析', 'concept',
                 1, 'created', NULL, '液压系统故障分析', NULL, '齿轮泵压力脉动分析',
                 1, '创建', ?),
                (2, 1, 1, 'kb:knowledge_object:1', '液压系统故障分析', 'concept',
                 2, 'content_updated', '液压系统故障分析', '液压系统故障分析',
                 '旧内容', '齿轮泵压力脉动分析', 1, '内容更新', ?)
            """,
            (TS, TS),
        )
        connection.commit()


def _raw_knowledge_snapshot(connection: sqlite3.Connection) -> tuple[object, ...]:
    """Return the raw business rows that v11 must preserve exactly."""
    return (
        tuple(
            connection.execute(
                "SELECT id, kind, authorship, epistemic_basis, title, content,"
                " importance, lifecycle, superseded_by_ko_id, confirmation_status,"
                " confirmed_at, confirmed_revision, current_revision, created_at,"
                " updated_at FROM knowledge_objects ORDER BY id"
            ).fetchall()
        ),
        tuple(
            connection.execute(
                "SELECT id, kind, title, content, root_cause, lesson,"
                " knowledge_object_id, document_id, page_id, status, created_at,"
                " updated_at FROM knowledge_memory_entries ORDER BY id"
            ).fetchall()
        ),
        tuple(
            connection.execute(
                "SELECT id, knowledge_object_id, source_type, source_id,"
                " source_note, source_fingerprint, fingerprint_version,"
                " captured_at, created_at FROM knowledge_object_sources ORDER BY id"
            ).fetchall()
        ),
        tuple(
            connection.execute(
                "SELECT id, source_ko_id, target_ko_id, relation_type,"
                " description, created_at FROM knowledge_relations ORDER BY id"
            ).fetchall()
        ),
        tuple(
            connection.execute(
                "SELECT * FROM knowledge_object_revisions ORDER BY id"
            ).fetchall()
        ),
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _fts_match_rowids(
    connection: sqlite3.Connection, table: str, match_expression: str
) -> set[int]:
    rows = connection.execute(
        f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (match_expression,)
    ).fetchall()
    return {int(row[0]) for row in rows}


def _seeded_v10_path(tmp_path: Path) -> Path:
    database_path = tmp_path / "knowledge.db"
    _build_v10_database(database_path)
    _seed_v10_knowledge_data(database_path)
    return database_path


# --- fresh database ---------------------------------------------------------


def test_fresh_database_migrates_1_to_14(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    assert SCHEMA_VERSION == 14
    assert database.SCHEMA_VERSION == 14
    with sqlite3.connect(database.database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert KNOWLEDGE_OBJECT_SEARCH_COLUMNS <= _table_columns(
            connection, "knowledge_objects"
        )
        assert KNOWLEDGE_MEMORY_SEARCH_COLUMNS <= _table_columns(
            connection, "knowledge_memory_entries"
        )
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
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert KNOWLEDGE_TRIGGERS <= triggers
    assert virtual_tables == ALL_VIRTUAL_TABLES


# --- non-empty v10 -> v11 ---------------------------------------------------


def test_v10_to_v11_preserves_knowledge_data_and_backfills(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    with sqlite3.connect(database_path) as connection:
        before = _raw_knowledge_snapshot(connection)

    backup_path = migrate_database(database_path)

    assert backup_path is not None and backup_path.is_file()
    assert backup_path.name.startswith("knowledge.v10.")
    with sqlite3.connect(database_path) as connection:
        after = _raw_knowledge_snapshot(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == 14
    assert before == after


def test_knowledge_data_integrity_fingerprint_ignores_shadow_columns(
    tmp_path: Path,
) -> None:
    database_path = _seeded_v10_path(tmp_path)
    with sqlite3.connect(database_path) as connection:
        before_fingerprint = migrations_module._knowledge_data_fingerprint(connection)

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        after_fingerprint = migrations_module._knowledge_data_fingerprint(connection)
        # Shadow columns must exist and be backfilled, yet stay outside the
        # raw-data fingerprint.
        shadow_rows = connection.execute(
            "SELECT id, search_title, search_content FROM knowledge_objects"
            " ORDER BY id"
        ).fetchall()
    assert before_fingerprint == after_fingerprint
    assert shadow_rows[0][1] == _tokenize_for_fts("液压系统故障分析")
    assert shadow_rows[0][2] == _tokenize_for_fts("齿轮泵压力脉动分析")


# --- shadow backfill + FTS MATCH -------------------------------------------


def test_chinese_knowledge_object_backfill_and_match(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        shadow_title, shadow_content = connection.execute(
            "SELECT search_title, search_content FROM knowledge_objects WHERE id = 1"
        ).fetchone()
        assert "液压" in shadow_title.split()
        assert "齿轮泵" in shadow_content.split()
        assert _fts_match_rowids(
            connection, "knowledge_object_search", "液压"
        ) == {1}
        assert _fts_match_rowids(
            connection, "knowledge_object_search", "齿轮泵"
        ) == {1}


def test_english_knowledge_object_backfill_and_match(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        shadow_title = connection.execute(
            "SELECT search_title FROM knowledge_objects WHERE id = 2"
        ).fetchone()[0]
        assert shadow_title == "cavitation analysis"
        assert _fts_match_rowids(
            connection, "knowledge_object_search", "cavitation"
        ) == {2}


def test_mixed_language_knowledge_object_backfill_and_match(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        shadow_title, shadow_content = connection.execute(
            "SELECT search_title, search_content FROM knowledge_objects WHERE id = 3"
        ).fetchone()
        assert "timer" in shadow_title.split()
        assert "定时器" in shadow_title.split()
        assert "prescaler" in shadow_content.split()
        assert _fts_match_rowids(
            connection, "knowledge_object_search", "timer"
        ) == {3}
        assert _fts_match_rowids(
            connection, "knowledge_object_search", "prescaler"
        ) == {3}


def test_memory_four_shadow_fields_backfill_and_match(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT search_title, search_content, search_root_cause, search_lesson"
            " FROM knowledge_memory_entries WHERE id = 1"
        ).fetchone()
        assert "调试" in row[0].split()
        assert "复位" in row[1].split()
        assert "时序" in row[2].split()
        assert "电容" in row[3].split()
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "search_title:调试"
        ) == {1}
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "search_content:复位"
        ) == {1}
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "search_root_cause:时序"
        ) == {1}
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "search_lesson:电容"
        ) == {1}
        # Field-specific recall: a lesson token must not hit the title column.
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "search_title:电容"
        ) == set()


def test_empty_memory_optional_fields_backfill_and_rebuild(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT search_title, search_content, search_root_cause, search_lesson"
            " FROM knowledge_memory_entries WHERE id = 2"
        ).fetchone()
        assert row == ("empty optional fields", "", "", "")
        assert _fts_match_rowids(
            connection, "knowledge_memory_search", "empty"
        ) == {2}


# --- FTS structure / triggers / rebuild / revision --------------------------


def test_knowledge_fts_tables_have_expected_schema(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        object_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_object_search'"
        ).fetchone()[0]
        memory_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_memory_search'"
        ).fetchone()[0]
        virtual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE sql LIKE '%fts5%'"
            )
        }
    assert "fts5" in object_sql
    assert "search_title" in object_sql and "search_content" in object_sql
    assert "content='knowledge_objects'" in object_sql
    assert "content_rowid='id'" in object_sql
    assert "tokenize='unicode61 remove_diacritics 2'" in object_sql
    assert "fts5" in memory_sql
    assert "search_root_cause" in memory_sql and "search_lesson" in memory_sql
    assert "content='knowledge_memory_entries'" in memory_sql
    assert "content_rowid='id'" in memory_sql
    assert "tokenize='unicode61 remove_diacritics 2'" in memory_sql
    assert virtual_tables == ALL_VIRTUAL_TABLES


def test_six_knowledge_triggers_exist(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert KNOWLEDGE_TRIGGERS <= triggers


def test_legacy_rows_are_searchable_immediately_after_rebuild(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    # No service restart, no manual rebuild: MATCH must see migrated rows.
    with sqlite3.connect(database_path) as connection:
        object_rows = connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_search"
        ).fetchone()[0]
        memory_rows = connection.execute(
            "SELECT COUNT(*) FROM knowledge_memory_search"
        ).fetchone()[0]
    assert object_rows == 3
    assert memory_rows == 2


def test_revisions_never_get_an_fts_index(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        virtual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE sql LIKE '%fts5%'"
            )
        }
        revision_fts_objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            " AND sql IS NOT NULL AND sql LIKE '%fts5%'"
            " AND (name LIKE '%revision%' OR sql LIKE '%knowledge_object_revisions%')"
        ).fetchall()
    assert virtual_tables == ALL_VIRTUAL_TABLES
    assert revision_fts_objects == []


# --- idempotency / backup ---------------------------------------------------


def test_migration_is_idempotent_and_backup_unique(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)

    first_backup = migrate_database(database_path)
    assert first_backup is not None
    with sqlite3.connect(database_path) as connection:
        after_first = _raw_knowledge_snapshot(connection)
        fts_snapshot = (
            connection.execute(
                "SELECT rowid, search_title, search_content"
                " FROM knowledge_object_search ORDER BY rowid"
            ).fetchall(),
            connection.execute(
                "SELECT rowid, search_title, search_content, search_root_cause,"
                " search_lesson FROM knowledge_memory_search ORDER BY rowid"
            ).fetchall(),
        )

    second_backup = migrate_database(database_path)
    assert second_backup is None
    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert _raw_knowledge_snapshot(connection) == after_first
        assert (
            connection.execute(
                "SELECT rowid, search_title, search_content"
                " FROM knowledge_object_search ORDER BY rowid"
            ).fetchall(),
            connection.execute(
                "SELECT rowid, search_title, search_content, search_root_cause,"
                " search_lesson FROM knowledge_memory_search ORDER BY rowid"
            ).fetchall(),
        ) == fts_snapshot
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    backups = list((database_path.parent / "backups").glob("knowledge.v10.*.db"))
    assert len(backups) == 1


# --- failure injection / rollback -------------------------------------------


@pytest.mark.parametrize("injection_point", V11_INJECTION_POINTS)
def test_v11_failure_injection_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection_point: str
) -> None:
    database_path = _seeded_v10_path(tmp_path)
    with sqlite3.connect(database_path) as connection:
        before = _raw_knowledge_snapshot(connection)

    monkeypatch.setattr(migrations_module, "_V11_INJECTION_POINT", injection_point)
    with pytest.raises(MigrationError, match=injection_point):
        migrate_database(database_path)
    monkeypatch.setattr(migrations_module, "_V11_INJECTION_POINT", None)

    with sqlite3.connect(database_path) as connection:
        assert _read_schema_version(database_path) == 10
        assert _raw_knowledge_snapshot(connection) == before
        object_columns = _table_columns(connection, "knowledge_objects")
        memory_columns = _table_columns(connection, "knowledge_memory_entries")
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
        assert "search_title" not in object_columns
        assert "search_title" not in memory_columns
        assert not ({"knowledge_object_search", "knowledge_memory_search"} & objects)
        assert not (KNOWLEDGE_TRIGGERS & objects)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # A re-run must succeed once the injection is cleared.
    migrate_database(database_path)
    assert _read_schema_version(database_path) == 14


# --- existing page FTS stability -------------------------------------------


def test_page_search_and_pages_triggers_unchanged_by_v11(tmp_path: Path) -> None:
    database_path = _seeded_v10_path(tmp_path)
    with sqlite3.connect(database_path) as connection:
        before_pages_columns = _table_columns(connection, "pages")
        before_page_objects = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ("
            "'page_search', 'pages_fts_insert', 'pages_fts_delete',"
            "'pages_fts_update') ORDER BY name"
        ).fetchall()
        before_page_search_rows = connection.execute(
            "SELECT COUNT(*) FROM page_search"
        ).fetchone()[0]

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        assert _table_columns(connection, "pages") == before_pages_columns
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ("
            "'page_search', 'pages_fts_insert', 'pages_fts_delete',"
            "'pages_fts_update') ORDER BY name"
        ).fetchall() == before_page_objects
        assert (
            connection.execute("SELECT COUNT(*) FROM page_search").fetchone()[0]
            == before_page_search_rows
        )
