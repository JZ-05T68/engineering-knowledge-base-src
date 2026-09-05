"""Tests for schema v13: raw Q&A identity, tombstone and citation snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import src.migrations as migrations_module
from src.database import Database
from src.migrations import (
    SCHEMA_VERSION,
    MigrationError,
    _read_schema_version,
    migrate_database,
)
from src.models import parse_memory_citations


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _build_v12_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a database that stops at schema v12 via the v13 failure hook."""

    database_path = tmp_path / "knowledge.db"
    monkeypatch.setattr(
        migrations_module, "_V13_INJECTION_POINT", "v13_create_table"
    )
    with pytest.raises(MigrationError, match="v13 迁移失败注入点"):
        migrate_database(database_path)
    monkeypatch.setattr(migrations_module, "_V13_INJECTION_POINT", None)
    assert _read_schema_version(database_path) == 12
    return database_path


def _seed_legacy_memory_rows(database_path: Path) -> dict[str, int]:
    """Seed v12-shaped rows spanning every classification outcome."""

    timestamp = "2026-09-04T09:35:25.937834+00:00"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
        " created_at, updated_at) VALUES ('手册一', 'a.pdf', 'raw/a.pdf', ?, 1, ?, ?)",
        ("a" * 64, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
        " created_at, updated_at) VALUES ('手册二', 'b.pdf', 'raw/b.pdf', ?, 1, ?, ?)",
        ("b" * 64, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO pages(document_id, page_number, image_path, status,"
        " review_status, created_at, updated_at)"
        " VALUES (1, 1, 'pages/1.png', 'text_extracted', 'reviewed', ?, ?)",
        (timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO pages(document_id, page_number, image_path, status,"
        " review_status, created_at, updated_at)"
        " VALUES (2, 1, 'pages/2.png', 'text_extracted', 'reviewed', ?, ?)",
        (timestamp, timestamp),
    )
    raw_content_1 = "问题：唯一验证标记是什么？\n\nAgent 回答：\n标记是 A-1。"
    raw_content_2 = "问题：额定转速是多少？\n\nAgent 回答：\n两份资料分别为 1379 与 1426 rpm。"
    raw_content_3 = "问题：维护周期？\n\nAgent 回答：\n每 500 小时检查。"
    rows = {
        "signature_single": (
            "experience",
            raw_content_1,
            "active",
            1,
            1,
        ),
        "signature_multi": (
            "experience",
            raw_content_2,
            "active",
            2,
            2,
        ),
        "signature_archived": (
            "experience",
            raw_content_3,
            "archived",
            1,
            1,
        ),
        "authored_experience": (
            "experience",
            "我在项目中总结出的调试经验，不能确认创建路径。",
            "active",
            None,
            None,
        ),
        "authored_problem_solving": (
            "problem_solving",
            "PWM 修改无效的问题排查记录。",
            "active",
            None,
            None,
        ),
    }
    ids: dict[str, int] = {}
    from src.database import _tokenize_for_fts

    for name, (kind, content, status, document_id, page_id) in rows.items():
        title = f"标题-{name}"
        cursor = connection.execute(
            "INSERT INTO knowledge_memory_entries("
            " kind, title, content, status, created_at, updated_at,"
            " document_id, page_id,"
            " search_title, search_content, search_root_cause, search_lesson)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')",
            (
                kind,
                title,
                content,
                status,
                timestamp,
                timestamp,
                document_id,
                page_id,
                _tokenize_for_fts(title),
                _tokenize_for_fts(content),
            ),
        )
        ids[name] = int(cursor.lastrowid)
    connection.commit()
    connection.close()
    return ids


def test_fresh_database_migrates_to_v13(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    assert SCHEMA_VERSION == 13
    assert database.SCHEMA_VERSION == 13
    with sqlite3.connect(database.database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == list(range(1, 14))
        columns = _table_columns(connection, "knowledge_memory_entries")
        assert {
            "creation_origin",
            "citation_snapshot",
            "content_fingerprint",
            "source_entry_id",
            "source_title",
            "root_cause_confirmed",
        } <= columns
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND tbl_name='knowledge_memory_entries'"
            )
        }
        assert {
            "knowledge_memory_fts_insert",
            "knowledge_memory_fts_delete",
            "knowledge_memory_fts_update",
        } <= triggers
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v12_legacy_rows_are_classified_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _build_v12_database(tmp_path, monkeypatch)
    ids = _seed_legacy_memory_rows(database_path)

    backup_path = migrate_database(database_path)

    assert backup_path is not None and backup_path.exists()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    rows = {
        int(row["id"]): row
        for row in connection.execute(
            "SELECT * FROM knowledge_memory_entries ORDER BY id"
        ).fetchall()
    }
    # Exact save-button signature -> raw_qa + human_saved, verbatim content.
    single = rows[ids["signature_single"]]
    assert single["kind"] == "raw_qa"
    assert single["creation_origin"] == "human_saved"
    canonical = str(single["content"]).replace("\r\n", "\n").strip()
    assert single["content_fingerprint"] == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    citations = parse_memory_citations(str(single["citation_snapshot"]))
    assert len(citations) == 1
    assert citations[0].document_title == "手册一"
    assert citations[0].page_number == 1
    assert citations[0].document_sha256 == "a" * 64
    # Multi-document content keeps its own page link in the snapshot.
    multi = rows[ids["signature_multi"]]
    assert multi["kind"] == "raw_qa"
    multi_citations = parse_memory_citations(str(multi["citation_snapshot"]))
    assert multi_citations[0].document_title == "手册二"
    # Archived rows keep their status through the tombstone rebuild.
    archived = rows[ids["signature_archived"]]
    assert archived["kind"] == "raw_qa"
    assert archived["status"] == "archived"
    # Non-signature and authored rows are never guessed into raw_qa.
    authored = rows[ids["authored_experience"]]
    assert authored["kind"] == "experience"
    assert authored["creation_origin"] is None
    problem_solving = rows[ids["authored_problem_solving"]]
    assert problem_solving["kind"] == "problem_solving"
    assert problem_solving["creation_origin"] is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_v13_migration_failure_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _build_v12_database(tmp_path, monkeypatch)
    ids = _seed_legacy_memory_rows(database_path)

    monkeypatch.setattr(
        migrations_module, "_V13_INJECTION_POINT", "v13_memory_backfill"
    )
    with pytest.raises(MigrationError, match="v13 迁移失败注入点"):
        migrate_database(database_path)
    monkeypatch.setattr(migrations_module, "_V13_INJECTION_POINT", None)

    assert _read_schema_version(database_path) == 12
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM knowledge_memory_entries WHERE id = ?", (ids["signature_single"],)
    ).fetchone()
    assert row["kind"] == "experience"
    assert "creation_origin" not in _table_columns(
        connection, "knowledge_memory_entries"
    )
    staging = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='knowledge_memory_entries_v13'"
    ).fetchone()
    assert staging is None
    connection.close()

    backup_path = migrate_database(database_path)
    assert backup_path is not None
    assert _read_schema_version(database_path) == 13


def test_v13_check_constraints_and_fts_survive_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _build_v12_database(tmp_path, monkeypatch)
    _seed_legacy_memory_rows(database_path)
    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    # raw_qa is a legal kind; deleted is a legal status.
    connection.execute(
        "INSERT INTO knowledge_memory_entries(kind, title, content, status,"
        " created_at, updated_at) VALUES ('raw_qa', ' tombstone ', 'x', 'deleted', 't', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content, status,"
            " created_at, updated_at) VALUES ('raw_qa2', 'bad', 'x', 'active', 't', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content, status,"
            " created_at, updated_at) VALUES ('raw_qa', 'bad', 'x', 'removed', 't', 't')"
        )
    # The rebuilt FTS index still matches the memory content.
    hits = connection.execute(
        "SELECT me.kind FROM knowledge_memory_search f"
        " JOIN knowledge_memory_entries me ON me.id = f.rowid"
        " WHERE knowledge_memory_search MATCH '\"验证\"'"
    ).fetchall()
    assert {row["kind"] for row in hits} == {"raw_qa"}
    connection.rollback()
    connection.close()


def test_v13_database_supports_sqlite_backup_round_trip(tmp_path: Path) -> None:
    from src.migrations import backup_database

    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    backup_path = backup_database(database_path, 13)

    connection = sqlite3.connect(backup_path)
    connection.row_factory = sqlite3.Row
    assert _read_schema_version(backup_path) == 13
    columns = _table_columns(connection, "knowledge_memory_entries")
    assert "creation_origin" in columns and "source_entry_id" in columns
    connection.close()
