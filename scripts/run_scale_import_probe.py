"""Scale-import probe for v0.2.3 S3A baselines (orchestration only).

Drives the *real* production services — ``DocumentService``/``PdfService``
import pipeline, ``Database`` queries, ``SearchService``, ``DiagnosticService``
and ``BackupService`` — against an explicitly isolated project root, recording
per-phase metrics through ``scripts/scale_metrics`` and per-phase facts as
JSONL.  The probe implements no import logic of its own and never touches the
formal data directory ``D:/Projects/engineering-kb``.

Every subcommand is a separate process invocation, so "restart the isolated
environment and re-read" is exercised by simply running the next command.

Typical sequence::

    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 \
        import --pdf runtime/v023-scale/pdfs/normal-50.pdf --expect-pages 50
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 status
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 \
        read-pages --pages 1,25,50 --expect-token NORMAL-50
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 \
        search --query "TOKEN NORMAL-50-000025" --expect-page 25
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 list-pages
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 diagnose
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 backup
    python scripts/run_scale_import_probe.py --root runtime/v023-scale/s3a/case50 consistency
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_scale_consistency import (  # noqa: E402
    CheckStatus,
    overall_status,
    run_checks,
)
from scripts.scale_metrics import (  # noqa: E402
    FormalPathError,
    ScaleMetricsCollector,
    directory_size_bytes,
)
from src.backup_service import BackupService, validate_backup  # noqa: E402
from src.config import Settings  # noqa: E402
from src.database import Database  # noqa: E402
from src.diagnostic_service import DiagnosticService  # noqa: E402
from src.document_service import DocumentService  # noqa: E402
from src.migrations import SCHEMA_VERSION  # noqa: E402
from src.pdf_service import PdfService  # noqa: E402
from src.search_service import SearchService  # noqa: E402

FORMAL_PROJECT_ROOT: Final[Path] = Path(r"D:\Projects\engineering-kb")
PROBE_PORT: Final[int] = 8502  # isolation range 8502-8512; never the formal 8501
RANDOM_SEED: Final[int] = 20260726


class ProbeError(RuntimeError):
    """A verification inside the probe failed (exit code 1)."""


class ProbeUsageError(ValueError):
    """Invalid probe invocation or protected target (exit code 2)."""


def _reject_formal_path(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    formal = FORMAL_PROJECT_ROOT.resolve(strict=False)
    if resolved == formal or formal in resolved.parents:
        raise ProbeUsageError(f"拒绝操作正式目录内的{label}：{resolved}")
    return resolved


def _build_settings(root: Path) -> Settings:
    """Assemble isolated settings, mirroring src/runtime.py wiring manually.

    ``get_settings()``/``application_*`` are deliberately not used: they pin
    the official endpoint and the formal data directory.  Every path field is
    overridden explicitly because Settings does not derive them from data_dir.
    """

    return Settings(
        _env_file=None,
        port=PROBE_PORT,
        data_dir=root / "data",
        raw_dir=root / "data" / "raw",
        pages_dir=root / "data" / "pages",
        markdown_dir=root / "data" / "markdown",
        database_dir=root / "data" / "database",
        database_path=root / "data" / "database" / "knowledge.db",
        backups_dir=root / "backups",
        logs_dir=root / "logs",
        log_path=root / "logs" / "probe.log",
        runtime_dir=root / "runtime",
        pid_path=root / "runtime" / "probe.pid.json",
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


class ProbeContext:
    """Shared plumbing: isolated settings, metric collection, result logging."""

    def __init__(self, root: Path, label: str) -> None:
        self.root = _reject_formal_path(root, "探针根目录")
        self.label = label
        self.settings = _build_settings(self.root)
        self.results_path = self.root / "probe-results.jsonl"
        _reject_formal_path(self.results_path, "探针结果文件")

    def metrics(self, phase: str) -> ScaleMetricsCollector:
        return ScaleMetricsCollector(
            f"{self.label}-{phase}",
            metrics_path=self.root / "metrics.jsonl",
            watch_dir=self.root,
        )

    def record(self, phase: str, **facts: Any) -> None:
        _append_jsonl(
            self.results_path,
            {"label": self.label, "phase": phase, "ts": time.time(), **facts},
        )

    def database(self) -> Database:
        return Database(self.settings.database_path)

    def document_service(self, database: Database) -> DocumentService:
        return DocumentService(
            database,
            self.settings.raw_dir,
            self.settings.pages_dir,
            self.settings.markdown_dir,
            pdf_service=PdfService(
                minimum_text_length=self.settings.minimum_text_length,
                dpi=self.settings.pdf_render_dpi,
            ),
            ocr_engine=None,
        )

    def single_document_id(self, database: Database) -> int:
        documents = database.list_documents()
        if not documents:
            raise ProbeError("隔离库中没有任何文档")
        if len(documents) > 1:
            raise ProbeUsageError("库中存在多个文档，请显式指定 --document-id")
        return int(documents[0].id)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ProbeError(message)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_import(context: ProbeContext, args: argparse.Namespace) -> int:
    pdf_path = _reject_formal_path(Path(args.pdf), "PDF 输入")
    _require(pdf_path.is_file(), f"PDF 不存在：{pdf_path}")
    import jieba  # local import: pay dictionary load once, before timing

    started = time.perf_counter()
    jieba.cut_for_search("probe-warmup")
    jieba_warmup = time.perf_counter() - started

    database = context.database()
    stats_before = database.dashboard_stats()
    progress_file = Path(args.progress_file) if args.progress_file else None
    if progress_file is not None:
        _reject_formal_path(progress_file, "进度文件")

    def _on_progress(done: int, total: int) -> None:
        if progress_file is not None:
            progress_file.write_text(f"{done}/{total}\n", encoding="utf-8")

    with context.metrics("import") as metrics:
        with pdf_path.open("rb") as stream:  # BinaryIO: no extra full copy
            result = context.document_service(database).import_pdf(
                stream,
                pdf_path.name,
                title=args.title,
                progress_callback=_on_progress,
            )
        metrics.finish(
            pdf_pages=len(result.pages), pdf_size_bytes=pdf_path.stat().st_size
        )

    document = result.document
    stats_after = database.dashboard_stats()
    diagnostics = result.diagnostics
    facts = {
        "pdf": str(pdf_path),
        "pdf_pages": len(result.pages),
        "pdf_size_bytes": pdf_path.stat().st_size,
        "duplicate": result.duplicate,
        "document_id": document.id,
        "import_status": str(document.import_status),
        "documents_before": stats_before.documents,
        "documents_after": stats_after.documents,
        "pages_before": stats_before.pages,
        "pages_after": stats_after.pages,
        "pages_delta": stats_after.pages - stats_before.pages,
        "diag_blank": len(diagnostics.blank_page_numbers),
        "diag_short": len(diagnostics.short_text_page_numbers),
        "diag_landscape": len(diagnostics.landscape_page_numbers),
        "diag_rotated": len(diagnostics.rotated_page_numbers),
        "diag_needs_review": len(diagnostics.needs_review_page_numbers),
        "diag_failed": len(diagnostics.failed_page_numbers),
        "jieba_warmup_seconds": round(jieba_warmup, 3),
    }
    context.record("import", **facts)
    print(f"导入完成：duplicate={result.duplicate} import_status={document.import_status}")
    print(
        f"页面：{facts['pages_before']} → {facts['pages_after']}（增量 {facts['pages_delta']}）"
    )
    print(
        "诊断：blank={diag_blank} short={diag_short} landscape={diag_landscape} "
        "rotated={diag_rotated} needs_review={diag_needs_review} failed={diag_failed}".format(
            **facts
        )
    )
    if args.expect_pages is not None and not result.duplicate:
        _require(
            len(result.pages) == args.expect_pages,
            f"导入页数 {len(result.pages)} 与期望 {args.expect_pages} 不一致",
        )
    if result.duplicate:
        _require(
            facts["pages_delta"] == 0 and facts["documents_after"] == facts["documents_before"],
            f"重复导入出现数据增量：{facts}",
        )
        print("重复导入零增量：通过。")
    return 0


def cmd_status(context: ProbeContext, _args: argparse.Namespace) -> int:
    from src.backup_service import read_database_summary

    database = context.database()
    stats = database.dashboard_stats()
    summary = read_database_summary(context.settings.database_path)
    png_count = sum(
        1 for path in context.settings.pages_dir.rglob("*.png") if path.is_file()
    )
    documents = [
        {"id": doc.id, "import_status": str(doc.import_status), "page_count": doc.page_count}
        for doc in database.list_documents()
    ]
    facts = {
        "documents": stats.documents,
        "pages": stats.pages,
        "fts": summary.fts,
        "png_files": png_count,
        "review_pending": stats.review_pages,
        "documents_detail": documents,
        "sizes": {
            "raw": directory_size_bytes(context.settings.raw_dir),
            "pages": directory_size_bytes(context.settings.pages_dir),
            "markdown": directory_size_bytes(context.settings.markdown_dir),
            "database": context.settings.database_path.stat().st_size,
            "backups": directory_size_bytes(context.settings.backups_dir),
        },
    }
    context.record("status", **facts)
    print(
        f"documents={stats.documents} pages={stats.pages} fts={summary.fts} "
        f"png={png_count} 待复核={stats.review_pages}"
    )
    for doc in documents:
        print(f"  文档 {doc['id']}：{doc['import_status']} page_count={doc['page_count']}")
    print(f"体积：{facts['sizes']}")
    return 0


def cmd_read_pages(context: ProbeContext, args: argparse.Namespace) -> int:
    database = context.database()
    document_id = args.document_id or context.single_document_id(database)
    total = database.dashboard_stats().pages
    wanted = [int(part) for part in args.pages.split(",") if part.strip()]
    if args.random > 0:
        rng = random.Random(RANDOM_SEED)
        wanted.extend(rng.sample(range(1, total + 1), min(args.random, total)))
    reads: list[dict[str, Any]] = []
    for page_number in wanted:
        started = time.perf_counter()
        page = database.get_page_by_number(document_id, page_number)
        elapsed = time.perf_counter() - started
        _require(page is not None, f"第 {page_number} 页读取失败（document {document_id}）")
        text = page.extracted_text or ""
        payload = len(text) + len(page.ocr_text or "") + len(page.markdown_content or "")
        token_ok = True
        if args.expect_token:
            token = f"TOKEN {args.expect_token}-{page_number:06d}"
            token_ok = token in text
        reads.append(
            {
                "page_number": page_number,
                "read_seconds": round(elapsed, 4),
                "payload_chars": payload,
                "token_ok": token_ok,
            }
        )
        _require(token_ok, f"第 {page_number} 页缺少期望 TOKEN")
    durations = [entry["read_seconds"] for entry in reads]
    context.record(
        "read_pages",
        document_id=document_id,
        reads=reads,
        count=len(reads),
        max_seconds=max(durations),
        avg_seconds=round(sum(durations) / len(durations), 4),
    )
    print(
        f"读取 {len(reads)} 页全部成功：max={max(durations)}s "
        f"avg={round(sum(durations) / len(durations), 4)}s"
    )
    return 0


def cmd_search(context: ProbeContext, args: argparse.Namespace) -> int:
    database = context.database()
    service = SearchService(database)
    started = time.perf_counter()
    hits = service.search(args.query, limit=args.limit)
    search_seconds = time.perf_counter() - started
    started = time.perf_counter()
    total = service.facet_counts(args.query).total
    facet_seconds = time.perf_counter() - started
    hit_pages = sorted({hit.page_number for hit in hits})
    truncated = total > len(hits)
    facts = {
        "query": args.query,
        "limit": args.limit,
        "returned": len(hits),
        "total_hits": total,
        "truncated_at_limit": truncated,
        "search_seconds": round(search_seconds, 4),
        "facet_seconds": round(facet_seconds, 4),
        "hit_pages_first20": hit_pages[:20],
    }
    context.record("search", **facts)
    print(
        f"搜索 {args.query!r}：返回 {len(hits)} 条 / 总命中 {total} 条 "
        f"（{'被 limit 截断' if truncated else '未截断'}），"
        f"search={facts['search_seconds']}s facet={facts['facet_seconds']}s"
    )
    if args.expect_page is not None:
        _require(
            args.expect_page in {hit.page_number for hit in hits},
            f"搜索结果中找不到第 {args.expect_page} 页",
        )
        print(f"目标页 {args.expect_page} 可定位：通过。")
    return 0


def cmd_list_pages(context: ProbeContext, args: argparse.Namespace) -> int:
    database = context.database()
    document_id = args.document_id or context.single_document_id(database)
    started = time.perf_counter()
    pages = database.list_pages(document_id)  # production API: full materialization
    elapsed = time.perf_counter() - started
    approx_chars = sum(
        len(page.extracted_text or "")
        + len(page.ocr_text or "")
        + len(page.markdown_content or "")
        for page in pages
    )
    context.record(
        "list_pages",
        document_id=document_id,
        count=len(pages),
        seconds=round(elapsed, 4),
        approx_payload_chars=approx_chars,
        materializes_full_text=True,
    )
    print(
        f"list_pages 返回 {len(pages)} 页，耗时 {round(elapsed, 4)}s，"
        f"物化正文约 {approx_chars} 字符"
    )
    return 0


def cmd_diagnose(context: ProbeContext, _args: argparse.Namespace) -> int:
    settings = context.settings
    service = DiagnosticService(
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
    with context.metrics("diagnose") as metrics:
        snapshot = service.run()
        metrics.finish()
    checks = [
        {"key": check.key, "status": str(check.status), "summary": check.summary}
        for check in snapshot.checks
    ]
    context.record(
        "diagnose", overall=str(snapshot.overall_status), checks=checks
    )
    print(f"诊断总体：{snapshot.overall_status}")
    for check in checks:
        print(f"  [{check['status']}] {check['key']}：{check['summary']}")
    return 0


def cmd_backup(context: ProbeContext, _args: argparse.Namespace) -> int:
    settings = context.settings
    service = BackupService(
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
    with context.metrics("backup") as metrics:
        backup = service.create_backup()
        metrics.finish()
    with context.metrics("restore-precheck") as metrics:
        validation = validate_backup(
            backup.backup_path,
            expected_app_version=settings.app_version,
            expected_schema_version=SCHEMA_VERSION,
        )
        metrics.finish()
    facts = {
        "backup_path": str(backup.backup_path),
        "backup_size_bytes": directory_size_bytes(backup.backup_path),
        "creation_seconds": round(backup.creation_seconds, 3),
        "verification_seconds": round(backup.verification_seconds, 3),
        "precheck_valid": validation.valid,
        "precheck_errors": list(validation.errors),
        "precheck_warnings": list(validation.warnings),
    }
    context.record("backup", **facts)
    print(
        f"备份完成：{backup.backup_path}（{facts['backup_size_bytes']} 字节，"
        f"创建 {facts['creation_seconds']}s + 自检 {facts['verification_seconds']}s）"
    )
    print(f"恢复预检：valid={validation.valid} errors={len(validation.errors)}")
    _require(validation.valid, f"恢复预检失败：{validation.errors}")
    return 0


def cmd_consistency(context: ProbeContext, args: argparse.Namespace) -> int:
    settings = context.settings
    checks = run_checks(
        settings.database_path,
        settings.pages_dir,
        data_dir=settings.data_dir,
        allow_processing=args.allow_processing,
    )
    overall = overall_status(checks)
    context.record(
        "consistency",
        overall=str(overall),
        checks=[{"key": c.key, "status": str(c.status)} for c in checks],
        invoked=True,
    )
    for check in checks:
        print(f"[{check.status}] {check.title}：{check.summary}")
    print(f"总体结果：{overall}")
    _require(overall is not CheckStatus.FAIL, "一致性检查存在 FAIL 项")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="v0.2.3 S3A 规模导入探针（复用生产服务的测试编排层，不接触正式目录）。"
    )
    parser.add_argument("--root", required=True, type=Path, help="隔离项目根目录（必填）")
    parser.add_argument("--label", default="probe", help="指标记录前缀（默认 probe）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="执行真实生产导入")
    import_parser.add_argument("--pdf", required=True, type=Path)
    import_parser.add_argument("--title", default=None)
    import_parser.add_argument("--expect-pages", type=int, default=None)
    import_parser.add_argument(
        "--progress-file",
        default=None,
        help="进度落盘文件（中断探测用，每次回调重写 done/total）",
    )
    import_parser.set_defaults(func=cmd_import)

    status_parser = subparsers.add_parser("status", help="输出库/文件计数与体积")
    status_parser.set_defaults(func=cmd_status)

    read_parser = subparsers.add_parser("read-pages", help="读取指定页与随机页")
    read_parser.add_argument("--document-id", type=int, default=None)
    read_parser.add_argument("--pages", default="", help="逗号分隔页号，如 1,25,50")
    read_parser.add_argument("--random", type=int, default=0, help="随机抽页数量")
    read_parser.add_argument("--expect-token", default=None, help="期望的 TOKEN 前缀")
    read_parser.set_defaults(func=cmd_read_pages)

    search_parser = subparsers.add_parser("search", help="搜索并记录命中口径")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--limit", type=int, default=100)
    search_parser.add_argument("--expect-page", type=int, default=None)
    search_parser.set_defaults(func=cmd_search)

    list_parser = subparsers.add_parser("list-pages", help="测量 list_pages 物化规模")
    list_parser.add_argument("--document-id", type=int, default=None)
    list_parser.set_defaults(func=cmd_list_pages)

    diagnose_parser = subparsers.add_parser("diagnose", help="运行完整生产诊断")
    diagnose_parser.set_defaults(func=cmd_diagnose)

    backup_parser = subparsers.add_parser("backup", help="创建隔离备份并恢复预检")
    backup_parser.set_defaults(func=cmd_backup)

    consistency_parser = subparsers.add_parser("consistency", help="只读一致性检查")
    consistency_parser.add_argument("--allow-processing", action="store_true")
    consistency_parser.set_defaults(func=cmd_consistency)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = ProbeContext(args.root, args.label)
        context.settings.ensure_directories()
        return int(args.func(context, args))
    except ProbeError as exc:
        print(f"探针验证失败：{exc}", file=sys.stderr)
        return 1
    except (ProbeUsageError, FormalPathError) as exc:
        print(f"探针用法错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # production failure surfaced as non-zero + record
        print(f"探针执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            context.record(
                "error", error_type=type(exc).__name__, error_message=str(exc)[:500]
            )
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
