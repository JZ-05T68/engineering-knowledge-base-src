"""Isolated upgrade for legacy schema-v8 database snapshots (v0.5.3 Phase 6C).

The legacy artifact is a raw SQLite snapshot (``data/database/backups/*.v8.*.db``),
not a current-format directory backup. This service upgrades it through the
project's single migration chain into either:

- a current-schema raw database snapshot + ``upgrade_report.json``, or
- (when ``assets_data_dir`` is supplied read-only) a complete, restorable
  current-format directory backup via the existing ``BackupService``.

The original legacy file is never modified, and production is never restored.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src import migrations
from src.backup_service import (
    BackupError,
    BackupService,
    read_database_summary,
    sha256_file,
    validate_backup,
)
from src.migrations import SCHEMA_VERSION

LEGACY_SCHEMA_VERSION = 8
_V8_BASELINE_TABLES = ("documents", "pages", "notes", "evidence_items", "page_embeddings")


class LegacyBackupUpgradeError(RuntimeError):
    """Raised when the isolated upgrade cannot complete safely."""


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    original_path: Path
    original_sha256: str
    staging_dir: Path
    migrated_database_path: Path
    backup_path: Path | None
    report: dict[str, object]
    steps: tuple[str, ...]


class LegacyBackupUpgradeService:
    """Run the eight-step isolated upgrade state machine."""

    def upgrade(
        self,
        legacy_db_path: Path,
        output_root: Path,
        *,
        assets_data_dir: Path | None = None,
        backup_name: str | None = None,
        dry_run: bool = False,
    ) -> UpgradeReport:
        steps: list[str] = []
        original = legacy_db_path.expanduser().resolve(strict=False)
        output = output_root.expanduser().resolve(strict=False)
        output.mkdir(parents=True, exist_ok=True)
        steps.append("Step 1：验证原始旧备份")

        if not original.is_file():
            raise LegacyBackupUpgradeError(f"旧备份文件不存在：{original}")
        original_sha256 = sha256_file(original)
        _require_sqlite_v8(original)
        if _is_within(original, Path("data").resolve(strict=False)):
            if original.name == "knowledge.db":
                raise LegacyBackupUpgradeError("拒绝把生产数据库作为旧备份输入。")
        if _is_within(output, Path("data").resolve(strict=False)):
            raise LegacyBackupUpgradeError("输出目录不得位于正式 data 目录内。")

        staging = Path(tempfile.mkdtemp(prefix="ekb-legacy-upgrade-"))
        steps.append(f"Step 2：复制到唯一 staging 目录 {staging.name}")
        legacy_copy = staging / "legacy" / original.name
        legacy_copy.parent.mkdir(parents=True)
        try:
            shutil.copyfile(original, legacy_copy)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(original) + suffix)
                if sidecar.is_file():
                    shutil.copyfile(sidecar, Path(str(legacy_copy) + suffix))
            with closing(sqlite3.connect(legacy_copy, timeout=30.0)) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            copied_sha = sha256_file(legacy_copy)
            if copied_sha == original_sha256:
                steps.append("复制前后字节哈希一致")
            else:
                steps.append(
                    "复制后经 WAL 合并，字节哈希不同；已改为逻辑等价校验"
                )
            _require_sqlite_v8(legacy_copy)
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, LegacyBackupUpgradeError):
                raise
            raise LegacyBackupUpgradeError(f"复制旧备份失败：{exc}") from exc

        migrated = staging / "database" / "knowledge.db"
        migrated.parent.mkdir(parents=True)
        shutil.copyfile(legacy_copy, migrated)
        try:
            steps.append("Step 3：验证 staging 中的 schema v8 数据")
            summary_v8 = read_database_summary(migrated)
            _require_healthy(summary_v8, LEGACY_SCHEMA_VERSION)
            baseline = _baseline_counts(migrated)

            steps.append("Step 4：在 staging 中执行既有迁移链")
            migration_backup = migrations.migrate_database(migrated)
            if migration_backup is not None:
                steps.append(f"迁移自动备份（staging 内）：{migration_backup.name}")

            steps.append("Step 5：验证迁移后的当前 schema 数据")
            summary = read_database_summary(migrated)
            _require_healthy(summary, SCHEMA_VERSION)
            post_counts = _baseline_counts(migrated)
            for table, before in baseline.items():
                if int(post_counts.get(table, 0)) != int(before):
                    raise LegacyBackupUpgradeError(
                        f"迁移后 {table} 行数变化：{before} → {post_counts.get(table, 0)}"
                    )
            if _table_count(migrated, "ai_calls") != 0:
                raise LegacyBackupUpgradeError("迁移后 ai_calls 应为空，禁止伪造历史 AI 调用。")

            if dry_run:
                report = _report(
                    steps, original, original_sha256, staging, migrated, None,
                    summary, baseline,
                )
                return UpgradeReport(
                    original_path=original,
                    original_sha256=original_sha256,
                    staging_dir=staging,
                    migrated_database_path=migrated,
                    backup_path=None,
                    report=report,
                    steps=tuple(steps),
                )

            steps.append("Step 6：从 staging 创建新的当前格式备份")
            if assets_data_dir is not None:
                backup_path = self._build_full_backup(
                    staging, migrated, output, assets_data_dir, backup_name
                )
                steps.append("Step 7：严格验证升级后的新备份")
                validation = validate_backup(
                    backup_path, expected_schema_version=SCHEMA_VERSION
                )
                if not validation.valid:
                    raise LegacyBackupUpgradeError(
                        "新备份验证失败：" + "；".join(validation.errors)
                    )
                steps.append("Step 8：隔离恢复演练")
                restore_summary = self._restore_drill(backup_path, output)
            else:
                backup_path = self._publish_raw_snapshot(
                    staging, migrated, output, backup_name
                )
                steps.append("Step 7：验证迁移后数据库（当前健康检查路径）")
                final_summary = read_database_summary(backup_path)
                _require_healthy(final_summary, SCHEMA_VERSION)
                steps.append("Step 8：隔离恢复演练（数据库直接打开验证）")
                restore_summary = self._open_drill(backup_path, output)

            report = _report(
                steps, original, original_sha256, staging, migrated, backup_path,
                summary, baseline, restore_summary=restore_summary,
            )
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, (LegacyBackupUpgradeError, BackupError, migrations.MigrationError)):
                raise LegacyBackupUpgradeError(str(exc)) from exc
            raise LegacyBackupUpgradeError(f"升级失败：{exc}") from exc
        return UpgradeReport(
            original_path=original,
            original_sha256=original_sha256,
            staging_dir=staging,
            migrated_database_path=migrated,
            backup_path=backup_path,
            report=report,
            steps=tuple(steps),
        )

    def _build_full_backup(
        self,
        staging: Path,
        migrated: Path,
        output: Path,
        assets_data_dir: Path,
        backup_name: str | None,
    ) -> Path:
        assets = assets_data_dir.expanduser().resolve(strict=False)
        if not assets.is_dir():
            raise LegacyBackupUpgradeError(f"资产源目录不存在：{assets}")
        staging_data = staging / "data"
        for kind in ("raw", "pages", "markdown"):
            source = assets / kind
            if not source.is_dir():
                raise LegacyBackupUpgradeError(f"资产源缺少 {kind} 目录：{source}")
            _copy_tree(source, staging_data / kind)
        database_dir = staging_data / "database"
        database_dir.mkdir(parents=True, exist_ok=True)
        staged_db = database_dir / "knowledge.db"
        shutil.copyfile(migrated, staged_db)
        _rebase_for_staging(staged_db, assets, staging_data)
        service = BackupService(
            app_version="0.5.2",
            data_dir=staging_data,
            raw_dir=staging_data / "raw",
            pages_dir=staging_data / "pages",
            markdown_dir=staging_data / "markdown",
            database_path=staged_db,
            backups_dir=output,
        )
        name = backup_name or f"upgraded-v8-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"
        return service.create_backup(output, backup_name=name).backup_path

    def _publish_raw_snapshot(
        self, staging: Path, migrated: Path, output: Path, backup_name: str | None
    ) -> Path:
        name = backup_name or (
            "upgraded-v8-snapshot-"
            + datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')
        )
        destination = output / name
        if destination.exists():
            raise LegacyBackupUpgradeError(f"输出目标已存在，不会覆盖：{destination}")
        staging_out = output / f".{name}.incomplete-{uuid.uuid4().hex}"
        staging_out.mkdir()
        database_dir = staging_out / "database"
        database_dir.mkdir()
        snapshot = database_dir / "knowledge.db"
        _sqlite_snapshot(migrated, snapshot)
        _write_report(staging_out, _simple_report(staging, migrated, snapshot))
        staging_out.rename(destination)
        return destination / "database" / "knowledge.db"

    def _restore_drill(self, backup_path: Path, output: Path) -> dict[str, object]:
        check_root = output / f".restore-check-{uuid.uuid4().hex}"
        check_data = check_root / "data"
        for kind in ("raw", "pages", "markdown"):
            (check_data / kind).mkdir(parents=True)
        (check_data / "database").mkdir()
        service = BackupService(
            app_version="0.5.2",
            data_dir=check_data,
            raw_dir=check_data / "raw",
            pages_dir=check_data / "pages",
            markdown_dir=check_data / "markdown",
            database_path=check_data / "database" / "knowledge.db",
            backups_dir=check_root / "backups",
        )
        try:
            result = service.restore_backup(backup_path, require_existing_target=False)
            summary = result.database_summary
            return {
                "schema_version": summary.schema_version,
                "integrity_check": summary.integrity_check,
                "foreign_key_violations": summary.foreign_key_violations,
                "documents": summary.documents,
                "pages": summary.pages,
            }
        finally:
            shutil.rmtree(check_root, ignore_errors=True)

    def _open_drill(self, snapshot_dir: Path, output: Path) -> dict[str, object]:
        check_root = output / f".open-check-{uuid.uuid4().hex}"
        check_root.mkdir()
        try:
            summary = read_database_summary(snapshot_dir)
            _require_healthy(summary, SCHEMA_VERSION)
            return {
                "schema_version": summary.schema_version,
                "integrity_check": summary.integrity_check,
                "foreign_key_violations": summary.foreign_key_violations,
            }
        finally:
            shutil.rmtree(check_root, ignore_errors=True)


def _require_sqlite_v8(path: Path) -> None:
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as connection:
            connection.execute("PRAGMA query_only = ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise LegacyBackupUpgradeError("旧备份数据库完整性检查失败。")
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            if version != LEGACY_SCHEMA_VERSION:
                raise LegacyBackupUpgradeError(
                    f"旧备份 schema v{version} 不是 v{LEGACY_SCHEMA_VERSION}，"
                    "本入口只处理 schema v8 旧备份。"
                )
    except sqlite3.Error as exc:
        raise LegacyBackupUpgradeError(f"旧备份不是可读取的 SQLite 数据库：{exc}") from exc


def _require_healthy(summary, version: int) -> None:
    if summary.integrity_check != "ok":
        raise LegacyBackupUpgradeError(f"数据库完整性检查失败：{summary.integrity_check}")
    if summary.foreign_key_violations:
        raise LegacyBackupUpgradeError("数据库存在外键违规。")
    if summary.schema_version != version:
        raise LegacyBackupUpgradeError(
            f"数据库 schema v{summary.schema_version} 与要求的 v{version} 不匹配。"
        )


def _baseline_counts(path: Path) -> dict[str, int]:
    return {table: _table_count(path, table) for table in _V8_BASELINE_TABLES}


def _table_count(path: Path, table: str) -> int:
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    try:
        uri = f"file:{source.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as src, closing(
            sqlite3.connect(destination, timeout=30.0)
        ) as dst:
            src.execute("PRAGMA query_only = ON")
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
            dst.commit()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise LegacyBackupUpgradeError(f"无法创建 SQLite 快照：{exc}") from exc
    destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
    destination.with_name(destination.name + "-shm").unlink(missing_ok=True)


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def _rebase_for_staging(db_path: Path, assets: Path, staging_data: Path) -> None:
    assets = assets.resolve(strict=False)
    staging_data = staging_data.resolve(strict=False)
    with closing(sqlite3.connect(db_path, timeout=30.0)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for row in connection.execute("SELECT id, source_path FROM documents").fetchall():
            new_path = _rebase_one(str(row[1]), assets / "raw", staging_data / "raw")
            connection.execute(
                "UPDATE documents SET source_path = ? WHERE id = ?", (new_path, row[0])
            )
        for row in connection.execute(
            "SELECT id, image_path, markdown_path FROM pages"
        ).fetchall():
            page_id, image_path, markdown_path = row
            new_image = _rebase_one(
                str(image_path), assets / "pages", staging_data / "pages"
            )
            new_markdown = None
            if markdown_path:
                new_markdown = _rebase_one(
                    str(markdown_path), assets / "markdown", staging_data / "markdown"
                )
            connection.execute(
                "UPDATE pages SET image_path = ?, markdown_path = ? WHERE id = ?",
                (new_image, new_markdown, page_id),
            )
        connection.commit()


def _rebase_one(recorded: str, source_root: Path, target_root: Path) -> str:
    value = Path(recorded)
    if not value.is_absolute():
        value = source_root.parent.parent / value
    try:
        relative = value.resolve(strict=False).relative_to(source_root.resolve(strict=False))
    except ValueError as exc:
        raise LegacyBackupUpgradeError(
            f"数据库路径超出资产源目录：{recorded}"
        ) from exc
    return str(target_root / relative)


def _report(
    steps: list[str],
    original: Path,
    original_sha256: str,
    staging: Path,
    migrated: Path,
    backup_path: Path | None,
    summary,
    baseline: dict[str, int],
    *,
    restore_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "upgrade": "schema-v8-legacy-backup-isolated-upgrade",
        "original_path": str(original),
        "original_sha256": original_sha256,
        "staging_dir": str(staging),
        "migrated_database_path": str(migrated),
        "backup_path": str(backup_path) if backup_path else None,
        "schema_version": summary.schema_version,
        "baseline_counts": baseline,
        "steps": steps,
        "restore_summary": restore_summary,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _simple_report(staging: Path, migrated: Path, snapshot: Path) -> dict[str, object]:
    return {
        "upgrade": "schema-v8-legacy-backup-isolated-upgrade",
        "migrated_database_path": str(migrated),
        "snapshot_path": str(snapshot),
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _write_report(root: Path, report: dict[str, object]) -> None:
    (root / "upgrade_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
