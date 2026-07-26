"""T25 terminal-state invariant verification for v0.2.3 scale runs (read-only).

After every scale tier (300/1000/2000/3000 pages) this checker verifies the
28 terminal-state invariants of task T25 against one isolated probe library:
database integrity and counts, file-system consistency (raw PDFs, page PNGs,
Markdown), and content sampling (page tokens, re-extracted text, PNG
dimensions, token search).  The checker never repairs, deletes or writes to
the verified library: SQLite is opened with ``mode=ro`` + ``PRAGMA
query_only``, files are only read, and diagnostics run through the read-only
production ``DiagnosticService``.

Reuse notes: check result types come from ``scripts/check_scale_consistency``;
integrity/foreign-key/count facts come from
``src.backup_service.read_database_summary``; the directory walk reuses
``src.diagnostic_service._walk_diagnostic_files``; the PNG decodability
predicate mirrors the production ``pymupdf.Pixmap`` full-decode used by
``src.pdf_service.is_complete_png``; the token-search check reuses
``src.text_utils.extract_search_terms`` so the read-only FTS query matches the
production ``SearchService`` normalization; page-content expectations come
from the deterministic ``scripts/generate_scale_pdf`` generator; isolated
settings wiring is shared with ``scripts/run_scale_import_probe``.

CLI example::

    python scripts/check_scale_invariants.py --root runtime/v023-scale/s3c/case300 \
        --expect-pages 300 --document-id NORMAL-300 \
        --source-pdf runtime/v023-scale/s3c/pdfs/normal-300.pdf \
        --json runtime/v023-scale/s3c/case300/t25-invariants.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Final

import pymupdf

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_scale_consistency import (  # noqa: E402
    CheckStatus,
    ScaleCheck,
    overall_status,
)
from scripts.generate_scale_pdf import page_token  # noqa: E402
from scripts.run_scale_import_probe import (  # noqa: E402
    ProbeUsageError,
    _build_settings,
    _reject_formal_path,
)
from src.backup_service import BackupError, read_database_summary  # noqa: E402
from src.diagnostic_service import (  # noqa: E402
    DiagnosticService,
    DiagnosticStatus,
    _walk_diagnostic_files,
)
from src.models import ImportStatus  # noqa: E402
from src.text_utils import extract_search_terms  # noqa: E402

DETAIL_LIMIT: Final[int] = 20
DEFAULT_SAMPLE_INTERVAL: Final[int] = 50
TEMP_INFIX: Final[str] = ".tmp-"
SIDE_STEP_RAW_PATTERN: Final[re.Pattern[str]] = re.compile(r"_\d+\.pdf$")


class InvariantUsageError(ValueError):
    """Invalid checker invocation or protected target (exit code 2)."""


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _walk_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        candidate
        for candidate, is_link in _walk_diagnostic_files(root)
        if not is_link and candidate.is_file()
    ]


def _decode_png_dimensions(root: Path) -> tuple[dict[Path, tuple[int, int]], list[str]]:
    """Fully decode every PNG under ``root`` with PyMuPDF (production predicate).

    Returns a mapping of resolved path to ``(width, height)`` plus the list of
    files that failed the strict decode.
    """

    dimensions: dict[Path, tuple[int, int]] = {}
    broken: list[str] = []
    for candidate in _walk_files(root):
        if candidate.suffix.lower() != ".png":
            continue
        resolved = candidate.resolve(strict=False)
        try:
            if resolved.stat().st_size == 0:
                raise ValueError("0 字节 PNG")
            pixmap = pymupdf.Pixmap(str(resolved))
            if pixmap.width <= 0 or pixmap.height <= 0:
                raise ValueError(f"PNG 尺寸异常：{pixmap.width}x{pixmap.height}")
            dimensions[resolved] = (int(pixmap.width), int(pixmap.height))
        except Exception as exc:  # decode failure = invariant violation
            broken.append(f"{resolved}（{type(exc).__name__}: {exc}）")
    return dimensions, broken


def sample_page_numbers(expect_pages: int, interval: int) -> tuple[int, ...]:
    """Return first/last/fixed-interval page numbers for content sampling."""

    if expect_pages < 1 or interval < 1:
        raise InvariantUsageError(
            f"抽样参数必须为正整数：pages={expect_pages} interval={interval}"
        )
    numbers = {1, expect_pages}
    numbers.update(range(interval, expect_pages + 1, interval))
    return tuple(sorted(numbers))


def run_invariants(
    root: Path,
    *,
    expect_documents: int,
    expect_pages: int,
    document_id: str,
    source_pdf: Path,
    sample_interval: int = DEFAULT_SAMPLE_INTERVAL,
    allow_processing_records: int = 0,
) -> tuple[ScaleCheck, ...]:
    """Run all 28 T25 invariant checks against one isolated probe library."""

    root = _reject_formal_path(Path(root), "T25 核验根目录")
    source_pdf = _reject_formal_path(Path(source_pdf), "源 PDF")
    settings = _build_settings(root)
    database_path = settings.database_path
    if not database_path.is_file():
        raise InvariantUsageError(f"数据库文件不存在：{database_path}")
    if not source_pdf.is_file():
        raise InvariantUsageError(f"源 PDF 不存在：{source_pdf}")
    if expect_documents < 1 or expect_pages < 1:
        raise InvariantUsageError("期望文档数与页数必须为正整数")

    checks: list[ScaleCheck] = []

    def record(
        key: str,
        title: str,
        ok: bool,
        pass_summary: str,
        fail_summary: str,
        details: tuple[str, ...] = (),
    ) -> None:
        checks.append(
            ScaleCheck(
                key,
                title,
                CheckStatus.PASS if ok else CheckStatus.FAIL,
                pass_summary if ok else fail_summary,
                details[:DETAIL_LIMIT],
            )
        )

    # ------------------------------------------------------------------
    # Database facts (read-only).
    # ------------------------------------------------------------------
    try:
        summary = read_database_summary(database_path)
    except BackupError as exc:
        return (
            ScaleCheck(
                "t25_01_database_integrity",
                "数据库完整性",
                CheckStatus.FAIL,
                f"数据库不可读，全部检查中止：{exc}",
            ),
        )

    record(
        "t25_01_database_integrity",
        "数据库完整性",
        summary.integrity_check == "ok",
        f"PRAGMA integrity_check：{summary.integrity_check}。",
        f"PRAGMA integrity_check 非 ok：{summary.integrity_check}。",
    )
    record(
        "t25_02_foreign_keys",
        "外键完整性",
        summary.foreign_key_violations == 0,
        "PRAGMA foreign_key_check 无结果。",
        f"外键违规 {summary.foreign_key_violations} 条。",
    )

    uri = f"file:{database_path.resolve(strict=False).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        document_rows = connection.execute(
            "SELECT id, title, filename, source_path, sha256, page_count, import_status"
            " FROM documents ORDER BY id"
        ).fetchall()
        page_rows = connection.execute(
            "SELECT id, document_id, page_number, image_path, markdown_path,"
            " extracted_text, status, review_status FROM pages ORDER BY id"
        ).fetchall()
        import_rows = connection.execute(
            "SELECT id, filename, sha256, status, document_id, total_pages,"
            " processed_pages, failed_pages, finished_at FROM import_records ORDER BY id"
        ).fetchall()
        duplicate_hash_rows = connection.execute(
            "SELECT sha256, COUNT(*) AS copies FROM documents"
            " WHERE sha256 IS NOT NULL AND sha256 <> ''"
            " GROUP BY sha256 HAVING copies > 1"
        ).fetchall()
        duplicate_page_rows = connection.execute(
            "SELECT document_id, page_number, COUNT(*) AS copies FROM pages"
            " GROUP BY document_id, page_number HAVING copies > 1"
        ).fetchall()

    record(
        "t25_03_documents_count",
        "文档数量符合预期",
        len(document_rows) == expect_documents,
        f"documents={len(document_rows)}，符合预期 {expect_documents}。",
        f"documents={len(document_rows)}，与预期 {expect_documents} 不符。",
    )
    record(
        "t25_04_pages_count",
        "页面数量等于 PDF 页数",
        len(page_rows) == expect_pages,
        f"pages={len(page_rows)}，精确等于 PDF 页数 {expect_pages}。",
        f"pages={len(page_rows)}，与 PDF 页数 {expect_pages} 不符。",
    )
    record(
        "t25_05_fts_count",
        "FTS 行数与页面一致",
        summary.fts == len(page_rows),
        f"page_search={summary.fts} 行，与 pages={len(page_rows)} 一致。",
        f"page_search={summary.fts} 行与 pages={len(page_rows)} 不一致。",
    )

    continuity_details: list[str] = []
    for doc_id, _title, _filename, _source, _sha, page_count, _status in document_rows:
        numbers = sorted(int(row[2]) for row in page_rows if int(row[1]) == int(doc_id))
        expected_numbers = list(range(1, int(page_count) + 1))
        if numbers != expected_numbers:
            missing = sorted(set(expected_numbers) - set(numbers))
            duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
            continuity_details.append(
                f"文档 {doc_id}：声明 {page_count} 页，实际 {len(numbers)} 页，"
                f"缺失 {missing[:5]}，重复 {duplicated[:5]}"
            )
    record(
        "t25_06_page_numbers_continuous",
        "页码唯一且连续",
        not continuity_details,
        "每个文档的 page_number 均为 1..N 唯一连续。",
        f"发现 {len(continuity_details)} 个文档页码不唯一或不连续。",
        tuple(continuity_details),
    )
    record(
        "t25_07_duplicate_document_sha256",
        "无重复文档 SHA-256",
        not duplicate_hash_rows,
        "documents.sha256 无重复。",
        f"发现 {len(duplicate_hash_rows)} 个重复文档 SHA-256。",
        tuple(f"sha256 {str(row[0])[:16]}… 出现 {row[1]} 次" for row in duplicate_hash_rows),
    )
    record(
        "t25_08_duplicate_pages",
        "无重复页面",
        not duplicate_page_rows,
        "同一文档内无重复 page_number。",
        f"发现 {len(duplicate_page_rows)} 组重复页面。",
        tuple(
            f"文档 {row[0]} 第 {row[1]} 页出现 {row[2]} 次" for row in duplicate_page_rows
        ),
    )

    not_completed = tuple(
        f"文档 {row[0]}《{row[1]}》import_status={row[6]}"
        for row in document_rows
        if str(row[6]) != ImportStatus.COMPLETED.value
    )
    record(
        "t25_09_documents_completed",
        "文档终态为 completed",
        not not_completed,
        "全部文档 import_status=completed。",
        f"发现 {len(not_completed)} 个文档终态非 completed。",
        not_completed,
    )

    processing_documents = tuple(
        f"文档 {row[0]}《{row[1]}》import_status=processing"
        for row in document_rows
        if str(row[6]) == ImportStatus.PROCESSING.value
    )
    processing_records = tuple(
        row for row in import_rows if str(row[3]) == ImportStatus.PROCESSING.value
    )
    unexpected_records = tuple(
        f"导入记录 {row[0]}（{row[1]}）status=processing document_id={row[4]}"
        for row in processing_records
        if row[4] is not None
    )
    leftover_records = tuple(
        row for row in processing_records if row[4] is None
    )
    leftover_details: list[str] = list(unexpected_records)
    if len(leftover_records) > allow_processing_records:
        leftover_details.append(
            f"中断残留导入记录 {len(leftover_records)} 条，"
            f"超过允许值 {allow_processing_records}。"
        )
    record(
        "t25_10_no_unexpected_processing",
        "无非预期 processing",
        not processing_documents and not leftover_details,
        "无 processing 文档，无非预期 processing 导入记录。",
        f"processing 文档 {len(processing_documents)} 个，"
        f"非预期 processing 导入记录问题 {len(leftover_details)} 项。",
        processing_documents + tuple(leftover_details),
    )

    failed_states = {
        ImportStatus.FAILED.value,
        ImportStatus.PARTIALLY_COMPLETED.value,
    }
    failed_documents = tuple(
        f"文档 {row[0]}《{row[1]}》import_status={row[6]}"
        for row in document_rows
        if str(row[6]) in failed_states
    )
    failed_records = tuple(
        f"导入记录 {row[0]}（{row[1]}）status={row[3]}"
        for row in import_rows
        if str(row[3]) in failed_states
    )
    record(
        "t25_11_no_failed_states",
        "无 failed/partially_completed",
        not failed_documents and not failed_records,
        "文档与导入记录均无 failed/partially_completed。",
        f"失败态文档 {len(failed_documents)} 个，失败态导入记录 {len(failed_records)} 条。",
        failed_documents + failed_records,
    )

    record_mismatch: list[str] = []
    document_status_by_id = {int(row[0]): str(row[6]) for row in document_rows}
    for row in import_rows:
        (
            record_id,
            filename,
            _sha,
            status,
            doc_id,
            total_pages,
            processed_pages,
            failed_pages,
            finished_at,
        ) = row
        if doc_id is None:
            continue  # interrupted attempt: covered by t25_10
        problems: list[str] = []
        if str(status) != document_status_by_id.get(int(doc_id), "<missing>"):
            problems.append(
                f"状态 {status} 与文档 {doc_id} 状态 "
                f"{document_status_by_id.get(int(doc_id), '<missing>')} 不符"
            )
        if int(failed_pages) != 0:
            problems.append(f"failed_pages={failed_pages}")
        if int(total_pages) != expect_pages or int(processed_pages) != expect_pages:
            problems.append(
                f"total/processed={total_pages}/{processed_pages}，期望 {expect_pages}"
            )
        if finished_at is None:
            problems.append("finished_at 为空")
        if problems:
            record_mismatch.append(f"导入记录 {record_id}（{filename}）：{'; '.join(problems)}")
    record(
        "t25_12_import_records_consistent",
        "导入记录与文档结果相符",
        not record_mismatch,
        f"{sum(1 for row in import_rows if row[4] is not None)} 条已关联导入记录"
        "均与文档终态相符。",
        f"发现 {len(record_mismatch)} 条导入记录与文档结果不符。",
        tuple(record_mismatch),
    )

    # ------------------------------------------------------------------
    # File facts (read-only).
    # ------------------------------------------------------------------
    raw_files = _walk_files(settings.raw_dir)
    page_files = _walk_files(settings.pages_dir)
    markdown_files = _walk_files(settings.markdown_dir)
    resolved_raw = {path.resolve(strict=False) for path in raw_files}
    resolved_markdown = {path.resolve(strict=False) for path in markdown_files}

    referenced_raw: dict[int, Path] = {}
    missing_raw: list[str] = []
    for doc_id, title, _filename, source_path, _sha, _count, _status in document_rows:
        resolved = Path(str(source_path)).resolve(strict=False)
        referenced_raw[int(doc_id)] = resolved
        try:
            if not resolved.is_file() or resolved.stat().st_size == 0:
                missing_raw.append(f"文档 {doc_id}《{title}》：{resolved}")
        except OSError:
            missing_raw.append(f"文档 {doc_id}《{title}》：{resolved}")
    record(
        "t25_13_raw_pdfs_present",
        "原始 PDF 存在且非空",
        not missing_raw,
        "数据库引用的 raw PDF 均存在、为普通文件且非空。",
        f"数据库引用中 {len(missing_raw)} 个 raw PDF 缺失或为空。",
        tuple(missing_raw),
    )

    source_sha256 = _sha256_of_file(source_pdf)
    hash_mismatch: list[str] = []
    for doc_id, title, _filename, _source, sha256, _count, _status in document_rows:
        recorded = str(sha256)
        if recorded != source_sha256:
            hash_mismatch.append(
                f"文档 {doc_id}《{title}》：库内 sha256 {recorded[:16]}… "
                f"与源 PDF {source_sha256[:16]}… 不一致"
            )
            continue
        raw_path = referenced_raw.get(int(doc_id))
        if raw_path is not None and raw_path.is_file() and raw_path.stat().st_size > 0:
            disk_sha256 = _sha256_of_file(raw_path)
            if disk_sha256 != source_sha256:
                hash_mismatch.append(
                    f"文档 {doc_id}《{title}》：raw 文件 sha256 {disk_sha256[:16]}… "
                    f"与源 PDF {source_sha256[:16]}… 不一致"
                )
    record(
        "t25_14_raw_sha256_matches",
        "raw PDF SHA-256 与源一致",
        not hash_mismatch,
        "库内 sha256、raw 文件 SHA-256 与源 PDF 三方完全一致。",
        f"发现 {len(hash_mismatch)} 个 SHA-256 不一致。",
        tuple(hash_mismatch),
    )

    png_dimensions, undecodable_pngs = _decode_png_dimensions(settings.pages_dir)
    referenced_images: dict[int, Path] = {}
    missing_images: list[str] = []
    for page_id, doc_id, page_number, image_path, _md, _text, _s1, _s2 in page_rows:
        resolved = Path(str(image_path)).resolve(strict=False)
        referenced_images[int(page_id)] = resolved
        if resolved not in png_dimensions:
            missing_images.append(
                f"页面 {page_id}（文档 {doc_id} 第 {page_number} 页）：{resolved}"
            )
    record(
        "t25_15_referenced_pngs_decodable",
        "引用 PNG 存在非空可解码",
        not missing_images,
        "数据库引用的每个 PNG 均存在、非空、可由 PyMuPDF 解码且宽高大于 0。",
        f"数据库引用中 {len(missing_images)} 个 PNG 缺失、为空或不可解码。",
        tuple(missing_images),
    )
    record(
        "t25_16_png_count",
        "PNG 数量等于页数",
        len(png_dimensions) == expect_pages and not undecodable_pngs,
        f"pages 目录共 {len(png_dimensions)} 个可解码 PNG，等于页数 {expect_pages}。",
        f"pages 目录可解码 PNG {len(png_dimensions)} 个、不可解码 "
        f"{len(undecodable_pngs)} 个，与页数 {expect_pages} 不符。",
    )
    disk_pngs = set(png_dimensions)
    referenced_set = set(referenced_images.values())
    unmapped_records = sorted(str(path) for path in referenced_set - disk_pngs)
    unmapped_files = sorted(str(path) for path in disk_pngs - referenced_set)
    record(
        "t25_17_page_png_one_to_one",
        "页面记录与 PNG 一一对应",
        not unmapped_records and not unmapped_files,
        "页面记录与 pages 目录 PNG 文件一一对应。",
        f"记录侧无文件 {len(unmapped_records)} 个，文件侧无记录 {len(unmapped_files)} 个。",
        tuple(unmapped_records[:DETAIL_LIMIT] + unmapped_files[:DETAIL_LIMIT]),
    )

    formal_files = raw_files + page_files + markdown_files
    zero_byte = tuple(
        str(path)
        for path in formal_files
        if path.is_file() and path.stat().st_size == 0
    )
    record(
        "t25_18_no_zero_byte_files",
        "无 0 字节正式文件",
        not zero_byte,
        "raw/pages/markdown 目录无 0 字节文件。",
        f"发现 {len(zero_byte)} 个 0 字节正式文件。",
        zero_byte,
    )
    record(
        "t25_19_no_undecodable_pngs",
        "无不可解码正式 PNG",
        not undecodable_pngs,
        "pages 目录全部 PNG 均可由 PyMuPDF 完整解码。",
        f"发现 {len(undecodable_pngs)} 个不可解码 PNG。",
        tuple(undecodable_pngs),
    )
    temp_residue = tuple(
        str(path) for path in formal_files if TEMP_INFIX in path.name
    )
    record(
        "t25_20_no_temp_residue",
        "无 .tmp-* 残留",
        not temp_residue,
        "raw/pages/markdown 目录无 .tmp-* 临时残留。",
        f"发现 {len(temp_residue)} 个 .tmp-* 残留文件。",
        temp_residue,
    )
    side_step_files = tuple(
        str(path)
        for path in raw_files
        if SIDE_STEP_RAW_PATTERN.search(path.name)
    )
    record(
        "t25_21_no_side_step_raw",
        "无 _1/_2 raw 侧跳文件",
        not side_step_files,
        "raw 目录无 _1/_2 侧跳文件。",
        f"发现 {len(side_step_files)} 个 raw 侧跳文件。",
        side_step_files,
    )

    referenced_markdown = {
        Path(str(row[4])).resolve(strict=False) for row in page_rows if row[4]
    }
    orphan_raw = sorted(str(path) for path in resolved_raw - set(referenced_raw.values()))
    orphan_markdown = sorted(
        str(path) for path in resolved_markdown - referenced_markdown
    )
    orphan_details = tuple(orphan_raw + orphan_markdown + unmapped_files)
    record(
        "t25_22_no_orphan_files",
        "无未登记孤儿文件",
        not orphan_details,
        "raw/PNG/Markdown 文件均被数据库登记引用。",
        f"发现 {len(orphan_details)} 个未登记孤儿文件。",
        orphan_details,
    )
    missing_markdown = tuple(
        f"页面 {row[0]}（文档 {row[1]} 第 {row[2]} 页）：{row[4]}"
        for row in page_rows
        if row[4] and not Path(str(row[4])).resolve(strict=False).is_file()
    )
    missing_details = tuple(missing_raw + missing_images + list(missing_markdown))
    record(
        "t25_23_no_missing_files",
        "无缺失文件",
        not missing_details,
        "数据库引用的 raw/PNG/Markdown 文件无一缺失。",
        f"发现 {len(missing_details)} 个缺失文件。",
        missing_details[:DETAIL_LIMIT],
    )

    diagnostic = DiagnosticService(
        app_version=settings.app_version,
        project_root=PROJECT_ROOT,
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        logs_dir=settings.logs_dir,
        log_path=settings.log_path,
        host=settings.host,
        port=settings.port,
    )
    snapshot = diagnostic.run()
    error_checks = tuple(
        f"{check.key}：{check.summary}"
        for check in snapshot.checks
        if check.status == DiagnosticStatus.ERROR
    )
    warning_checks = tuple(
        f"{check.key}：{check.summary}"
        for check in snapshot.checks
        if check.status == DiagnosticStatus.WARNING
    )
    record(
        "t25_24_diagnostics_no_error",
        "诊断无非预期 ERROR",
        not error_checks,
        "生产诊断无 ERROR"
        + (f"（预期内 warning {len(warning_checks)} 项）。" if warning_checks else "。"),
        f"生产诊断出现 {len(error_checks)} 个 ERROR。",
        error_checks,
    )

    # ------------------------------------------------------------------
    # Content sampling (read-only).
    # ------------------------------------------------------------------
    samples = sample_page_numbers(expect_pages, sample_interval)
    pages_by_number = {int(row[2]): row for row in page_rows}
    token_failures: list[str] = []
    text_mismatch: list[str] = []
    dimension_mismatch: list[str] = []
    search_miss: list[str] = []
    with pymupdf.open(str(source_pdf)) as source_document, closing(
        sqlite3.connect(uri, uri=True, timeout=30.0)
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        for page_number in samples:
            row = pages_by_number.get(page_number)
            if row is None:
                token_failures.append(f"第 {page_number} 页在数据库中不存在")
                continue
            page_id, _doc_id, _num, image_path, _md, db_text, _s1, _s2 = row
            token = page_token(document_id, page_number)
            if token not in str(db_text):
                token_failures.append(f"第 {page_number} 页缺少页号标记 {token!r}")
            expected_text = (
                source_document[page_number - 1].get_text("text") or ""
            ).strip()
            if str(db_text) != expected_text:
                text_mismatch.append(
                    f"第 {page_number} 页：库内 {len(str(db_text))} 字符，"
                    f"源 PDF 重提取 {len(expected_text)} 字符"
                )
            scale = settings.pdf_render_dpi / 72
            fresh = source_document[page_number - 1].get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), alpha=False
            )
            actual = png_dimensions.get(
                Path(str(image_path)).resolve(strict=False)
            )
            if actual != (int(fresh.width), int(fresh.height)):
                dimension_mismatch.append(
                    f"第 {page_number} 页：PNG {actual}，"
                    f"重渲染 {(int(fresh.width), int(fresh.height))}"
                )
            terms = extract_search_terms(f"TOKEN {document_id}-{page_number:06d}")
            match = " ".join(f'"{term}"' for term in terms)
            hit_rows = connection.execute(
                "SELECT rowid FROM page_search WHERE page_search MATCH ?", (match,)
            ).fetchall()
            hit_ids = {int(hit[0]) for hit in hit_rows}
            if hit_ids != {int(page_id)}:
                search_miss.append(
                    f"第 {page_number} 页：标记词命中 {sorted(hit_ids)}，"
                    f"期望唯一命中页面 {page_id}"
                )
    record(
        "t25_25_page_tokens",
        "抽核页号文本正确",
        not token_failures,
        f"{len(samples)} 个抽样页（首/末/间隔 {sample_interval}）页号标记全部正确。",
        f"{len(token_failures)} 个抽样页页号标记不正确。",
        tuple(token_failures),
    )
    record(
        "t25_26_sampled_text_matches",
        "抽样文本与源 PDF 一致",
        not text_mismatch,
        f"{len(samples)} 个抽样页库内文本与源 PDF 确定性重提取逐字一致。",
        f"{len(text_mismatch)} 个抽样页文本与源 PDF 重提取不一致。",
        tuple(text_mismatch),
    )
    record(
        "t25_27_sampled_png_dimensions",
        "抽样 PNG 尺寸一致",
        not dimension_mismatch,
        f"{len(samples)} 个抽样页 PNG 尺寸与同参数重渲染一致。",
        f"{len(dimension_mismatch)} 个抽样页 PNG 尺寸与重渲染不一致。",
        tuple(dimension_mismatch),
    )
    record(
        "t25_28_token_search_hits",
        "标记词搜索命中抽样页",
        not search_miss,
        f"{len(samples)} 个抽样页固定标记词均唯一命中正确页面。",
        f"{len(search_miss)} 个抽样页标记词未正确命中。",
        tuple(search_miss),
    )
    return tuple(checks)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser for the T25 invariant checker."""

    parser = argparse.ArgumentParser(
        description="T25 终态不变量核验（只读）：数据库、文件与内容抽核共 28 项。",
    )
    parser.add_argument("--root", required=True, type=Path, help="隔离项目根目录（必填）")
    parser.add_argument("--expect-documents", type=int, default=1, help="期望文档数")
    parser.add_argument("--expect-pages", type=int, required=True, help="期望 PDF 页数")
    parser.add_argument("--document-id", required=True, help="生成器文档标识")
    parser.add_argument("--source-pdf", required=True, type=Path, help="源 PDF 路径")
    parser.add_argument(
        "--sample-interval",
        type=int,
        default=DEFAULT_SAMPLE_INTERVAL,
        help=f"内容抽样固定间隔（默认 {DEFAULT_SAMPLE_INTERVAL}）",
    )
    parser.add_argument(
        "--allow-processing-records",
        type=int,
        default=0,
        help="允许的中断残留 processing 导入记录数（默认 0，强杀恢复场景用 1）",
    )
    parser.add_argument("--json", type=Path, default=None, help="机器可读结果输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: run all 28 checks, print results, set exit code."""

    args = build_parser().parse_args(argv)
    try:
        if args.json is not None:
            _reject_formal_path(args.json, "结果 JSON")
        checks = run_invariants(
            args.root,
            expect_documents=args.expect_documents,
            expect_pages=args.expect_pages,
            document_id=args.document_id,
            source_pdf=args.source_pdf,
            sample_interval=args.sample_interval,
            allow_processing_records=args.allow_processing_records,
        )
    except (InvariantUsageError, ProbeUsageError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    for check in checks:
        print(f"[{check.status}] {check.key} {check.title}：{check.summary}")
        for detail in check.details:
            print(f"  - {detail}")
    overall = overall_status(checks)
    print(f"总体结果：{overall}（{len(checks)} 项）")

    if args.json is not None:
        payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "root": str(args.root),
            "expectations": {
                "documents": args.expect_documents,
                "pages": args.expect_pages,
                "document_id": args.document_id,
                "source_pdf": str(args.source_pdf),
                "sample_interval": args.sample_interval,
                "allow_processing_records": args.allow_processing_records,
            },
            "environment": {
                "python": sys.version.split()[0],
                "pymupdf": importlib_metadata.version("pymupdf"),
                "sqlite": sqlite3.sqlite_version,
            },
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
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"JSON 结果：{args.json}")
    return 1 if overall is CheckStatus.FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
