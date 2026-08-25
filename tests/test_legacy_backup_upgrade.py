"""Tests for the isolated schema-v8 legacy backup upgrade (Phase 6C)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src import migrations
from src.backup_service import read_database_summary, sha256_file
from src.legacy_backup_upgrade_service import (
    LegacyBackupUpgradeError,
    LegacyBackupUpgradeService,
)
from src.migrations import SCHEMA_VERSION, migrate_database


def _make_v8_database(tmp_path: Path, assets: Path) -> Path:
    db_path = tmp_path / "v8" / "knowledge.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    current = int(
        connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    )
    if current < 1:
        migrations._apply_version_one(connection)
    if current < 2:
        migrations._apply_version_two(connection)
    if current < 3:
        migrations._apply_version_three(connection)
    if current < 4:
        migrations._apply_version_four(connection)
    if current < 5:
        migrations._apply_version_five(connection)
    if current < 6:
        migrations._apply_version_six(connection)
    if current < 7:
        migrations._apply_version_seven(connection)
    if current < 8:
        migrations._apply_version_eight(connection)
    connection.commit()
    connection.close()
    (assets / "raw").mkdir(parents=True)
    (assets / "pages" / "1").mkdir(parents=True)
    (assets / "markdown").mkdir(parents=True)
    (assets / "raw" / "manual.pdf").write_bytes(b"%PDF-1.4")
    (assets / "pages" / "1" / "page_0001.png").write_bytes(b"\x89PNG")
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO documents(title, filename, source_path, sha256,"
            " page_count, created_at, updated_at)"
            " VALUES ('手册','manual.pdf','data/raw/manual.pdf',?,1,"
            "'2026-08-25T09:00:00','2026-08-25T09:00:00')",
            (sha256_file(assets / "raw" / "manual.pdf"),),
        )
        connection.execute(
            "INSERT INTO pages(document_id, page_number, image_path,"
            " extracted_text, ocr_text, markdown_content, markdown_path,"
            " status, review_status, processing_error, search_extracted_text,"
            " search_ocr_text, search_markdown_content, created_at, updated_at)"
            " VALUES (1,1,'data/pages/1/page_0001.png','','','','',"
            " 'text_extracted','pending','','','','','2026-08-25T09:00:00',"
            "'2026-08-25T09:00:00')"
        )
        connection.commit()
    return db_path


def test_full_upgrade_path_and_original_unchanged(tmp_path: Path) -> None:
    assets = tmp_path / "data"
    legacy = _make_v8_database(tmp_path, assets)
    original_sha = sha256_file(legacy)
    output = tmp_path / "out"

    report = LegacyBackupUpgradeService().upgrade(
        legacy, output, assets_data_dir=assets
    )

    assert sha256_file(legacy) == original_sha
    assert report.backup_path is not None
    assert report.report["schema_version"] == SCHEMA_VERSION
    assert report.report["restore_summary"]["schema_version"] == SCHEMA_VERSION
    validation = __import__("src.backup_service", fromlist=["validate_backup"]).validate_backup(
        report.backup_path, expected_schema_version=SCHEMA_VERSION
    )
    assert validation.valid


def test_dry_run_migrates_without_final_backup(tmp_path: Path) -> None:
    assets = tmp_path / "data"
    legacy = _make_v8_database(tmp_path, assets)
    original_sha = sha256_file(legacy)

    report = LegacyBackupUpgradeService().upgrade(
        legacy, tmp_path / "out", assets_data_dir=assets, dry_run=True
    )

    assert sha256_file(legacy) == original_sha
    assert report.backup_path is None
    assert report.report["schema_version"] == SCHEMA_VERSION


def test_raw_snapshot_output_without_assets(tmp_path: Path) -> None:
    assets = tmp_path / "data"
    legacy = _make_v8_database(tmp_path, assets)
    original_sha = sha256_file(legacy)

    report = LegacyBackupUpgradeService().upgrade(legacy, tmp_path / "out")

    assert sha256_file(legacy) == original_sha
    assert report.backup_path is not None
    summary = read_database_summary(report.backup_path)
    assert summary.schema_version == SCHEMA_VERSION
    assert summary.integrity_check == "ok"


def test_rejects_non_v8_database(tmp_path: Path) -> None:
    v12 = tmp_path / "v12.db"
    migrate_database(v12)

    with pytest.raises(LegacyBackupUpgradeError, match="不是 v8"):
        LegacyBackupUpgradeService().upgrade(v12, tmp_path / "out")


def test_rejects_corrupt_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")

    with pytest.raises(LegacyBackupUpgradeError, match="SQLite"):
        LegacyBackupUpgradeService().upgrade(corrupt, tmp_path / "out")


def test_failure_injection_leaves_original_unchanged_and_no_output(
    tmp_path: Path, monkeypatch
) -> None:
    assets = tmp_path / "data"
    legacy = _make_v8_database(tmp_path, assets)
    original_sha = sha256_file(legacy)
    monkeypatch.setattr(migrations, "_V12_INJECTION_POINT", "v12_ai_calls")
    output = tmp_path / "out"

    with pytest.raises(LegacyBackupUpgradeError):
        LegacyBackupUpgradeService().upgrade(legacy, output, assets_data_dir=assets)

    assert sha256_file(legacy) == original_sha
    assert not any(output.iterdir()) if output.exists() else True


def test_normal_restore_validation_still_rejects_raw_v8(tmp_path: Path) -> None:
    assets = tmp_path / "data"
    legacy = _make_v8_database(tmp_path, assets)

    validation = __import__(
        "src.backup_service", fromlist=["validate_backup"]
    ).validate_backup(legacy, expected_schema_version=SCHEMA_VERSION)

    assert not validation.valid
    assert any("备份目录不存在" in error for error in validation.errors)
