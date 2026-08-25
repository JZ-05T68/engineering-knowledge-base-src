"""Schema 10 (Phase 2B knowledge foundation) migration and constraint tests.

Covers the v9 → v10 rebuild: orthogonal ``knowledge_objects`` fields,
``knowledge_base_meta`` UUID, ``knowledge_object_revisions`` (legacy event and
baseline migration), the user-only ``knowledge_memory_entries`` rebuild, and
the additive fingerprint columns on ``knowledge_object_sources``. Existing v9
rows must be preserved exactly. All databases are temporary fixtures.
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
    "id", "kind", "authorship", "epistemic_basis", "title", "content",
    "importance", "lifecycle", "superseded_by_ko_id", "confirmation_status",
    "confirmed_at", "confirmed_revision", "current_revision", "created_at",
    "updated_at", "search_title", "search_content",
}
KNOWLEDGE_SOURCE_COLUMNS = {
    "id", "knowledge_object_id", "source_type", "source_id",
    "source_note", "source_fingerprint", "fingerprint_version",
    "captured_at", "created_at",
}
KNOWLEDGE_RELATION_COLUMNS = {
    "id", "source_ko_id", "target_ko_id", "relation_type",
    "description", "created_at",
}
KNOWLEDGE_MEMORY_COLUMNS = {
    "id", "kind", "title", "content", "root_cause", "lesson",
    "knowledge_object_id", "document_id", "page_id", "status",
    "created_at", "updated_at", "search_title", "search_content",
    "search_root_cause", "search_lesson",
}
KNOWLEDGE_REVISION_COLUMNS = {
    "id", "knowledge_object_id", "object_local_id_snapshot",
    "object_stable_id_snapshot", "object_title_snapshot",
    "object_kind_snapshot", "revision_number", "event_type",
    "before_title", "after_title", "before_content", "after_content",
    "before_lifecycle", "after_lifecycle", "before_confirmation",
    "after_confirmation", "superseded_by_before", "superseded_by_after",
    "source_ref", "payload_version", "detail", "created_at",
}


def _create_v9_database(database_path: Path) -> None:
    """Build a representative schema v9 database with legacy rows."""

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
        migrations_module._apply_version_nine(connection)
        # v9 knowledge data: reviewed / draft / archived objects.
        connection.execute(
            "INSERT INTO knowledge_objects(kind, title, content, importance, status,"
            " created_at, updated_at, reviewed_at)"
            " VALUES ('fact', '事实A', '内容A', 'normal', 'reviewed', ?, ?, ?)",
            (TS, TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_objects(kind, title, content, importance, status,"
            " created_at, updated_at)"
            " VALUES ('experience', '经验B', '内容B', 'primary', 'draft', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_objects(kind, title, content, importance, status,"
            " created_at, updated_at)"
            " VALUES ('concept', '概念C', '内容C', 'normal', 'archived', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
            " source_id, source_note, created_at) VALUES (1, 'page', 1, '关键页', ?)",
            (TS,),
        )
        connection.execute(
            "INSERT INTO knowledge_relations(source_ko_id, target_ko_id, relation_type,"
            " description, created_at) VALUES (1, 2, 'supports', '证据支持', ?)",
            (TS,),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content,"
            " knowledge_object_id, created_at, updated_at)"
            " VALUES ('knowledge_change', '知识创建：事实A', '旧日志1', 1, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content,"
            " knowledge_object_id, created_at, updated_at)"
            " VALUES ('knowledge_change', '知识更新：事实A', '旧日志2', 1, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content,"
            " created_at, updated_at)"
            " VALUES ('knowledge_change', '知识创建：孤儿对象', '孤儿日志', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content, root_cause,"
            " lesson, created_at, updated_at)"
            " VALUES ('experience', '用户经验', '经验正文', '原因', '教训', ?, ?)",
            (TS, TS),
        )
        connection.commit()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _insert_knowledge_object(
    connection: sqlite3.Connection,
    *,
    kind: str = "concept",
    title: str = "知识对象",
    content: str = "内容",
    importance: str = "normal",
    lifecycle: str = "active",
    confirmation_status: str = "unconfirmed",
) -> int:
    cursor = connection.execute(
        "INSERT INTO knowledge_objects(kind, authorship, epistemic_basis, title,"
        " content, importance, lifecycle, confirmation_status, created_at, updated_at)"
        " VALUES (?, 'user', 'unknown_legacy', ?, ?, ?, ?, ?, ?, ?)",
        (kind, title, content, importance, lifecycle, confirmation_status, TS, TS),
    )
    return int(cursor.lastrowid)


def test_migrate_v9_to_v10_preserves_data_and_adds_structures(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v9_database(database_path)

    backup_path = migrate_database(database_path)

    assert backup_path is not None and backup_path.is_file()
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        objects = connection.execute(
            "SELECT id, kind, authorship, epistemic_basis, lifecycle,"
            " confirmation_status, confirmed_revision, current_revision, title"
            " FROM knowledge_objects ORDER BY id"
        ).fetchall()
        sources = connection.execute(
            "SELECT knowledge_object_id, source_type, source_id, source_fingerprint,"
            " fingerprint_version FROM knowledge_object_sources"
        ).fetchall()
        relations = connection.execute(
            "SELECT source_ko_id, target_ko_id, relation_type FROM knowledge_relations"
        ).fetchall()
        memories = connection.execute(
            "SELECT id, kind, status FROM knowledge_memory_entries ORDER BY id"
        ).fetchall()
        revisions = connection.execute(
            "SELECT knowledge_object_id, object_title_snapshot, revision_number,"
            " event_type FROM knowledge_object_revisions ORDER BY id"
        ).fetchall()
        assert _table_columns(connection, "knowledge_objects") == KNOWLEDGE_OBJECT_COLUMNS
        assert _table_columns(connection, "knowledge_object_sources") == KNOWLEDGE_SOURCE_COLUMNS
        assert _table_columns(connection, "knowledge_relations") == KNOWLEDGE_RELATION_COLUMNS
        assert _table_columns(connection, "knowledge_memory_entries") == KNOWLEDGE_MEMORY_COLUMNS
        assert (
            _table_columns(connection, "knowledge_object_revisions")
            == KNOWLEDGE_REVISION_COLUMNS
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == SCHEMA_VERSION == 11
    # v9 reviewed → active + confirmed, confirmation bound to baseline (#3).
    assert objects[0] == (
        1, "fact", "user", "unknown_legacy", "active", "confirmed", 3, 3, "事实A",
    )
    # v9 draft → active + unconfirmed.
    assert objects[1][:7] == (
        2, "experience", "user", "unknown_legacy", "active", "unconfirmed", None,
    )
    # v9 archived → archived + unconfirmed.
    assert objects[2][:7] == (
        3, "concept", "user", "unknown_legacy", "archived", "unconfirmed", None,
    )
    # Source row preserved; fingerprint NULL (UNKNOWN backfill).
    assert sources == [(1, "page", 1, None, 1)]
    assert relations == [(1, 2, "supports")]
    # User memory row keeps its id; knowledge_change rows moved out.
    assert memories == [(4, "experience", "active")]
    # 2 legacy events + 1 baseline for object 1; baselines for 2 and 3; orphan.
    assert [row[:4] for row in revisions] == [
        (1, "事实A", 1, "legacy_event"),
        (1, "事实A", 2, "legacy_event"),
        (1, "事实A", 3, "legacy_baseline"),
        (2, "经验B", 1, "legacy_baseline"),
        (3, "概念C", 1, "legacy_baseline"),
        (None, "孤儿对象", 0, "legacy_event"),
    ]


def test_fresh_database_has_v10_structure_and_single_uuid(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    assert database.SCHEMA_VERSION == 11
    with sqlite3.connect(database.database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert _table_columns(connection, "knowledge_objects") == KNOWLEDGE_OBJECT_COLUMNS
        assert (
            _table_columns(connection, "knowledge_object_revisions")
            == KNOWLEDGE_REVISION_COLUMNS
        )
        meta_rows = connection.execute(
            "SELECT COUNT(*) FROM knowledge_base_meta"
        ).fetchone()[0]
        uuid_row = connection.execute(
            "SELECT kb_uuid FROM knowledge_base_meta WHERE id = 1"
        ).fetchone()
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert meta_rows == 1
    assert len(uuid_row[0]) == 36
    assert database.get_knowledge_base_uuid() == uuid_row[0]


def test_remigration_is_noop_and_uuid_stable(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v9_database(database_path)

    first = Database(database_path)
    first_uuid = first.get_knowledge_base_uuid()
    second = Database(database_path)

    assert second.get_knowledge_base_uuid() == first_uuid
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
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert object_count == 3
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_knowledge_object_check_constraints_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        def _row(**overrides: str) -> tuple[str, ...]:
            base = {
                "kind": "concept",
                "authorship": "user",
                "epistemic_basis": "unknown_legacy",
                "title": "标题",
                "content": "内容",
                "importance": "normal",
                "lifecycle": "active",
                "confirmation_status": "unconfirmed",
            }
            base.update(overrides)
            return tuple(base.values())

        bad_rows = (
            _row(kind="audio"),
            _row(authorship="robot"),
            _row(epistemic_basis="telepathy"),
            _row(title="   "),
            _row(content=""),
            _row(importance="urgent"),
            _row(lifecycle="published"),
            _row(confirmation_status="verified"),
        )
        for row in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO knowledge_objects(kind, authorship, epistemic_basis,"
                    " title, content, importance, lifecycle, confirmation_status,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*row, TS, TS),
                )
            connection.rollback()
        # active 不得携带 successor
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_objects(kind, authorship, epistemic_basis,"
                " title, content, importance, lifecycle, superseded_by_ko_id,"
                " confirmation_status, created_at, updated_at)"
                " VALUES ('concept','user','unknown_legacy','标题','内容','normal',"
                "'active', 1, 'unconfirmed', ?, ?)",
                (TS, TS),
            )
        connection.rollback()
        # confirmed 必须携带 confirmed_at 与 confirmed_revision
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_objects(kind, authorship, epistemic_basis,"
                " title, content, importance, lifecycle, confirmation_status,"
                " created_at, updated_at)"
                " VALUES ('concept','user','unknown_legacy','标题','内容','normal',"
                "'active', 'confirmed', ?, ?)",
                (TS, TS),
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


def test_knowledge_object_delete_cascades_links_and_keeps_revisions(tmp_path: Path) -> None:
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
            "INSERT INTO knowledge_object_revisions(knowledge_object_id,"
            " object_local_id_snapshot, object_title_snapshot, object_kind_snapshot,"
            " revision_number, event_type, detail, created_at)"
            " VALUES (?, ?, '对象A', 'concept', 1, 'created', '创建', ?)",
            (first, first, TS),
        )
        connection.execute("DELETE FROM knowledge_objects WHERE id = ?", (first,))
        remaining_sources = connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_sources"
        ).fetchone()[0]
        remaining_relations = connection.execute(
            "SELECT COUNT(*) FROM knowledge_relations"
        ).fetchone()[0]
        revision_row = connection.execute(
            "SELECT knowledge_object_id, object_local_id_snapshot,"
            " object_title_snapshot FROM knowledge_object_revisions"
        ).fetchone()
    assert remaining_sources == 0
    assert remaining_relations == 0
    # Revision row is byte-level untouched by the object deletion (no FK).
    assert tuple(revision_row) == (first, first, "对象A")


def test_knowledge_memory_check_constraints_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # 非法 kind（knowledge_change 已移除）
            connection.execute(
                "INSERT INTO knowledge_memory_entries(kind, title, created_at, updated_at)"
                " VALUES ('knowledge_change', '标题', ?, ?)",
                (TS, TS),
            )
        with pytest.raises(sqlite3.IntegrityError):  # 空标题
            connection.execute(
                "INSERT INTO knowledge_memory_entries(kind, title, created_at, updated_at)"
                " VALUES ('experience', '   ', ?, ?)",
                (TS, TS),
            )
        with pytest.raises(sqlite3.IntegrityError):  # 非法 status
            connection.execute(
                "INSERT INTO knowledge_memory_entries(kind, title, status,"
                " created_at, updated_at) VALUES ('experience', '标题', 'deleted', ?, ?)",
                (TS, TS),
            )
        connection.rollback()
