"""Offline command-line restore for verified v0.5.0 directory backups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import service_manager  # noqa: E402
from src.backup_service import (  # noqa: E402
    BackupError,
    BackupService,
    _is_link_like,
    validate_backup,
)
from src.config import Settings, get_settings  # noqa: E402
from src.migrations import SCHEMA_VERSION  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit, non-interactive restore command interface."""

    parser = argparse.ArgumentParser(
        description="验证并离线恢复工程知识库 v0.5.0 完整备份"
    )
    parser.add_argument("--backup", type=Path, required=True, help="包含 manifest.json 的备份目录")
    parser.add_argument(
        "--target-data-dir",
        type=Path,
        help="仅用于隔离验收；省略时恢复正式 data 目录",
    )
    parser.add_argument(
        "--target-backups-dir",
        type=Path,
        help="隔离恢复的恢复前备份目录；默认位于目标 data 同级",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="实际恢复必须明确传入 RESTORE；预检查不需要",
    )
    parser.add_argument(
        "--precheck-only",
        action="store_true",
        help="只验证 manifest、文件哈希和数据库，不写入任何目标目录",
    )
    return parser


def _service_for_paths(settings: Settings, data_dir: Path, backups_dir: Path) -> BackupService:
    data = data_dir.resolve(strict=False)
    return BackupService(
        app_version=settings.app_version,
        data_dir=data,
        raw_dir=data / "raw",
        pages_dir=data / "pages",
        markdown_dir=data / "markdown",
        database_path=data / "database" / "knowledge.db",
        backups_dir=backups_dir.resolve(strict=False),
        host=settings.host,
        port=settings.port,
        minimum_text_length=settings.minimum_text_length,
        pdf_render_dpi=settings.pdf_render_dpi,
    )


def _formal_service(settings: Settings) -> BackupService:
    return BackupService(
        app_version=settings.app_version,
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        host=settings.host,
        port=settings.port,
        minimum_text_length=settings.minimum_text_length,
        pdf_render_dpi=settings.pdf_render_dpi,
    )


def _formal_service_is_running() -> bool:
    state = service_manager.detect_state(clean_stale=False)
    return state.code in {"running", "starting", "port_occupied"}


def _validate_isolated_target(target: Path, formal_data_dir: Path) -> Path:
    requested = target.expanduser()
    if _is_link_like(requested):
        raise BackupError(f"隔离恢复目标不能是符号链接或 Windows 重解析点：{requested}")
    resolved = requested.resolve(strict=False)
    forbidden = {
        Path(resolved.anchor).resolve(strict=False),
        Path.home().resolve(strict=False),
        PROJECT_ROOT.resolve(strict=False),
        formal_data_dir.resolve(strict=False),
    }
    if resolved in forbidden:
        raise BackupError(f"隔离恢复目标过于宽泛或与正式目录重合：{resolved}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    """Validate first, then restore only after explicit confirmation."""

    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    # Validate the caller-provided path before resolution so Windows junctions
    # cannot hide their reparse-point identity.
    backup_path = arguments.backup.expanduser()
    validation = validate_backup(
        backup_path,
        expected_app_version=settings.app_version,
        expected_schema_version=SCHEMA_VERSION,
    )
    if not validation.valid or validation.manifest is None:
        print("[FAIL] 备份验证失败：")
        for error in validation.errors:
            print(f"  - {error}")
        return 2
    statistics = validation.manifest["statistics"]
    print("[PASS] 备份预检查通过")
    print(f"创建时间：{validation.manifest['created_at']}")
    print(
        f"文档：{statistics['documents']}，页面：{statistics['pages']}，"
        f"FTS：{statistics['fts']}，schema：v{validation.manifest['schema_version']}"
    )
    print(f"验证耗时：{validation.duration_seconds:.3f} 秒")
    if arguments.precheck_only:
        return 0
    if arguments.confirm != "RESTORE":
        print("[FAIL] 未提供 --confirm RESTORE；没有修改任何资料。")
        return 3

    isolated = arguments.target_data_dir is not None
    if isolated:
        target_data = _validate_isolated_target(
            arguments.target_data_dir, settings.data_dir
        )
        backups_dir = (
            arguments.target_backups_dir.resolve(strict=False)
            if arguments.target_backups_dir
            else target_data.parent / "isolated-pre-restore-backups"
        )
        service = _service_for_paths(settings, target_data, backups_dir)
        service_check = None
        require_existing = False
    else:
        service = _formal_service(settings)
        service_check = _formal_service_is_running
        require_existing = True

    try:
        result = service.restore_backup(
            backup_path,
            service_is_running=service_check,
            require_existing_target=require_existing,
        )
    except BackupError as exc:
        print(f"[FAIL] {exc}")
        return 4
    print("[PASS] 恢复及恢复后验证完成")
    print(f"目标：{result.data_dir}")
    print(f"恢复前备份：{result.pre_restore_backup or '目标原先不存在，无需备份'}")
    print(
        f"恢复后统计：文档 {result.database_summary.documents}，"
        f"页面 {result.database_summary.pages}，FTS {result.database_summary.fts}"
    )
    print(
        f"恢复耗时：{result.restore_seconds:.3f} 秒；"
        f"恢复后验证：{result.post_validation_seconds:.3f} 秒"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
