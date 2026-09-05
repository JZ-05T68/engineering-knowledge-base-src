"""Seed the isolated 8511 preview environment from safe, one-way sources.

Direction is enforced by construction: this script only ever writes under
``staging-data/`` (the 8511 preview root) and only ever reads from the
canonical ``data/`` directory. No command here can overwrite production
data, because production paths are never passed as a write target.

Seeds:

- ``empty``           wipe the preview data root for a first-use (novice) run.
- ``formal-snapshot`` one-way copy of the canonical formal data into the
                      preview root: documents / pages / markdown /
                      agent readings plus a consistent SQLite backup of the
                      knowledge base (never a raw file copy of a live DB).

Logs and runtime pid files are left untouched; deleting them is the
service manager's job through normal stop/start cycles.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DEFAULT_STAGING_ROOT, get_settings  # noqa: E402

_DATA_SUBDIRS: Final[tuple[str, ...]] = (
    "raw",
    "pages",
    "markdown",
    "agent-readings",
)
_IGNORED_IN_COPY: Final = shutil.ignore_patterns(
    "*.db-shm",
    "*.db-wal",
    "__pycache__",
    ".DS_Store",
)


def _preview_data_dir() -> Path:
    """Return the preview data directory, refusing any other write root."""

    data_dir = DEFAULT_STAGING_ROOT / "data"
    resolved_root = DEFAULT_STAGING_ROOT.resolve()
    expected_root = (PROJECT_ROOT / "staging-data").resolve()
    if resolved_root != expected_root:
        raise SystemExit("拒绝执行：预览数据根目录必须是 canonical 的 staging-data/。")
    return data_dir


def _backup_sqlite(source: Path, target: Path) -> None:
    """Copy one SQLite database through the online backup API."""

    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        target_connection = sqlite3.connect(target)
        try:
            with target_connection:
                source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def seed_empty(*, assume_yes: bool) -> int:
    """Reset the preview data root to an empty first-use state."""

    data_dir = _preview_data_dir()
    if not assume_yes and not _confirm(f"将清空预览数据目录 {data_dir}"):
        print("已取消。")
        return 1
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    (DEFAULT_STAGING_ROOT / "backups").mkdir(parents=True, exist_ok=True)
    print(f"预览环境已重置为空库：{data_dir}")
    print("下次启动 8511 时会按当前 schema 全新初始化。")
    return 0


def seed_formal_snapshot(*, assume_yes: bool) -> int:
    """Copy the canonical formal data into the preview root (one-way)."""

    settings = get_settings()
    source_data = settings.data_dir
    source_database = settings.database_path
    if not source_database.is_file():
        print(f"取消：正式库不存在：{source_database}")
        return 1
    target_data = _preview_data_dir()
    if target_data.exists() and any(target_data.iterdir()):
        if not assume_yes and not _confirm(
            f"预览数据目录 {target_data} 非空，将被正式快照覆盖（仅预览环境）"
        ):
            print("已取消。")
            return 1
        shutil.rmtree(target_data)
    target_data.mkdir(parents=True)

    for subdir in _DATA_SUBDIRS:
        source_subdir = source_data / subdir
        if source_subdir.is_dir():
            shutil.copytree(source_subdir, target_data / subdir, ignore=_IGNORED_IN_COPY)
            print(f"已复制 {subdir}/")
    _backup_sqlite(source_database, target_data / "database" / "knowledge.db")
    print("已通过 SQLite backup API 复制正式数据库（一致性好于文件复制）。")
    print(f"预览环境已就绪：{target_data}")
    print("方向校验：本次操作只写入了 staging-data/，正式数据只被读取。")
    return 0


def _confirm(message: str) -> bool:
    """Ask for one explicit interactive confirmation."""

    answer = input(f"{message}，继续？[y/N] ").strip().lower()
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description="8511 预览环境数据种子工具")
    parser.add_argument("mode", choices=("empty", "formal-snapshot"))
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    arguments = parser.parse_args()
    if arguments.mode == "empty":
        return seed_empty(assume_yes=arguments.yes)
    return seed_formal_snapshot(assume_yes=arguments.yes)


if __name__ == "__main__":
    raise SystemExit(main())
