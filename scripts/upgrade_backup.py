"""CLI for isolated schema-v8 legacy backup upgrade (v0.5.3 Phase 6C).

Usage:
    python scripts/upgrade_backup.py <legacy_db_path> <output_dir>
        [--assets-data-dir PATH] [--dry-run] [--backup-name NAME]

Exit codes: 0 success, 2 invalid arguments, 3 upgrade failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.legacy_backup_upgrade_service import (  # noqa: E402
    LegacyBackupUpgradeError,
    LegacyBackupUpgradeService,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="隔离升级 schema v8 旧备份到当前 schema。"
    )
    parser.add_argument("legacy_db_path", help="schema v8 旧备份数据库文件路径")
    parser.add_argument("output_dir", help="输出目录（不得位于正式 data 目录内）")
    parser.add_argument(
        "--assets-data-dir",
        default=None,
        help="可选的只读资产源 data 目录（含 raw/pages/markdown），用于生成完整目录备份",
    )
    parser.add_argument(
        "--dry-run",
        "--validate-only",
        action="store_true",
        help="只验证并迁移到 staging，不产出最终备份",
    )
    parser.add_argument("--backup-name", default=None, help="新备份目录名（可选）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    legacy = Path(args.legacy_db_path)
    output = Path(args.output_dir)
    assets = Path(args.assets_data_dir) if args.assets_data_dir else None
    try:
        report = LegacyBackupUpgradeService().upgrade(
            legacy,
            output,
            assets_data_dir=assets,
            backup_name=args.backup_name,
            dry_run=args.dry_run,
        )
    except LegacyBackupUpgradeError as exc:
        print(f"升级失败：{exc}", file=sys.stderr)
        return 3
    print(json.dumps(report.report, ensure_ascii=False, indent=2, sort_keys=True))
    if report.backup_path is not None:
        print(f"新备份路径：{report.backup_path}")
    print(f"原始旧备份 SHA-256：{report.original_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
