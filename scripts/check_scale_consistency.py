"""Read-only consistency checks for isolated v0.2.3 scale-test databases.

The checker never repairs, deletes or writes to the checked data: SQLite is
opened with ``mode=ro`` + ``PRAGMA query_only``, and no file under the pages
directory is touched beyond ``stat``.  Both ``--database`` and ``--pages-dir``
are mandatory — the tool never guesses the formal paths — and any target
inside the formal project ``D:/Projects/engineering-kb`` is refused outright.
Note: opening a WAL-mode database read-only may create transient ``-shm`` /
``-wal`` sidecar files (standard SQLite behavior, identical to the production
``read_database_summary`` diagnostics); the database file itself is never
modified, which the test-suite verifies byte-for-byte.

Reuse notes: integrity / foreign-key / table-count facts come from
``src.backup_service.read_database_summary`` and the directory walk reuses
``src.diagnostic_service._walk_diagnostic_files``.  The higher-level
missing-file and orphan logic of ``DiagnosticService`` is bound to the formal
application layout (project root, raw/markdown roots, link classification),
so the equivalent checks are re-implemented here in a simplified, test-only
form instead of being imported as a second "official" copy.

Schema notes (schema v4, see src/migrations.py and src/models.py): the FTS
table is ``page_search``; the document lifecycle column is
``documents.import_status`` with the value ``'processing'``
(``ImportStatus.PROCESSING``).

CLI example::

    python scripts/check_scale_consistency.py --database runtime/v023-scale/db/knowledge.db \
        --pages-dir runtime/v023-scale/pages --json runtime/v023-scale/consistency.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backup_service import BackupError, read_database_summary  # noqa: E402
from src.diagnostic_service import _walk_diagnostic_files  # noqa: E402

FORMAL_PROJECT_ROOT: Final[Path] = Path(r"D:\Projects\engineering-kb")
DETAIL_LIMIT: Final[int] = 20


class CheckStatus(StrEnum):
    """Outcome severity of one consistency check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ScaleCheck:
    """One read-only consistency check result."""

    key: str
    title: str
    status: CheckStatus
    summary: str
    details: tuple[str, ...] = ()


class TargetPathError(ValueError):
    """Raised when a CLI target is missing, unusable or formally protected."""


def run_checks(
    database: Path | str,
    pages_dir: Path | str,
    *,
    data_dir: Path | str | None = None,
    allow_processing: bool = False,
) -> tuple[ScaleCheck, ...]:
    """Run every read-only consistency check and return per-check results.

    Relative paths recorded in the database resolve against the parent of
    ``data_dir`` when given, otherwise against the database file's directory.
    """

    database_path = Path(database)
    pages_root = Path(pages_dir)
    _validate_targets(database_path, pages_root)
    base_root = (
        Path(data_dir).resolve(strict=False).parent
        if data_dir is not None
        else database_path.resolve(strict=False).parent
    )

    try:
        summary = read_database_summary(database_path)
    except BackupError as exc:
        return (
            ScaleCheck(
                "database_integrity",
                "数据库完整性",
                CheckStatus.FAIL,
                f"数据库不可读，全部检查中止：{exc}",
            ),
        )

    checks: list[ScaleCheck] = [
        ScaleCheck(
            "database_integrity",
            "数据库完整性",
            CheckStatus.PASS
            if summary.integrity_check == "ok"
            else CheckStatus.FAIL,
            f"PRAGMA integrity_check：{summary.integrity_check}",
        ),
        ScaleCheck(
            "foreign_keys",
            "外键完整性",
            CheckStatus.PASS
            if summary.foreign_key_violations == 0
            else CheckStatus.FAIL,
            f"外键违规 {summary.foreign_key_violations} 条。",
        ),
        ScaleCheck(
            "documents_count",
            "文档计数",
            CheckStatus.PASS,
            f"documents：{summary.documents} 行。",
        ),
        ScaleCheck(
            "pages_count",
            "页面计数",
            CheckStatus.PASS,
            f"pages：{summary.pages} 行。",
        ),
        ScaleCheck(
            "fts_count",
            "FTS 索引行数",
            CheckStatus.PASS if summary.fts == summary.pages else CheckStatus.FAIL,
            f"page_search：{summary.fts} 行，与页面 {summary.pages} 行一致。"
            if summary.fts == summary.pages
            else f"page_search {summary.fts} 行与页面 {summary.pages} 行不一致。",
        ),
    ]

    uri = f"file:{database_path.resolve(strict=False).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        documents = connection.execute(
            "SELECT id, title, source_path, sha256, page_count, import_status FROM documents"
        ).fetchall()
        pages = connection.execute(
            "SELECT id, document_id, page_number, image_path, markdown_path FROM pages"
        ).fetchall()
        mismatch_rows = connection.execute(
            """
            SELECT d.id, d.page_count, COUNT(p.id)
            FROM documents d LEFT JOIN pages p ON p.document_id = d.id
            GROUP BY d.id HAVING d.page_count != COUNT(p.id)
            """
        ).fetchall()
        duplicate_page_rows = connection.execute(
            """
            SELECT document_id, page_number, COUNT(*) AS copies
            FROM pages GROUP BY document_id, page_number HAVING copies > 1
            """
        ).fetchall()
        duplicate_hash_rows = connection.execute(
            """
            SELECT sha256, COUNT(*) AS copies
            FROM documents
            WHERE sha256 IS NOT NULL AND sha256 <> ''
            GROUP BY sha256 HAVING copies > 1
            """
        ).fetchall()

    checks.append(
        ScaleCheck(
            "document_page_counts",
            "文档页数一致性",
            CheckStatus.PASS if not mismatch_rows else CheckStatus.FAIL,
            "每个文档的 page_count 与实际页面行数一致。"
            if not mismatch_rows
            else f"发现 {len(mismatch_rows)} 个文档页数不一致。",
            tuple(
                f"文档 {document_id} 声明 {declared} 页，实际 {actual} 页"
                for document_id, declared, actual in mismatch_rows[:DETAIL_LIMIT]
            ),
        )
    )

    missing_pdfs = tuple(
        f"文档 {row[0]}《{row[1]}》：{row[2]}"
        for row in documents
        if not _resolve_recorded(str(row[2]), base_root).is_file()
    )
    checks.append(
        ScaleCheck(
            "source_pdfs",
            "原始 PDF 文件",
            CheckStatus.PASS if not missing_pdfs else CheckStatus.FAIL,
            "数据库引用的原始 PDF 均存在。"
            if not missing_pdfs
            else f"数据库引用中缺少 {len(missing_pdfs)} 个原始 PDF。",
            missing_pdfs[:DETAIL_LIMIT],
        )
    )

    bad_images: list[str] = []
    for page_id, document_id, page_number, image_path, _markdown_path in pages:
        resolved = _resolve_recorded(str(image_path), base_root)
        try:
            if not resolved.is_file() or resolved.stat().st_size == 0:
                bad_images.append(
                    f"页面 {page_id}（文档 {document_id} 第 {page_number} 页）：{image_path}"
                )
        except OSError:
            bad_images.append(
                f"页面 {page_id}（文档 {document_id} 第 {page_number} 页）：{image_path}"
            )
    checks.append(
        ScaleCheck(
            "page_images",
            "页面 PNG 文件",
            CheckStatus.PASS if not bad_images else CheckStatus.FAIL,
            "数据库引用的页面 PNG 均存在且非空。"
            if not bad_images
            else f"数据库引用中有 {len(bad_images)} 个页面 PNG 缺失或为空。",
            tuple(bad_images[:DETAIL_LIMIT]),
        )
    )

    missing_markdown = tuple(
        f"页面 {page_id}（文档 {document_id} 第 {page_number} 页）：{markdown_path}"
        for page_id, document_id, page_number, _image_path, markdown_path in pages
        if markdown_path
        and not _resolve_recorded(str(markdown_path), base_root).is_file()
    )
    checks.append(
        ScaleCheck(
            "markdown_files",
            "页面 Markdown 文件",
            CheckStatus.PASS if not missing_markdown else CheckStatus.FAIL,
            "数据库引用的 Markdown 文件均存在。"
            if not missing_markdown
            else f"数据库引用中缺少 {len(missing_markdown)} 个 Markdown 文件。",
            missing_markdown[:DETAIL_LIMIT],
        )
    )

    referenced_images = {
        _resolve_recorded(str(row[3]), base_root) for row in pages
    }
    orphans = tuple(
        sorted(
            candidate.resolve(strict=False).relative_to(pages_root.resolve(strict=False)).as_posix()
            for candidate, is_link in _walk_diagnostic_files(pages_root)
            if not is_link
            and candidate.suffix.lower() == ".png"
            and candidate.resolve(strict=False) not in referenced_images
        )
    )
    checks.append(
        ScaleCheck(
            "orphan_page_files",
            "孤儿页面文件",
            CheckStatus.PASS if not orphans else CheckStatus.WARN,
            "pages 目录中的 PNG 均被数据库引用。"
            if not orphans
            else f"发现 {len(orphans)} 个数据库未引用的 PNG（仅报告，不删除）。",
            orphans[:DETAIL_LIMIT],
        )
    )

    processing = tuple(
        f"文档 {row[0]}《{row[1]}》import_status=processing"
        for row in documents
        if str(row[5]) == "processing"
    )
    processing_status = CheckStatus.PASS
    if processing:
        processing_status = CheckStatus.WARN if allow_processing else CheckStatus.FAIL
    checks.append(
        ScaleCheck(
            "processing_documents",
            "永久悬挂 PROCESSING 状态",
            processing_status,
            "没有停留在 processing 状态的文档。"
            if not processing
            else f"发现 {len(processing)} 个文档停留在 processing 状态。",
            processing[:DETAIL_LIMIT],
        )
    )

    checks.append(
        ScaleCheck(
            "duplicate_page_numbers",
            "同文档重复页码",
            CheckStatus.PASS if not duplicate_page_rows else CheckStatus.FAIL,
            "同一文档内没有重复的 page_number。"
            if not duplicate_page_rows
            else f"发现 {len(duplicate_page_rows)} 组重复页码。",
            tuple(
                f"文档 {document_id} 第 {page_number} 页出现 {copies} 次"
                for document_id, page_number, copies in duplicate_page_rows[:DETAIL_LIMIT]
            ),
        )
    )
    checks.append(
        ScaleCheck(
            "duplicate_sha256",
            "文档 SHA-256 重复",
            CheckStatus.PASS if not duplicate_hash_rows else CheckStatus.FAIL,
            "documents.sha256 没有重复。"
            if not duplicate_hash_rows
            else f"发现 {len(duplicate_hash_rows)} 个重复的 SHA-256。",
            tuple(
                f"sha256 {str(sha256)[:16]}… 出现 {copies} 次"
                for sha256, copies in duplicate_hash_rows[:DETAIL_LIMIT]
            ),
        )
    )
    return tuple(checks)


def overall_status(checks: tuple[ScaleCheck, ...] | list[ScaleCheck]) -> CheckStatus:
    """Return the highest severity across all checks."""

    statuses = {check.status for check in checks}
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL
    if CheckStatus.WARN in statuses:
        return CheckStatus.WARN
    return CheckStatus.PASS


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser for the consistency checker."""

    parser = argparse.ArgumentParser(
        description="只读检查 v0.2.3 容量测试隔离库的一致性（不修复、不删除、不接触正式数据）。",
    )
    parser.add_argument("--database", required=True, type=Path, help="测试数据库路径（必填）")
    parser.add_argument("--pages-dir", required=True, type=Path, help="页面 PNG 目录（必填）")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="测试数据根目录；提供后，库内相对路径按其上级目录解析",
    )
    parser.add_argument(
        "--allow-processing",
        action="store_true",
        help="将 processing 状态文档降级为 WARN（用于中断恢复场景）",
    )
    parser.add_argument("--json", type=Path, default=None, help="机器可读结果输出路径（JSON）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: run checks, print PASS/WARN/FAIL lines, set exit code."""

    args = build_parser().parse_args(argv)
    try:
        if args.json is not None:
            _reject_formal_path(args.json)
        checks = run_checks(
            args.database,
            args.pages_dir,
            data_dir=args.data_dir,
            allow_processing=args.allow_processing,
        )
    except TargetPathError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    for check in checks:
        print(f"[{check.status}] {check.title}：{check.summary}")
        for detail in check.details:
            print(f"  - {detail}")
    overall = overall_status(checks)
    print(f"总体结果：{overall}")

    if args.json is not None:
        _write_json_report(args.json, args.database, args.pages_dir, checks, overall)
        print(f"JSON 结果：{args.json}")
    return 1 if overall is CheckStatus.FAIL else 0


def _reject_formal_path(path: Path) -> None:
    """Refuse an output target inside the formal data directory."""

    resolved = path.resolve(strict=False)
    formal = FORMAL_PROJECT_ROOT.resolve(strict=False)
    if resolved == formal or formal in resolved.parents:
        raise TargetPathError(f"拒绝写入正式数据目录：{resolved}")


def _validate_targets(database_path: Path, pages_root: Path) -> None:
    """Reject protected or unusable targets before any file is opened."""

    for label, path in (("数据库", database_path), ("pages 目录", pages_root)):
        resolved = path.resolve(strict=False)
        formal = FORMAL_PROJECT_ROOT.resolve(strict=False)
        if resolved == formal or formal in resolved.parents:
            raise TargetPathError(f"拒绝检查正式数据目录内的{label}：{resolved}")
    if not database_path.is_file():
        raise TargetPathError(f"数据库文件不存在：{database_path}")
    if not pages_root.is_dir():
        raise TargetPathError(f"pages 目录不存在：{pages_root}")


def _resolve_recorded(value: str, base_root: Path) -> Path:
    """Resolve a path recorded in the database against the test base root."""

    path = Path(value)
    if not path.is_absolute():
        path = base_root / path
    return path.resolve(strict=False)


def _write_json_report(
    json_path: Path,
    database: Path,
    pages_dir: Path,
    checks: tuple[ScaleCheck, ...],
    overall: CheckStatus,
) -> None:
    """Write the machine-readable report, creating parent directories."""

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": str(database),
        "pages_dir": str(pages_dir),
        "overall": str(overall),
        "checks": [
            {
                "key": check.key,
                "title": check.title,
                "status": str(check.status),
                "summary": check.summary,
                "details": list(check.details),
            }
            for check in checks
        ],
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
