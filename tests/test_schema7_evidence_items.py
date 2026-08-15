"""Schema 7 (typed evidence items) migration and constraint tests.

Covers the v0.4.0 slice 1-3 design: the rebuilt ``evidence_items`` table with
``evidence_type`` (page / text_selection / image_region), image-region anchor
columns (same CHECK semantics as the notes table) and the manual confirmation
pair. Legacy rows must be preserved exactly and map to
``text_selection`` / ``unconfirmed``. All databases are temporary fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import migrations as migrations_module
from src.database import Database
from src.migrations import SCHEMA_VERSION, MigrationError

TS = "2026-08-01T00:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64

# v4 形态的 19 列（迁移前旧行只有这些列，新增列全部由迁移映射填充）
EVIDENCE_COLUMNS_V6 = (
    "basket_id", "document_id", "page_id", "document_title", "filename",
    "page_number", "review_status", "projects_json", "tags_json",
    "evidence_text", "text_kind", "context", "context_kind", "user_note",
    "source_text_sha256", "source_locator", "selection_sha256",
    "added_at", "position",
)

LEGACY_ROWS: tuple[tuple, ...] = (
    (
        1, 1, 1, "液压手册", "hyd.pdf",
        1, "reviewed", '["泵站"]', '["维护"]',
        "液压泵需要定期检查", "original_material", "泵的上下文", "system_generated",
        "现场备注", HASH_B, "document_id=1; page_id=1", HASH_C, TS, 1,
    ),
    (
        1, 1, 2, "液压手册", "hyd.pdf",
        2, "pending", "[]", "[]",
        "用户摘录的一段文字", "user_excerpt", "", "system_generated",
        "", HASH_B, "document_id=1; page_id=2", HASH_D, TS, 2,
    ),
)

NEW_COLUMNS_V7 = (
    "evidence_type", "region_image_sha256", "region_image_width",
    "region_image_height", "region_x0", "region_y0", "region_x1", "region_y1",
    "confirmation_status", "confirmed_at",
)


def _create_v6_database(database_path: Path) -> None:
    """Build a representative schema v6 database with legacy evidence rows."""

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
        placeholders = ", ".join("?" for _ in EVIDENCE_COLUMNS_V6)
        for row in LEGACY_ROWS:
            connection.execute(
                f"INSERT INTO evidence_items({', '.join(EVIDENCE_COLUMNS_V6)})"
                f" VALUES ({placeholders})",
                row,
            )
        connection.commit()
        migrations_module._apply_version_five(connection)
        migrations_module._apply_version_six(connection)


def _legacy_fingerprint(connection: sqlite3.Connection) -> tuple:
    return tuple(
        connection.execute(
            f"SELECT {', '.join(EVIDENCE_COLUMNS_V6)} FROM evidence_items ORDER BY id"
        ).fetchall()
    )


def _seed_v7_library(database_path: Path) -> None:
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
        connection.execute(
            "INSERT INTO evidence_baskets(name, created_at, updated_at)"
            " VALUES ('篮', ?, ?)",
            (TS, TS),
        )
        connection.commit()


def _valid_v7_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "basket_id": 1,
        "document_id": 1,
        "page_id": 1,
        "document_title": "手册",
        "filename": "a.pdf",
        "page_number": 1,
        "review_status": "pending",
        "evidence_type": "text_selection",
        "evidence_text": "选区文字",
        "text_kind": "original_material",
        "context_kind": "system_generated",
        "source_text_sha256": HASH_A,
        "source_locator": "document_id=1; page_id=1",
        "selection_sha256": HASH_B,
        "added_at": TS,
        "position": 1,
    }
    row.update(overrides)
    return row


def _insert_row(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    connection.execute(
        f"INSERT INTO evidence_items({columns}) VALUES ({placeholders})", row
    )


# --- A. migration v6 → v7 ---------------------------------------------------------


def test_v6_to_v7_upgrade_preserves_rows_and_maps_legacy_values(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v6_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _legacy_fingerprint(connection)

    Database(database_path)

    with sqlite3.connect(database_path) as connection:
        after = _legacy_fingerprint(connection)
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        mapped = connection.execute(
            f"SELECT {', '.join(NEW_COLUMNS_V7)} FROM evidence_items ORDER BY id"
        ).fetchall()
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'evidence_items'"
            )
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert before == after  # 19 个旧列逐行原样保留
    assert version == SCHEMA_VERSION == 8
    assert mapped == [
        ("text_selection", None, None, None, None, None, None, None, "unconfirmed", None),
        ("text_selection", None, None, None, None, None, None, None, "unconfirmed", None),
    ]
    assert {"idx_evidence_items_page", "idx_evidence_items_document"} <= indexes


def test_v7_migration_keeps_pre_upgrade_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v6_database(database_path)
    database = Database(database_path)
    assert database.last_backup_path is not None
    assert database.last_backup_path.is_file()


def test_remigration_is_noop(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _create_v6_database(database_path)
    Database(database_path)
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        item_count = connection.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]
    assert item_count == len(LEGACY_ROWS)
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_failed_v7_migration_rolls_back_and_keeps_v6_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指纹校验在全部 DDL 之后——事务中途失败必须整体回滚到 v6 形态。"""
    database_path = tmp_path / "knowledge.db"
    _create_v6_database(database_path)
    with sqlite3.connect(database_path) as connection:
        before = _legacy_fingerprint(connection)
    original_fingerprint = migrations_module._core_data_fingerprint
    calls = 0

    def changed_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        result = original_fingerprint(connection)
        return result if calls == 1 else (*result, "simulated change")

    monkeypatch.setattr(migrations_module, "_core_data_fingerprint", changed_fingerprint)

    with pytest.raises(MigrationError, match="schema v7"):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evidence_items)")}
        after = _legacy_fingerprint(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert version == 6
    assert "evidence_type" not in columns  # 无半升级列状态
    assert before == after


# --- B. DB-level constraints --------------------------------------------------------


def test_invalid_type_region_and_confirmation_combinations_rejected(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    _seed_v7_library(database_path)
    bad_rows = (
        _valid_v7_row(evidence_type="audio"),
        _valid_v7_row(evidence_text="   "),  # text_selection 必须非空
        _valid_v7_row(region_image_sha256=HASH_C),  # 文字选区不得带区域列
        _valid_v7_row(evidence_type="image_region", evidence_text=""),  # 缺区域列
        _valid_v7_row(  # 区域坐标越界
            evidence_type="image_region",
            evidence_text="",
            region_image_sha256=HASH_C,
            region_image_width=100,
            region_image_height=200,
            region_x0=0,
            region_y0=0,
            region_x1=101,
            region_y1=100,
        ),
        _valid_v7_row(confirmation_status="confirmed"),  # 缺 confirmed_at
        _valid_v7_row(confirmed_at=TS),  # unconfirmed 不得带 confirmed_at
        _valid_v7_row(confirmation_status="maybe"),
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for row in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_row(connection, row)
            connection.rollback()


def test_page_and_region_rows_accepted_with_type_semantics(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    Database(database_path)
    _seed_v7_library(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_row(  # 整页证据允许空 evidence_text
            connection,
            _valid_v7_row(evidence_type="page", evidence_text="", position=1),
        )
        _insert_row(  # 合法区域证据
            connection,
            _valid_v7_row(
                evidence_type="image_region",
                evidence_text="",
                region_image_sha256=HASH_C,
                region_image_width=100,
                region_image_height=200,
                region_x0=10,
                region_y0=20,
                region_x1=60,
                region_y1=120,
                selection_sha256=HASH_D,
                position=2,
            ),
        )
        _insert_row(  # 确认状态与时间戳成对出现
            connection,
            _valid_v7_row(
                confirmation_status="confirmed",
                confirmed_at=TS,
                selection_sha256="e" * 64,
                position=3,
            ),
        )
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0]
    assert count == 3
