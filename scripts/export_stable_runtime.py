"""Export an immutable stable runtime snapshot for the 8501 formal service.

Why this exists: the 8501 formal service used to run the canonical workspace
directly, so any half-written source edit made by a development agent could be
picked up by a Streamlit rerun (this caused the v13 mid-edit migration
incidents). The stable runtime is the fix:

- the canonical workspace ``D:\\Projects\\ekb-dev`` stays the only place code
  is ever written;
- the stable runtime under ``D:\\Projects\\ekb-runtime\\stable`` is a frozen
  export of one committed git checkpoint, used exclusively to run 8501;
- formal data is seeded into the snapshot exactly once; afterwards the
  snapshot owns the live formal data and this script never overwrites it.

The snapshot is not a clone, not a worktree and not a second development
workspace: nobody edits it. To update 8501 after a new stable checkpoint,
commit/push in the canonical workspace, then re-run this script and restart
the formal service.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_STABLE_ROOT: Final[Path] = Path(r"D:\Projects\ekb-runtime\stable")
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


class ExportError(RuntimeError):
    """Raised when the stable runtime cannot be exported safely."""


def _require_clean_checkpoint() -> str:
    """Return HEAD and refuse to export a dirty or unpushed workspace."""

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            raise ExportError(f"git {arguments[0]} 失败：{result.stderr.strip()}")
        return result.stdout.strip()

    status = git("status", "--porcelain")
    if status:
        raise ExportError(
            "canonical workspace 存在未提交改动；请先形成 checkpoint 再导出稳定运行时。"
        )
    head = git("rev-parse", "HEAD")
    git("rev-parse", "--verify", "HEAD@{upstream}")
    ahead_behind = git("rev-list", "--left-right", "--count", "HEAD@{upstream}...HEAD")
    behind, ahead = (int(value) for value in ahead_behind.split())
    if behind or ahead:
        raise ExportError(
            "本地与 origin 不一致（ahead/behind 非 0）；请先 push 对齐再导出。"
        )
    return head


def _extract_source(target: Path, head: str) -> None:
    """Extract the committed source tree into the stable runtime."""

    archive = subprocess.run(
        ["git", "archive", head, "--format=tar"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise ExportError(f"git archive 失败：{archive.stderr.decode(errors='replace')}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")


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


def _seed_formal_data(target: Path) -> None:
    """Seed formal data into the snapshot exactly once (never overwritten)."""

    target_database = target / "data" / "database" / "knowledge.db"
    if target_database.is_file():
        print("正式数据已存在于稳定运行时，保持不动（不覆盖正式历史）。")
        return
    source_database = PROJECT_ROOT / "data" / "database" / "knowledge.db"
    if not source_database.is_file():
        raise ExportError(f"找不到正式库，无法完成首次数据种子：{source_database}")
    for subdir in _DATA_SUBDIRS:
        source_subdir = PROJECT_ROOT / "data" / subdir
        if source_subdir.is_dir():
            shutil.copytree(
                source_subdir, target / "data" / subdir, ignore=_IGNORED_IN_COPY
            )
            print(f"已复制 {subdir}/")
    _backup_sqlite(source_database, target_database)
    print("已通过 SQLite backup API 种子正式数据库。")


def _seed_local_config(target: Path) -> None:
    """Copy the local .env so the formal runtime keeps its own credentials."""

    source_env = PROJECT_ROOT / ".env"
    target_env = target / ".env"
    if source_env.is_file() and not target_env.exists():
        shutil.copyfile(source_env, target_env)
        print("已复制本地 .env 配置（内容不打印、不进入 Git）。")


def _ensure_venv(target: Path) -> None:
    """Give the snapshot the project interpreter via a junction (no copy)."""

    link = target / ".venv"
    if link.exists():
        return
    real_venv = PROJECT_ROOT / ".venv"
    if not real_venv.is_dir():
        raise ExportError(f"找不到项目虚拟环境：{real_venv}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(real_venv)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExportError(f"创建 .venv junction 失败：{result.stderr.strip()}")
    print("已创建 .venv junction（解释器共享，源代码仍完全独立）。")


def export(target: Path) -> int:
    """Export the current pushed checkpoint into the stable runtime."""

    head = _require_clean_checkpoint()
    target.mkdir(parents=True, exist_ok=True)
    print(f"导出 checkpoint {head[:12]} → {target}")
    _extract_source(target, head)
    _seed_formal_data(target)
    _seed_local_config(target)
    _ensure_venv(target)
    manifest = {
        "commit": head,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "role": "stable-runtime",
        "note": "只读运行快照；正式数据只在此目录内演进。",
    }
    (target / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("稳定运行时导出完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 8501 稳定运行时快照")
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_STABLE_ROOT,
        help=f"快照目标目录（默认 {DEFAULT_STABLE_ROOT}）",
    )
    arguments = parser.parse_args()
    try:
        return export(arguments.target)
    except ExportError as exc:
        print(f"导出中止：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
