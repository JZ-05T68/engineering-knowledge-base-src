"""Phase 10A staging-instance isolation tests. No network, no AI calls.

Proves the local AI staging instance is fully isolated from production:
separate roots, separate SQLite databases, and no write / migration /
backup / deletion on the staging side can touch production data.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import scripts.ai_real_page_index as index_script
from src.backup_service import BackupService
from src.config import (
    OFFICIAL_PORT,
    STAGING_PORT,
    Settings,
    staging_settings,
)
from src.database import Database

PRODUCTION_LIKE_PATHS = (
    "data_dir",
    "raw_dir",
    "pages_dir",
    "markdown_dir",
    "agent_readings_dir",
    "database_dir",
    "database_path",
    "backups_dir",
    "logs_dir",
    "log_path",
    "runtime_dir",
    "pid_path",
)


def _production_settings(root: Path) -> Settings:
    """A production-shaped settings instance rooted at ``root``."""

    data_dir = root / "data"
    return Settings(
        _env_file=None,
        port=OFFICIAL_PORT,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        pages_dir=data_dir / "pages",
        markdown_dir=data_dir / "markdown",
        database_dir=data_dir / "database",
        database_path=data_dir / "database" / "knowledge.db",
        backups_dir=root / "backups",
        logs_dir=root / "logs",
        log_path=root / "logs" / "engineering-kb.log",
        runtime_dir=root / "runtime",
        pid_path=root / "runtime" / "engineering-kb.pid.json",
    )


def _production_counts(database_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as c:
        documents = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        version = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return int(documents), int(pages), int(version)


def _seed_production(root: Path) -> Settings:
    settings = _production_settings(root)
    settings.ensure_directories()
    database = Database(settings.database_path)
    document = database.create_document(
        title="生产手册",
        filename="prod.pdf",
        source_path=settings.raw_dir / "prod.pdf",
        sha256="a" * 64,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=settings.pages_dir / "page_0001.png",
        extracted_text="生产页面文本",
    )
    return settings


# ---------------------------------------------------------------- topology
def test_staging_topology_is_fully_separate(tmp_path: Path) -> None:
    production = _production_settings(tmp_path / "prod")
    staging = staging_settings(tmp_path / "staging")

    assert staging.port == STAGING_PORT == 8511
    assert production.port == OFFICIAL_PORT == 8501
    assert staging.host == production.host == "127.0.0.1"
    for field in PRODUCTION_LIKE_PATHS:
        staging_path = getattr(staging, field)
        production_path = getattr(production, field)
        assert staging_path != production_path, field
        assert tmp_path / "staging" in staging_path.parents or (
            staging_path == tmp_path / "staging"
        ), field


def test_env_path_override_cannot_break_staging_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = _production_settings(tmp_path / "prod")
    monkeypatch.setenv("EKB_DATABASE_PATH", str(production.database_path))
    monkeypatch.setenv("EKB_DATA_DIR", str(production.data_dir))
    monkeypatch.setenv("EKB_RAW_DIR", str(production.raw_dir))
    monkeypatch.setenv("EKB_BACKUPS_DIR", str(production.backups_dir))

    staging = staging_settings(tmp_path / "staging")

    for field in PRODUCTION_LIKE_PATHS:
        assert getattr(staging, field) != getattr(production, field), field
    assert staging.database_path == tmp_path / "staging" / "data" / "database" / "knowledge.db"


def test_default_staging_root_is_gitignored() -> None:
    from src.config import DEFAULT_STAGING_ROOT, PROJECT_ROOT

    assert DEFAULT_STAGING_ROOT == PROJECT_ROOT / "staging-data"
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "staging-data/" in gitignore


# ---------------------------------------------------------------- isolation
def test_staging_writes_do_not_change_production_counts(tmp_path: Path) -> None:
    production = _seed_production(tmp_path / "prod")
    before = _production_counts(production.database_path)

    staging = staging_settings(tmp_path / "staging")
    staging.ensure_directories()
    staging_db = Database(staging.database_path)
    document = staging_db.create_document(
        title="实验手册",
        filename="exp.pdf",
        source_path=staging.raw_dir / "exp.pdf",
        sha256="b" * 64,
    )
    staging_db.create_page(
        document_id=document.id,
        page_number=1,
        image_path=staging.pages_dir / "page_0001.png",
        extracted_text="staging 实验文本",
    )

    assert _production_counts(production.database_path) == before
    assert not (production.data_dir / "raw" / "exp.pdf").exists()


def test_staging_migration_does_not_touch_production(tmp_path: Path) -> None:
    production = _seed_production(tmp_path / "prod")
    before = _production_counts(production.database_path)

    staging = staging_settings(tmp_path / "staging")
    Database(staging.database_path)  # 从零建库并跑全部 migration

    assert _production_counts(production.database_path) == before
    with sqlite3.connect(
        f"file:{staging.database_path.as_posix()}?mode=ro", uri=True
    ) as connection:
        staging_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert staging_version == before[2] == 14


def test_staging_backup_stays_in_staging_backups_dir(tmp_path: Path) -> None:
    production = _seed_production(tmp_path / "prod")
    staging = staging_settings(tmp_path / "staging")
    staging.ensure_directories()
    Database(staging.database_path)
    service = BackupService(
        app_version=staging.app_version,
        data_dir=staging.data_dir,
        raw_dir=staging.raw_dir,
        pages_dir=staging.pages_dir,
        markdown_dir=staging.markdown_dir,
        database_path=staging.database_path,
        backups_dir=staging.backups_dir,
    )

    result = service.create_backup()

    assert tmp_path / "staging" in result.backup_path.parents
    assert not production.backups_dir.exists() or not any(production.backups_dir.iterdir())


def test_staging_delete_and_rebuild_leaves_production_intact(tmp_path: Path) -> None:
    production = _seed_production(tmp_path / "prod")
    before = _production_counts(production.database_path)
    staging_root = tmp_path / "staging"
    staging = staging_settings(staging_root)
    staging.ensure_directories()
    Database(staging.database_path)

    shutil.rmtree(staging_root)
    assert not staging_root.exists()
    assert _production_counts(production.database_path) == before

    # 重建 staging 同样不影响 production
    staging.ensure_directories()
    Database(staging.database_path)
    assert _production_counts(production.database_path) == before


# ------------------------------------------------------- script integration
def test_index_script_staging_uses_staging_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    production = _seed_production(tmp_path / "prod")
    staging = staging_settings(tmp_path / "staging")
    monkeypatch.setattr(index_script, "staging_settings", lambda: staging)
    monkeypatch.setattr(
        index_script,
        "get_settings",
        lambda: pytest.fail("staging 模式不得读取正式配置"),
    )

    exit_code = index_script.main(["--staging"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "instance: staging" in out
    assert "total pages: 0" in out  # staging 空库
    # production 未被触碰
    assert _production_counts(production.database_path) == (1, 1, 14)


def test_index_script_default_uses_production_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    production = _seed_production(tmp_path / "prod")
    monkeypatch.setattr(index_script, "get_settings", lambda: production)
    monkeypatch.setattr(
        index_script,
        "staging_settings",
        lambda: pytest.fail("正式模式不得读取 staging 配置"),
    )

    exit_code = index_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "instance: production" in out
    assert "total pages: 1" in out
