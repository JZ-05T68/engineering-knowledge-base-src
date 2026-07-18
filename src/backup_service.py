"""Verified local backups and guarded offline restoration for user data."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from src.migrations import SCHEMA_VERSION

LOGGER = logging.getLogger(__name__)

BACKUP_FORMAT: Final[str] = "engineering-knowledge-base-directory"
BACKUP_FORMAT_VERSION: Final[int] = 1
CONFIG_FORMAT_VERSION: Final[int] = 1
MANIFEST_NAME: Final[str] = "manifest.json"
CONFIG_RELATIVE_PATH: Final[str] = "config/settings.json"
DATABASE_KIND: Final[str] = "database"
DATA_KINDS: Final[tuple[str, ...]] = ("raw", "pages", "markdown")
FILE_KINDS: Final[set[str]] = {DATABASE_KIND, *DATA_KINDS, "config"}
_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024
_MANIFEST_KEYS: Final[set[str]] = {
    "backup_format",
    "backup_format_version",
    "application_version",
    "schema_version",
    "created_at",
    "complete",
    "statistics",
    "database",
    "directories",
    "key_paths",
    "files",
}
_STATISTIC_KEYS: Final[set[str]] = {
    "documents",
    "pages",
    "fts",
    "evidence",
    "projects",
    "tags",
}
_KEY_PATH_KEYS: Final[set[str]] = {
    "data",
    "raw",
    "pages",
    "markdown",
    "database",
    "config",
}


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSummary:
    """Read-only database integrity and count summary."""

    schema_version: int
    integrity_check: str
    foreign_key_violations: int
    documents: int
    pages: int
    fts: int
    evidence: int
    projects: int
    tags: int

    @property
    def statistics(self) -> dict[str, int]:
        """Return the manifest-compatible count mapping."""

        return {
            "documents": self.documents,
            "pages": self.pages,
            "fts": self.fts,
            "evidence": self.evidence,
            "projects": self.projects,
            "tags": self.tags,
        }


@dataclass(frozen=True, slots=True)
class BackupValidation:
    """Complete validation result for one candidate backup directory."""

    backup_path: Path
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: Mapping[str, object] | None
    database_summary: DatabaseSummary | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Successfully finalized backup and measured timings."""

    backup_path: Path
    manifest: Mapping[str, object]
    creation_seconds: float
    verification_seconds: float


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Successfully restored data and retained its pre-restore backup."""

    data_dir: Path
    pre_restore_backup: Path | None
    validation_seconds: float
    restore_seconds: float
    post_validation_seconds: float
    database_summary: DatabaseSummary


class BackupService:
    """Create, validate, and restore complete local user-data backups."""

    def __init__(
        self,
        *,
        app_version: str,
        data_dir: Path,
        raw_dir: Path,
        pages_dir: Path,
        markdown_dir: Path,
        database_path: Path,
        backups_dir: Path,
        host: str = "127.0.0.1",
        port: int = 8501,
        minimum_text_length: int = 20,
        pdf_render_dpi: int = 150,
    ) -> None:
        self.app_version = _normalized_version(app_version)
        self.data_dir = _resolved(data_dir)
        self.raw_dir = _resolved(raw_dir)
        self.pages_dir = _resolved(pages_dir)
        self.markdown_dir = _resolved(markdown_dir)
        self.database_path = _resolved(database_path)
        self.backups_dir = _resolved(backups_dir)
        self.host = host
        self.port = int(port)
        self.minimum_text_length = int(minimum_text_length)
        self.pdf_render_dpi = int(pdf_render_dpi)
        self._layout = self._validate_layout()

    def create_backup(
        self,
        target_root: Path | None = None,
        *,
        backup_name: str | None = None,
    ) -> BackupResult:
        """Create and fully verify a directory backup before atomic finalization."""

        started = time.perf_counter()
        root = _resolved(target_root or self.backups_dir)
        _reject_symlink(root, "备份目标目录")
        if _is_within(root, self.data_dir) or _is_within(self.data_dir, root):
            raise BackupError("备份目标必须位于正式 data 目录之外。")
        root.mkdir(parents=True, exist_ok=True)
        name = backup_name or _default_backup_name(self.app_version)
        _validate_backup_name(name)
        destination = root / name
        if destination.exists():
            raise BackupError(f"备份目标已存在，不会覆盖：{destination}")
        staging = root / f".{name}.incomplete-{uuid.uuid4().hex}"
        if staging.exists():
            raise BackupError(f"临时备份目录已存在：{staging}")

        try:
            staging.mkdir(parents=False)
            database_relative = f"data/{self._layout['database']}"
            snapshot_path = _safe_destination(staging, database_relative)
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_snapshot(self.database_path, snapshot_path)
            database_summary = read_database_summary(snapshot_path)
            _require_healthy_database(database_summary, SCHEMA_VERSION)

            records: list[dict[str, object]] = []
            records.append(_file_record(snapshot_path, staging, DATABASE_KIND))
            for kind, source_root in (
                ("raw", self.raw_dir),
                ("pages", self.pages_dir),
                ("markdown", self.markdown_dir),
            ):
                target_prefix = f"data/{self._layout[kind]}"
                _safe_destination(staging, target_prefix).mkdir(parents=True, exist_ok=True)
                for source_file, relative in _walk_regular_files(source_root):
                    target = _safe_destination(staging, f"{target_prefix}/{relative.as_posix()}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_regular_file(source_file, target)
                    records.append(_file_record(target, staging, kind))

            config_path = _safe_destination(staging, CONFIG_RELATIVE_PATH)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config = self._backup_config()
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            records.append(_file_record(config_path, staging, "config"))
            records.sort(key=lambda item: str(item["path"]))

            manifest = self._manifest(database_summary, records)
            manifest_path = staging / MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            verification_started = time.perf_counter()
            validation = validate_backup(
                staging,
                expected_app_version=self.app_version,
                expected_schema_version=SCHEMA_VERSION,
            )
            verification_seconds = time.perf_counter() - verification_started
            if not validation.valid:
                raise BackupError("备份验证失败：" + "；".join(validation.errors))
            os.replace(staging, destination)
        except Exception as exc:
            _cleanup_incomplete(staging, exc)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"创建完整备份失败：{exc}") from exc

        creation_seconds = time.perf_counter() - started
        LOGGER.info(
            "完整备份成功：path=%s files=%s bytes=%s",
            destination,
            len(manifest["files"]),
            sum(int(item["size"]) for item in manifest["files"]),
        )
        return BackupResult(
            backup_path=destination,
            manifest=manifest,
            creation_seconds=creation_seconds,
            verification_seconds=verification_seconds,
        )

    def restore_backup(
        self,
        backup_path: Path,
        *,
        service_is_running: Callable[[], bool] | None = None,
        require_existing_target: bool = True,
    ) -> RestoreResult:
        """Restore a validated backup using staging, rollback, and post-checks.

        This method is intended for the independent restore script.  The caller
        must provide ``service_is_running`` for a formal restore so an open
        Streamlit database is never replaced.
        """

        validation = validate_backup(
            backup_path,
            expected_app_version=self.app_version,
            expected_schema_version=SCHEMA_VERSION,
        )
        if not validation.valid or validation.manifest is None:
            raise BackupError("拒绝恢复无效备份：" + "；".join(validation.errors))
        if service_is_running is not None and service_is_running():
            raise BackupError("正式服务仍在运行。请先停止服务，再执行恢复。")
        if require_existing_target and not self.database_path.is_file():
            raise BackupError("当前正式数据库不存在，无法创建恢复前备份，已取消恢复。")

        restore_started = time.perf_counter()
        backup_root = _resolved(backup_path)
        stage_root = self.data_dir.parent / f".ekb-restore-{uuid.uuid4().hex}"
        rollback = self.data_dir.parent / f".ekb-rollback-{uuid.uuid4().hex}"
        failed_target = self.data_dir.parent / f".ekb-failed-restore-{uuid.uuid4().hex}"
        pre_restore: Path | None = None
        stage_data = stage_root / "data"
        switched = False
        had_original = self.data_dir.exists()
        try:
            stage_data.mkdir(parents=True)
            self._stage_backup_files(backup_root, validation.manifest, stage_data)
            self._preserve_excluded_current_files(stage_data)
            staged_database = stage_data / self._layout["database"]
            _rebase_database_paths(
                staged_database,
                backup_root / CONFIG_RELATIVE_PATH,
                target_raw=self.raw_dir,
                target_pages=self.pages_dir,
                target_markdown=self.markdown_dir,
            )
            staged_summary = read_database_summary(staged_database)
            _require_healthy_database(staged_summary, SCHEMA_VERSION)
            _require_manifest_statistics(staged_summary, validation.manifest)
            self._verify_staged_assets(stage_data, backup_root, validation.manifest)

            if self.database_path.is_file():
                pre_restore = self.create_backup(
                    self.backups_dir,
                    backup_name=_default_backup_name(
                        self.app_version, prefix="pre-restore"
                    ),
                ).backup_path

            if had_original:
                os.replace(self.data_dir, rollback)
            try:
                os.replace(stage_data, self.data_dir)
                switched = True
            except Exception:
                if had_original and rollback.exists() and not self.data_dir.exists():
                    os.replace(rollback, self.data_dir)
                raise

            post_started = time.perf_counter()
            post_summary = read_database_summary(self.database_path)
            _require_healthy_database(post_summary, SCHEMA_VERSION)
            _require_manifest_statistics(post_summary, validation.manifest)
            self._verify_restored_assets(backup_root, validation.manifest)
            post_seconds = time.perf_counter() - post_started

            if rollback.exists():
                _remove_tree_within(rollback, self.data_dir.parent)
            switched = False
        except Exception as exc:
            if switched and rollback.exists():
                try:
                    os.replace(self.data_dir, failed_target)
                    os.replace(rollback, self.data_dir)
                    _remove_tree_within(failed_target, self.data_dir.parent)
                    switched = False
                except Exception as rollback_exc:
                    raise BackupError(
                        "恢复后验证失败且自动回滚失败；恢复前备份仍保留在 "
                        f"{pre_restore}。原始错误：{exc}；回滚错误：{rollback_exc}"
                    ) from rollback_exc
            elif switched and not had_original and self.data_dir.exists():
                _remove_tree_within(self.data_dir, self.data_dir.parent)
                switched = False
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"恢复失败，原正式资料已保留：{exc}") from exc
        finally:
            if stage_root.exists():
                _remove_tree_within(stage_root, self.data_dir.parent)

        LOGGER.info(
            "离线恢复成功：data=%s pre_restore_backup=%s",
            self.data_dir,
            pre_restore,
        )
        return RestoreResult(
            data_dir=self.data_dir,
            pre_restore_backup=pre_restore,
            validation_seconds=validation.duration_seconds,
            restore_seconds=time.perf_counter() - restore_started,
            post_validation_seconds=post_seconds,
            database_summary=post_summary,
        )

    def _validate_layout(self) -> dict[str, str]:
        values = {
            "raw": self.raw_dir,
            "pages": self.pages_dir,
            "markdown": self.markdown_dir,
            "database": self.database_path,
        }
        layout: dict[str, str] = {}
        for name, path in values.items():
            try:
                relative = path.relative_to(self.data_dir)
            except ValueError as exc:
                raise BackupError(f"关键路径必须位于正式 data 目录内：{name}={path}") from exc
            if not relative.parts:
                raise BackupError(f"关键路径不能直接等于 data 目录：{name}")
            layout[name] = relative.as_posix()
        directory_paths = {self.raw_dir, self.pages_dir, self.markdown_dir}
        if len(directory_paths) != 3:
            raise BackupError("raw、pages 和 markdown 目录必须彼此独立。")
        return layout

    def _backup_config(self) -> dict[str, object]:
        return {
            "config_format_version": CONFIG_FORMAT_VERSION,
            "source_paths": {
                "data": str(self.data_dir),
                "raw": str(self.raw_dir),
                "pages": str(self.pages_dir),
                "markdown": str(self.markdown_dir),
                "database": str(self.database_path),
            },
            "runtime": {
                "host": self.host,
                "port": self.port,
                "minimum_text_length": self.minimum_text_length,
                "pdf_render_dpi": self.pdf_render_dpi,
            },
        }

    def _manifest(
        self,
        database_summary: DatabaseSummary,
        records: list[dict[str, object]],
    ) -> dict[str, object]:
        by_kind = {
            kind: [record for record in records if record["kind"] == kind]
            for kind in DATA_KINDS
        }
        database_record = next(
            record for record in records if record["kind"] == DATABASE_KIND
        )
        return {
            "backup_format": BACKUP_FORMAT,
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "application_version": self.app_version,
            "schema_version": database_summary.schema_version,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "complete": True,
            "statistics": database_summary.statistics,
            "database": {
                "relative_path": database_record["path"],
                "size": database_record["size"],
                "sha256": database_record["sha256"],
                "integrity_check": database_summary.integrity_check,
                "foreign_key_violations": database_summary.foreign_key_violations,
            },
            "directories": {
                kind: {
                    "files": len(by_kind[kind]),
                    "bytes": sum(int(record["size"]) for record in by_kind[kind]),
                }
                for kind in DATA_KINDS
            },
            "key_paths": {
                "data": "data",
                "raw": f"data/{self._layout['raw']}",
                "pages": f"data/{self._layout['pages']}",
                "markdown": f"data/{self._layout['markdown']}",
                "database": f"data/{self._layout['database']}",
                "config": CONFIG_RELATIVE_PATH,
            },
            "files": records,
        }

    def _stage_backup_files(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
        stage_data: Path,
    ) -> None:
        key_paths = _mapping(manifest["key_paths"], "key_paths")
        source_prefixes = {
            kind: str(key_paths[kind]) for kind in (*DATA_KINDS, DATABASE_KIND)
        }
        target_prefixes = {
            "raw": self._layout["raw"],
            "pages": self._layout["pages"],
            "markdown": self._layout["markdown"],
            DATABASE_KIND: self._layout["database"],
        }
        for raw_record in _list(manifest["files"], "files"):
            record = _mapping(raw_record, "files[]")
            kind = str(record["kind"])
            if kind == "config":
                continue
            source_relative = str(record["path"])
            prefix = source_prefixes[kind]
            if kind == DATABASE_KIND:
                suffix = ""
            else:
                suffix = _relative_suffix(source_relative, prefix)
            target_relative = target_prefixes[kind]
            if suffix:
                target_relative = f"{target_relative}/{suffix}"
            source = _safe_existing_backup_file(backup_root, source_relative)
            target = _safe_destination(stage_data, target_relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular_file(source, target)

    def _preserve_excluded_current_files(self, stage_data: Path) -> None:
        """Preserve placeholders and migration backups excluded from data backups."""

        if not self.data_dir.exists():
            return
        for directory_name in ("raw", "pages", "markdown"):
            source = getattr(self, f"{directory_name}_dir") / ".gitkeep"
            if source.is_file() and not source.is_symlink():
                target = stage_data / self._layout[directory_name] / ".gitkeep"
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(source, target)
        source_database_dir = self.database_path.parent
        target_database_dir = (stage_data / self._layout["database"]).parent
        for name in (".gitkeep", "backups"):
            source = source_database_dir / name
            target = target_database_dir / name
            if not source.exists() or target.exists():
                continue
            if source.is_symlink():
                raise BackupError(f"拒绝保留符号链接：{source}")
            if source.is_dir():
                _copy_regular_tree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(source, target)

    def _verify_staged_assets(
        self,
        stage_data: Path,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> None:
        self._verify_asset_hashes(
            backup_root,
            manifest,
            target_roots={
                "raw": stage_data / self._layout["raw"],
                "pages": stage_data / self._layout["pages"],
                "markdown": stage_data / self._layout["markdown"],
            },
        )

    def _verify_restored_assets(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> None:
        self._verify_asset_hashes(
            backup_root,
            manifest,
            target_roots={
                "raw": self.raw_dir,
                "pages": self.pages_dir,
                "markdown": self.markdown_dir,
            },
        )

    @staticmethod
    def _verify_asset_hashes(
        backup_root: Path,
        manifest: Mapping[str, object],
        *,
        target_roots: Mapping[str, Path],
    ) -> None:
        key_paths = _mapping(manifest["key_paths"], "key_paths")
        for raw_record in _list(manifest["files"], "files"):
            record = _mapping(raw_record, "files[]")
            kind = str(record["kind"])
            if kind not in DATA_KINDS:
                continue
            suffix = _relative_suffix(str(record["path"]), str(key_paths[kind]))
            target = _safe_destination(target_roots[kind], suffix)
            if not target.is_file() or target.is_symlink():
                raise BackupError(f"恢复后关键文件缺失：{kind}/{suffix}")
            if target.stat().st_size != int(record["size"]):
                raise BackupError(f"恢复后文件大小不一致：{kind}/{suffix}")
            if sha256_file(target) != str(record["sha256"]):
                raise BackupError(f"恢复后文件哈希不一致：{kind}/{suffix}")


def read_database_summary(database_path: Path) -> DatabaseSummary:
    """Inspect an existing SQLite database without creating or modifying it."""

    path = _resolved(database_path)
    if not path.is_file() or path.is_symlink():
        raise BackupError(f"数据库文件不存在或不是普通文件：{path}")
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as connection:
            connection.execute("PRAGMA query_only = ON")
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = "ok" if integrity_rows == [("ok",)] else "; ".join(
                str(row[0]) for row in integrity_rows[:20]
            )
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            schema_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            counts = {
                "documents": _table_count(connection, "documents"),
                "pages": _table_count(connection, "pages"),
                "fts": _table_count(connection, "page_search"),
                "evidence": _table_count(connection, "evidence_items"),
                "projects": _table_count(connection, "projects"),
                "tags": _table_count(connection, "tags"),
            }
    except sqlite3.Error as exc:
        raise BackupError(f"无法只读检查数据库：{exc}") from exc
    return DatabaseSummary(
        schema_version=schema_version,
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        **counts,
    )


def validate_backup(
    backup_path: Path,
    *,
    expected_app_version: str | None = None,
    expected_schema_version: int = SCHEMA_VERSION,
) -> BackupValidation:
    """Validate manifest types, inventory hashes, database, and source links."""

    started = time.perf_counter()
    root = _resolved(backup_path)
    errors: list[str] = []
    warnings: list[str] = []
    manifest: Mapping[str, object] | None = None
    summary: DatabaseSummary | None = None
    try:
        _reject_symlink(root, "备份目录")
        if not root.is_dir():
            raise BackupError(f"备份目录不存在：{root}")
        manifest = _load_and_validate_manifest(root / MANIFEST_NAME)
        if expected_app_version is not None and str(manifest["application_version"]) != (
            _normalized_version(expected_app_version)
        ):
            raise BackupError(
                "备份应用版本不兼容："
                f"{manifest['application_version']}，当前仅支持 "
                f"{_normalized_version(expected_app_version)}"
            )
        if int(manifest["schema_version"]) != expected_schema_version:
            raise BackupError(
                f"备份 schema v{manifest['schema_version']} 不兼容，"
                f"当前仅支持 v{expected_schema_version}。"
            )
        records = _list(manifest["files"], "files")
        expected_files: set[str] = set()
        records_by_path: dict[str, Mapping[str, object]] = {}
        for raw_record in records:
            record = _validate_file_record(raw_record)
            relative = str(record["path"])
            if relative in expected_files:
                raise BackupError(f"manifest 存在重复文件路径：{relative}")
            expected_files.add(relative)
            records_by_path[relative] = record
            file_path = _safe_existing_backup_file(root, relative)
            if file_path.stat().st_size != int(record["size"]):
                raise BackupError(f"备份文件大小不一致：{relative}")
            if sha256_file(file_path) != str(record["sha256"]):
                raise BackupError(f"备份文件哈希不一致：{relative}")
        actual_files = {
            path.relative_to(root).as_posix()
            for path, _ in _walk_regular_files(root, include_manifest=True)
        }
        expected_with_manifest = expected_files | {MANIFEST_NAME}
        missing = expected_with_manifest - actual_files
        unexpected = actual_files - expected_with_manifest
        if missing:
            raise BackupError("备份缺少文件：" + "、".join(sorted(missing)))
        if unexpected:
            raise BackupError("备份包含清单外文件：" + "、".join(sorted(unexpected)))

        database_info = _mapping(manifest["database"], "database")
        database_relative = str(database_info["relative_path"])
        database_record = records_by_path.get(database_relative)
        if database_record is None or database_record["kind"] != DATABASE_KIND:
            raise BackupError("manifest 的数据库记录不存在或类型错误。")
        if int(database_info["size"]) != int(database_record["size"]):
            raise BackupError("manifest 数据库大小字段不一致。")
        if str(database_info["sha256"]) != str(database_record["sha256"]):
            raise BackupError("manifest 数据库哈希字段不一致。")
        summary = read_database_summary(root / database_relative)
        _require_healthy_database(summary, expected_schema_version)
        _require_manifest_statistics(summary, manifest)
        if str(database_info["integrity_check"]) != summary.integrity_check:
            raise BackupError("manifest 数据库完整性结果与实际不一致。")
        if int(database_info["foreign_key_violations"]) != summary.foreign_key_violations:
            raise BackupError("manifest 外键检查结果与实际不一致。")

        _validate_directory_totals(manifest, records_by_path.values())
        config = _load_backup_config(root / CONFIG_RELATIVE_PATH)
        _validate_database_asset_links(root, manifest, records_by_path, config)
    except BackupError as exc:
        errors.append(str(exc))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        errors.append(f"备份格式或文件读取失败：{exc}")
    return BackupValidation(
        backup_path=root,
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        manifest=manifest,
        database_summary=summary,
        duration_seconds=time.perf_counter() - started,
    )


def list_backup_candidates(backups_dir: Path) -> list[Path]:
    """List finalized-looking backup directories newest first, without validating."""

    root = _resolved(backups_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and not path.name.startswith(".")
        and (path / MANIFEST_NAME).is_file()
    ]
    return sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def sha256_file(path: Path) -> str:
    """Return a regular file's SHA-256 without following symbolic links."""

    if path.is_symlink() or not path.is_file():
        raise BackupError(f"拒绝读取非普通文件：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    if source_path.is_symlink() or not source_path.is_file():
        raise BackupError(f"正式数据库不存在或不是普通文件：{source_path}")
    try:
        uri = f"file:{source_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as source, closing(
            sqlite3.connect(destination_path, timeout=30.0)
        ) as destination:
            source.execute("PRAGMA query_only = ON")
            source.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
    except sqlite3.Error as exc:
        destination_path.unlink(missing_ok=True)
        raise BackupError(f"无法创建一致性 SQLite 快照：{exc}") from exc
    destination_path.with_name(destination_path.name + "-wal").unlink(missing_ok=True)
    destination_path.with_name(destination_path.name + "-shm").unlink(missing_ok=True)


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    allowed = {"documents", "pages", "page_search", "evidence_items", "projects", "tags"}
    if table not in allowed:
        raise BackupError(f"不允许统计数据库表：{table}")
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _require_healthy_database(summary: DatabaseSummary, schema_version: int) -> None:
    if summary.integrity_check != "ok":
        raise BackupError(f"数据库完整性检查失败：{summary.integrity_check}")
    if summary.foreign_key_violations:
        raise BackupError(f"数据库存在 {summary.foreign_key_violations} 条外键违规。")
    if summary.schema_version != schema_version:
        raise BackupError(
            f"数据库 schema v{summary.schema_version} 与要求的 v{schema_version} 不兼容。"
        )


def _require_manifest_statistics(
    summary: DatabaseSummary, manifest: Mapping[str, object]
) -> None:
    statistics = _mapping(manifest["statistics"], "statistics")
    for name, actual in summary.statistics.items():
        if int(statistics[name]) != actual:
            raise BackupError(
                f"数据库统计与 manifest 不一致：{name}="
                f"{actual}，manifest={statistics[name]}"
            )


def _load_and_validate_manifest(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise BackupError("备份缺少 manifest.json，或该文件不是普通文件。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest.json 无法读取或不是有效 JSON：{exc}") from exc
    manifest = _mapping(value, "manifest")
    if set(manifest) != _MANIFEST_KEYS:
        missing = _MANIFEST_KEYS - set(manifest)
        unknown = set(manifest) - _MANIFEST_KEYS
        raise BackupError(
            "manifest 字段不符合白名单。"
            f"缺少={sorted(missing)}，未知={sorted(unknown)}"
        )
    if manifest["backup_format"] != BACKUP_FORMAT:
        raise BackupError("不支持的备份格式。")
    if _integer(manifest["backup_format_version"], "backup_format_version") != (
        BACKUP_FORMAT_VERSION
    ):
        raise BackupError(
            f"不支持的备份格式版本：{manifest['backup_format_version']}"
        )
    if manifest["complete"] is not True:
        raise BackupError("备份未标记为完整，不允许恢复。")
    _normalized_version(_string(manifest["application_version"], "application_version"))
    _integer(manifest["schema_version"], "schema_version", minimum=1)
    _parse_timestamp(_string(manifest["created_at"], "created_at"))
    statistics = _mapping(manifest["statistics"], "statistics")
    if set(statistics) != _STATISTIC_KEYS:
        raise BackupError("statistics 字段不符合白名单。")
    for key, raw_value in statistics.items():
        _integer(raw_value, f"statistics.{key}", minimum=0)
    database = _mapping(manifest["database"], "database")
    if set(database) != {
        "relative_path",
        "size",
        "sha256",
        "integrity_check",
        "foreign_key_violations",
    }:
        raise BackupError("database 字段不符合白名单。")
    _safe_manifest_path(_string(database["relative_path"], "database.relative_path"))
    _integer(database["size"], "database.size", minimum=0)
    _validate_sha256(_string(database["sha256"], "database.sha256"))
    _string(database["integrity_check"], "database.integrity_check")
    _integer(
        database["foreign_key_violations"],
        "database.foreign_key_violations",
        minimum=0,
    )
    directories = _mapping(manifest["directories"], "directories")
    if set(directories) != set(DATA_KINDS):
        raise BackupError("directories 字段不符合白名单。")
    for kind, raw_value in directories.items():
        counts = _mapping(raw_value, f"directories.{kind}")
        if set(counts) != {"files", "bytes"}:
            raise BackupError(f"directories.{kind} 字段不符合白名单。")
        _integer(counts["files"], f"directories.{kind}.files", minimum=0)
        _integer(counts["bytes"], f"directories.{kind}.bytes", minimum=0)
    key_paths = _mapping(manifest["key_paths"], "key_paths")
    if set(key_paths) != _KEY_PATH_KEYS:
        raise BackupError("key_paths 字段不符合白名单。")
    for key, raw_value in key_paths.items():
        _safe_manifest_path(_string(raw_value, f"key_paths.{key}"), allow_directory=True)
    _list(manifest["files"], "files")
    return manifest


def _validate_file_record(value: object) -> Mapping[str, object]:
    record = _mapping(value, "files[]")
    if set(record) != {"path", "size", "sha256", "kind"}:
        raise BackupError("files[] 字段不符合白名单。")
    _safe_manifest_path(_string(record["path"], "files[].path"))
    _integer(record["size"], "files[].size", minimum=0)
    _validate_sha256(_string(record["sha256"], "files[].sha256"))
    kind = _string(record["kind"], "files[].kind")
    if kind not in FILE_KINDS:
        raise BackupError(f"files[] 包含未知文件类型：{kind}")
    return record


def _validate_directory_totals(
    manifest: Mapping[str, object], records: Iterable[Mapping[str, object]]
) -> None:
    directories = _mapping(manifest["directories"], "directories")
    materialized = list(records)
    for kind in DATA_KINDS:
        kind_records = [record for record in materialized if record["kind"] == kind]
        values = _mapping(directories[kind], f"directories.{kind}")
        if int(values["files"]) != len(kind_records):
            raise BackupError(f"manifest 的 {kind} 文件数量不一致。")
        if int(values["bytes"]) != sum(int(record["size"]) for record in kind_records):
            raise BackupError(f"manifest 的 {kind} 文件字节数不一致。")


def _load_backup_config(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise BackupError("备份缺少恢复配置 config/settings.json。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"恢复配置无法读取：{exc}") from exc
    config = _mapping(value, "config")
    if set(config) != {"config_format_version", "source_paths", "runtime"}:
        raise BackupError("恢复配置字段不符合白名单。")
    if _integer(config["config_format_version"], "config_format_version") != (
        CONFIG_FORMAT_VERSION
    ):
        raise BackupError("恢复配置版本不受支持。")
    source_paths = _mapping(config["source_paths"], "source_paths")
    if set(source_paths) != {"data", "raw", "pages", "markdown", "database"}:
        raise BackupError("恢复配置 source_paths 字段不符合白名单。")
    for key, value in source_paths.items():
        raw_path = _string(value, f"source_paths.{key}")
        if not Path(raw_path).is_absolute():
            raise BackupError(f"恢复配置中的源路径必须是绝对路径：{key}")
    runtime = _mapping(config["runtime"], "runtime")
    if set(runtime) != {"host", "port", "minimum_text_length", "pdf_render_dpi"}:
        raise BackupError("恢复配置 runtime 字段不符合白名单。")
    if _string(runtime["host"], "runtime.host") != "127.0.0.1":
        raise BackupError("备份配置的监听地址不是 127.0.0.1。")
    _integer(runtime["port"], "runtime.port", minimum=1, maximum=65535)
    _integer(runtime["minimum_text_length"], "runtime.minimum_text_length", minimum=0)
    _integer(runtime["pdf_render_dpi"], "runtime.pdf_render_dpi", minimum=72, maximum=600)
    return config


def _validate_database_asset_links(
    backup_root: Path,
    manifest: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
    config: Mapping[str, object],
) -> None:
    key_paths = _mapping(manifest["key_paths"], "key_paths")
    source_paths = _mapping(config["source_paths"], "source_paths")
    database_path = backup_root / str(key_paths["database"])
    try:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            documents = connection.execute("SELECT source_path, sha256 FROM documents").fetchall()
            pages = connection.execute(
                "SELECT image_path, markdown_path, markdown_content FROM pages"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"无法核对数据库文件引用：{exc}") from exc
    for source_path, expected_hash in documents:
        relative = _database_record_to_backup_path(
            str(source_path),
            str(source_paths["raw"]),
            str(key_paths["raw"]),
        )
        record = records.get(relative)
        if record is None or record["kind"] != "raw":
            raise BackupError(f"数据库引用的原始 PDF 未包含在备份中：{relative}")
        if str(record["sha256"]).casefold() != str(expected_hash).casefold():
            raise BackupError(f"原始 PDF 哈希与数据库记录不一致：{relative}")
    for image_path, markdown_path, markdown_content in pages:
        image_relative = _database_record_to_backup_path(
            str(image_path),
            str(source_paths["pages"]),
            str(key_paths["pages"]),
        )
        if image_relative not in records or records[image_relative]["kind"] != "pages":
            raise BackupError(f"数据库引用的页面 PNG 未包含在备份中：{image_relative}")
        if markdown_path:
            markdown_relative = _database_record_to_backup_path(
                str(markdown_path),
                str(source_paths["markdown"]),
                str(key_paths["markdown"]),
            )
            if (
                markdown_relative not in records
                or records[markdown_relative]["kind"] != "markdown"
            ):
                raise BackupError(
                    f"数据库引用的页面 Markdown 未包含在备份中：{markdown_relative}"
                )
        elif str(markdown_content).strip():
            raise BackupError("数据库存在 Markdown 正文但缺少 markdown_path。")


def _database_record_to_backup_path(
    recorded_path: str, source_root: str, backup_prefix: str
) -> str:
    source = _resolved(Path(source_root))
    recorded = Path(recorded_path)
    if not recorded.is_absolute():
        recorded = source.parent.parent / recorded
    normalized = _resolved(recorded)
    try:
        relative = normalized.relative_to(source)
    except ValueError as exc:
        raise BackupError(f"数据库路径超出配置目录，拒绝使用：{recorded_path}") from exc
    return f"{backup_prefix}/{relative.as_posix()}"


def _rebase_database_paths(
    database_path: Path,
    config_path: Path,
    *,
    target_raw: Path,
    target_pages: Path,
    target_markdown: Path,
) -> None:
    config = _load_backup_config(config_path)
    roots = _mapping(config["source_paths"], "source_paths")
    try:
        with closing(sqlite3.connect(database_path, timeout=30.0)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            document_rows = connection.execute("SELECT id, source_path FROM documents").fetchall()
            for document_id, recorded_path in document_rows:
                relative = _recorded_relative(str(recorded_path), str(roots["raw"]))
                connection.execute(
                    "UPDATE documents SET source_path = ? WHERE id = ?",
                    (str(_safe_destination(target_raw, relative.as_posix())), document_id),
                )
            page_rows = connection.execute(
                "SELECT id, image_path, markdown_path FROM pages"
            ).fetchall()
            for page_id, image_path, markdown_path in page_rows:
                image_relative = _recorded_relative(str(image_path), str(roots["pages"]))
                new_markdown: str | None = None
                if markdown_path:
                    markdown_relative = _recorded_relative(
                        str(markdown_path), str(roots["markdown"])
                    )
                    new_markdown = str(
                        _safe_destination(target_markdown, markdown_relative.as_posix())
                    )
                connection.execute(
                    "UPDATE pages SET image_path = ?, markdown_path = ? WHERE id = ?",
                    (
                        str(_safe_destination(target_pages, image_relative.as_posix())),
                        new_markdown,
                        page_id,
                    ),
                )
            connection.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"无法安全重定位恢复数据库路径：{exc}") from exc


def _recorded_relative(recorded_path: str, source_root: str) -> Path:
    root = _resolved(Path(source_root))
    value = Path(recorded_path)
    if not value.is_absolute():
        value = root.parent.parent / value
    try:
        return _resolved(value).relative_to(root)
    except ValueError as exc:
        raise BackupError(f"备份数据库路径超出原配置目录：{recorded_path}") from exc


def _walk_regular_files(
    root: Path, *, include_manifest: bool = False
) -> Iterable[tuple[Path, Path]]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise BackupError(f"目录不存在或不是普通目录：{root}")
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            candidate = current_path / directory
            if candidate.is_symlink():
                raise BackupError(f"拒绝跟随符号链接目录：{candidate}")
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                raise BackupError(f"拒绝复制非普通文件：{path}")
            relative = path.relative_to(root)
            if not include_manifest and path.name == ".gitkeep":
                continue
            yield path, relative


def _copy_regular_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for source_file, relative in _walk_regular_files(source, include_manifest=True):
        destination = _safe_destination(target, relative.as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_file(source_file, destination)


def _copy_regular_file(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"拒绝复制非普通文件：{source}")
    if target.exists():
        raise BackupError(f"目标文件已存在，不会覆盖：{target}")
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle, length=_HASH_CHUNK_SIZE)
        target_handle.flush()
        os.fsync(target_handle.fileno())


def _file_record(path: Path, root: Path, kind: str) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "kind": kind,
    }


def _safe_manifest_path(value: str, *, allow_directory: bool = False) -> PurePosixPath:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise BackupError(f"manifest 包含非法相对路径：{value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError(f"manifest 包含不安全路径：{value!r}")
    if not allow_directory and path.name in {"", ".", ".."}:
        raise BackupError(f"manifest 包含非法文件路径：{value!r}")
    return path


def _safe_destination(root: Path, relative: str) -> Path:
    path = _safe_manifest_path(relative, allow_directory=True)
    destination = _resolved(root / Path(*path.parts))
    if not _is_within(destination, _resolved(root)):
        raise BackupError(f"路径逃逸目标目录：{relative}")
    return destination


def _safe_existing_backup_file(root: Path, relative: str) -> Path:
    path = _safe_destination(root, relative)
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"备份关键文件缺失或不是普通文件：{relative}")
    return path


def _relative_suffix(value: str, prefix: str) -> str:
    value_path = _safe_manifest_path(value)
    prefix_path = _safe_manifest_path(prefix, allow_directory=True)
    try:
        relative = value_path.relative_to(prefix_path)
    except ValueError as exc:
        raise BackupError(f"文件路径不属于声明目录：{value}") from exc
    if not relative.parts:
        return ""
    return relative.as_posix()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink(path: Path, label: str) -> None:
    if path.exists() and path.is_symlink():
        raise BackupError(f"{label}不能是符号链接：{path}")


def _remove_tree_within(path: Path, allowed_parent: Path) -> None:
    resolved = _resolved(path)
    parent = _resolved(allowed_parent)
    if resolved == parent or not _is_within(resolved, parent):
        raise BackupError(f"拒绝清理超出临时范围的目录：{resolved}")
    if resolved.is_symlink():
        raise BackupError(f"拒绝清理符号链接目录：{resolved}")
    shutil.rmtree(resolved)


def _cleanup_incomplete(staging: Path, cause: Exception) -> None:
    if not staging.exists():
        return
    try:
        _remove_tree_within(staging, staging.parent)
    except Exception:
        try:
            (staging / "INCOMPLETE.txt").write_text(
                "此备份未完成，禁止用于恢复。\n"
                f"失败类型：{type(cause).__name__}\n",
                encoding="utf-8",
            )
        except OSError:
            LOGGER.exception("无法清理或标记未完成备份：%s", staging)


def _default_backup_name(app_version: str, *, prefix: str = "ekb") -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-v{_normalized_version(app_version)}-{timestamp}"


def _validate_backup_name(value: str) -> None:
    if (
        not value
        or len(value) > 120
        or value in {".", ".."}
        or any(character in value for character in '<>:"/\\|?*')
    ):
        raise BackupError(f"备份名称不安全：{value!r}")


def _normalized_version(value: str) -> str:
    version = value.strip()
    if version.startswith("v"):
        version = version[1:]
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise BackupError(f"应用版本格式错误：{value!r}")
    return version


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError(f"创建时间格式错误：{value!r}") from exc
    if parsed.tzinfo is None:
        raise BackupError("创建时间必须包含时区。")
    return parsed


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise BackupError(f"SHA-256 字段格式错误：{value!r}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise BackupError(f"{field} 必须是对象。")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise BackupError(f"{field} 必须是数组。")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise BackupError(f"{field} 必须是字符串。")
    return value


def _integer(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(f"{field} 必须是整数。")
    if minimum is not None and value < minimum:
        raise BackupError(f"{field} 不能小于 {minimum}。")
    if maximum is not None and value > maximum:
        raise BackupError(f"{field} 不能大于 {maximum}。")
    return value
