"""Schema 9 (knowledge foundation) migration and constraint tests.

Covers the v0.5.2 additive migration: four new tables
(``knowledge_objects``, ``knowledge_object_sources``,
``knowledge_relations``, ``knowledge_memory_entries``) without any rebuild of
existing tables. Existing v8 data must be preserved exactly. All databases
are temporary fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database
from src.migrations import SCHEMA_VERSION, migrate_database

TS = "2026-08-01T00:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

KNOWLEDGE_OBJECT_COLUMNS = {
    "id", "kind", "title", "content", "importance", "status",
    "created_at", "updated_at", "reviewed_at",
}
KNOWLEDGE_SOURCE_COLUMNS = {
    "id", "knowledge_object_id", "source_type", "source_id",
    "source_note", "created_at",
}
KNOWLEDGE_RELATION_COLUMNS = {
    "id", "source_ko_id", "target_ko_id", "relation_type",
    "description", "created_at",
}
KNOWLEDGE_MEMORY_COLUMNS = {
    "id", "kind", "title", "content", "root_cause", "lesson",
    "knowledge_object_id", "document_id", "page_id",
    "created_at", "updated_at",
}


def _create_v8_database(database_path: Path) -> None:
    """Build a representative schema v8 database with legacy rows."""

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        migrations_module._apply_version_one(connection)
        migrations_module._apply_version_two(connection)
        migrations_module._apply_version_three(connection)
        connection.execute(
            "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
            " created_at, updated_at)"
            " VALUES ('液压手册', 'hyd.pdf', 'data/raw/hyd.pdf', ?, 2, ?, ?)",
            (HASH_A, TS, TS),
        )
        for page_number in (1, 2):
            connection.execute(
                "INSERT INTO pages(document_id, page_number, image_path, status,"
                " review_status, created_at, updated_at)"
                " VALUES (1, ?, ?, 'text_extracted', ?, ?, ?)",
                (
                    page_number,
                    f"data/pages/1/page_{page_number:04d}.png",
                    "reviewed" if page_number == 1 else "pending",
                    TS,
                    TS,
                ),
            )
        connection.commit()
        migrations_module._apply_version_four(connection)
        connection.execute(
            "INSERT INTO evidence_baskets(name, created_at, updated_at)"
            " VALUES ('默认证据篮', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO evidence_items(basket_id, document_id, page_id,"
            " document_title, filename, page_number, review_status, projects_json,"
            " tags_json, evidence_text, text_kind, context, context_kind, user_note,"
            " source_text_sha256, source_locator, selection_sha256, added_at, position)"
            " VALUES (1, 1, 1, '液压手册', 'hyd.pdf', 1, 'reviewed', '[]', '[]',"
            " '液压泵需要定期检查', 'original_material', '', 'system_generated', '',"
            " ?, 'document_id=1; page_id=1', ?, ?, 1)",
            (HASH_B, HASH_C, TS),
        )
        connection.commit()
        migrations_module._apply_version_five(connection)
        connection.execute(
            "INSERT INTO notes(note_type, page_id, personal_note, created_at, updated_at)"
            " VALUES ('page', 1, '第一页笔记', ?, ?)",
            (TS, TS),
        )
        connection.commit()
        migrations_module._apply_version_six(connection)
        migrations_module._apply_version_seven(connection)
        migrations_module._apply_version_eight(connection)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _insert_knowledge_object(
    connection: sqlite3.Connection,
    *,
    kind: str = "concept",
    title: str = "知识对象",
    content: str = "内容",
    importance: str = "normal",
    status: str = "draft",
) -> int:
    cursor = connection.execute(
        "INSERT INTO knowledge_objects(kind, title, content, importance, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, title, content, importance, status, TS, TS),
    )
    return int(cursor.lastrowid)


def test_migrate_v8_to_v9_preserves_data_and_adds_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v8_database(database_path)

    backup_path = migrate_database(database_path)

    assert backup_path is not None and backup_path.is_file()
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        documents = connection.execute(
            "SELECT title, filename, sha256 FROM documents"
        ).fetchall()
        pages = connection.execute(
            "SELECT id, page_number, review_status FROM pages ORDER BY id"
        ).fetchall()
        notes = connection.execute(
            "SELECT note_type, page_id, personal_note FROM notes"
        ).fetchall()
        evidence = connection.execute(
            "SELECT evidence_type, confirmation_status, evidence_text"
            " FROM evidence_items"
        ).fetchall()
        assert _table_columns(connection, "knowledge_objects") == KNOWLEDGE_OBJECT_COLUMNS
        assert _table_columns(connection, "knowledge_object_sources") == KNOWLEDGE_SOURCE_COLUMNS
        assert _table_columns(connection, "knowledge_relations") == KNOWLEDGE_RELATION_COLUMNS
        assert _table_columns(connection, "knowledge_memory_entries") == KNOWLEDGE_MEMORY_COLUMNS
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == SCHEMA_VERSION == 9
    assert documents == [("液压手册", "hyd.pdf", HASH_A)]
    assert pages == [(1, 1, "reviewed"), (2, 2, "pending")]
    assert notes == [("page", 1, "第一页笔记")]
    assert evidence == [
        ("text_selection", "unconfirmed", "液压泵需要定期检查")
    ]


def test_fresh_database_has_v9_structure(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    assert database.SCHEMA_VERSION == 9
    with sqlite3.connect(database.database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert _table_columns(connection, "knowledge_objects") == KNOWLEDGE_OBJECT_COLUMNS
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_remigration_is_noop(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v8_database(database_path)

    Database(database_path)
    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        object_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_objects"
        ).fetchone()[0]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert object_count == 0
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_knowledge_object_check_constraints_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        bad_rows = (
            ("audio", "标题", "内容", "normal", "draft"),  # 非法 kind
            ("concept", "   ", "内容", "normal", "draft"),  # 空标题
            ("concept", "标题", "", "normal", "draft"),  # 空内容
            ("concept", "标题", "内容", "urgent", "draft"),  # 非法重要度
            ("concept", "标题", "内容", "normal", "published"),  # 非法状态
        )
        for row in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO knowledge_objects(kind, title, content, importance,"
                    " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*row, TS, TS),
                )
            connection.rollback()


def test_knowledge_object_source_unique_and_check_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        ko_id = _insert_knowledge_object(connection)
        connection.execute(
            "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
            " source_id, created_at) VALUES (?, 'page', 1, ?)",
            (ko_id, TS),
        )
        with pytest.raises(sqlite3.IntegrityError):  # 重复来源
            connection.execute(
                "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
                " source_id, created_at) VALUES (?, 'page', 1, ?)",
                (ko_id, TS),
            )
        with pytest.raises(sqlite3.IntegrityError):  # 非法 source_type
            connection.execute(
                "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
                " source_id, created_at) VALUES (?, 'tag', 1, ?)",
                (ko_id, TS),
            )
        connection.rollback()


def test_knowledge_relation_self_loop_and_duplicate_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        first = _insert_knowledge_object(connection, title="对象A")
        second = _insert_knowledge_object(connection, title="对象B")
        with pytest.raises(sqlite3.IntegrityError):  # 自环
            connection.execute(
                "INSERT INTO knowledge_relations(source_ko_id, target_ko_id,"
                " relation_type, created_at) VALUES (?, ?, 'relates_to', ?)",
                (first, first, TS),
            )
        connection.execute(
            "INSERT INTO knowledge_relations(source_ko_id, target_ko_id,"
            " relation_type, created_at) VALUES (?, ?, 'relates_to', ?)",
            (first, second, TS),
        )
        with pytest.raises(sqlite3.IntegrityError):  # 重复关系
            connection.execute(
                "INSERT INTO knowledge_relations(source_ko_id, target_ko_id,"
                " relation_type, created_at) VALUES (?, ?, 'relates_to', ?)",
                (first, second, TS),
            )
        with pytest.raises(sqlite3.IntegrityError):  # 非法 relation_type
            connection.execute(
                "INSERT INTO knowledge_relations(source_ko_id, target_ko_id,"
                " relation_type, created_at) VALUES (?, ?, 'parent_of', ?)",
                (first, second, TS),
            )
        connection.rollback()


def test_knowledge_object_delete_cascades_links_and_nullifies_memory(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        first = _insert_knowledge_object(connection, title="对象A")
        second = _insert_knowledge_object(connection, title="对象B")
        connection.execute(
            "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
            " source_id, created_at) VALUES (?, 'page', 1, ?)",
            (first, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_relations(source_ko_id, target_ko_id,"
            " relation_type, created_at) VALUES (?, ?, 'supports', ?)",
            (first, second, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, knowledge_object_id,"
            " created_at, updated_at) VALUES ('knowledge_change', '变更', ?, ?, ?)",
            (first, TS, TS),
        )
        connection.execute("DELETE FROM knowledge_objects WHERE id = ?", (first,))
        remaining_sources = connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_sources"
        ).fetchone()[0]
        remaining_relations = connection.execute(
            "SELECT COUNT(*) FROM knowledge_relations"
        ).fetchone()[0]
        memory_ko = connection.execute(
            "SELECT knowledge_object_id FROM knowledge_memory_entries"
        ).fetchone()[0]
    assert remaining_sources == 0
    assert remaining_relations == 0
    assert memory_ko is None  # ON DELETE SET NULL


def test_knowledge_memory_check_constraints_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # 非法 kind
            connection.execute(
                "INSERT INTO knowledge_memory_entries(kind, title, created_at, updated_at)"
                " VALUES ('diary', '标题', ?, ?)",
                (TS, TS),
            )
        with pytest.raises(sqlite3.IntegrityError):  # 空标题
            connection.execute(
                "INSERT INTO knowledge_memory_entries(kind, title, created_at, updated_at)"
                " VALUES ('experience', '   ', ?, ?)",
                (TS, TS),
            )
        connection.rollback()
