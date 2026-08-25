"""Schema 8 (page_embeddings) migration and constraint tests.

Covers the v0.5.0 Phase 7 additive migration: a fresh ``page_embeddings``
table keyed by ``(page_id, model, dimensions, config_version)`` with a real
``ON DELETE CASCADE`` foreign key to ``pages(id)``. Existing v7 data must be
preserved exactly. All databases are temporary fixtures.
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

EXPECTED_COLUMNS = {
    "id",
    "page_id",
    "source_text_sha256",
    "model",
    "dimensions",
    "config_version",
    "vector",
    "created_at",
    "updated_at",
}


def _create_v7_database(database_path: Path) -> None:
    """Build a representative schema v7 database with legacy rows."""

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


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _unique_index_shapes(connection: sqlite3.Connection) -> set[tuple[str, ...]]:
    shapes: set[tuple[str, ...]] = set()
    for index in connection.execute("PRAGMA index_list(page_embeddings)").fetchall():
        if not int(index[2]):
            continue
        shapes.add(
            tuple(
                column[2]
                for column in connection.execute(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            )
        )
    return shapes


def test_migrate_v7_to_v8_preserves_data_and_adds_table(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)

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
        assert _table_columns(connection, "page_embeddings") == EXPECTED_COLUMNS
        foreign_keys = [
            (row[2], row[3], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(page_embeddings)")
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert version == SCHEMA_VERSION == 11
    assert documents == [("液压手册", "hyd.pdf", HASH_A)]
    assert pages == [(1, 1, "reviewed"), (2, 2, "pending")]
    assert notes == [("page", 1, "第一页笔记")]
    assert evidence == [
        ("text_selection", "unconfirmed", "液压泵需要定期检查")
    ]
    assert ("pages", "page_id", "id", "CASCADE") in foreign_keys
    with sqlite3.connect(database_path) as connection:
        assert ("page_id", "model", "dimensions", "config_version") in (
            _unique_index_shapes(connection)
        )


def test_fresh_database_has_v8_structure(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    assert database.SCHEMA_VERSION == 11
    with sqlite3.connect(database.database_path) as connection:
        assert _table_columns(connection, "page_embeddings") == EXPECTED_COLUMNS
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)

    Database(database_path)
    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        embedding_count = connection.execute(
            "SELECT COUNT(*) FROM page_embeddings"
        ).fetchone()[0]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert embedding_count == 0
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_text_sha256", "short"),
        ("model", "   "),
        ("dimensions", 0),
        ("config_version", -1),
    ],
)
def test_page_embeddings_check_constraints_enforced(
    tmp_path: Path, column: str, value: object
) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)
    migrate_database(database_path)
    row = {
        "page_id": 1,
        "source_text_sha256": HASH_B,
        "model": "fake-emb",
        "dimensions": 4,
        "config_version": 1,
        "vector": sqlite3.Binary(b"\x01" + b"\x00" * 16),
        "created_at": TS,
        "updated_at": TS,
    }
    row[column] = value

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO page_embeddings(page_id, source_text_sha256, model,"
                " dimensions, config_version, vector, created_at, updated_at)"
                " VALUES (:page_id, :source_text_sha256, :model, :dimensions,"
                " :config_version, :vector, :created_at, :updated_at)",
                row,
            )


def test_page_embeddings_vector_length_check_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO page_embeddings(page_id, source_text_sha256, model,"
                " dimensions, config_version, vector, created_at, updated_at)"
                " VALUES (1, ?, 'fake-emb', 4, 1, ?, ?, ?)",
                (HASH_B, sqlite3.Binary(b"\x01" + b"\x00" * 8), TS, TS),
            )


def test_page_embeddings_foreign_key_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO page_embeddings(page_id, source_text_sha256, model,"
                " dimensions, config_version, vector, created_at, updated_at)"
                " VALUES (999, ?, 'fake-emb', 4, 1, ?, ?, ?)",
                (HASH_B, sqlite3.Binary(b"\x01" + b"\x00" * 16), TS, TS),
            )


def test_page_embeddings_unique_configuration_enforced(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v7_database(database_path)
    migrate_database(database_path)
    blob = sqlite3.Binary(b"\x01" + b"\x00" * 16)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO page_embeddings(page_id, source_text_sha256, model,"
            " dimensions, config_version, vector, created_at, updated_at)"
            " VALUES (1, ?, 'fake-emb', 4, 1, ?, ?, ?)",
            (HASH_B, blob, TS, TS),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO page_embeddings(page_id, source_text_sha256, model,"
                " dimensions, config_version, vector, created_at, updated_at)"
                " VALUES (1, ?, 'fake-emb', 4, 1, ?, ?, ?)",
                (HASH_C, blob, TS, TS),
            )
