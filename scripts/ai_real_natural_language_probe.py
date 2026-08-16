"""Controlled real natural-language hybrid smoke (v0.5.0 Phase 10E-C).

This is the last controlled real call of Phase 10E. It proves that the
canonical runtime assembly ``application_hybrid_search_service()`` turns the
raw natural-language query ``定时器预分频器和自动重装载寄存器的作用`` into the
full chain ``normalization → lexical + vector → RRF → hydration`` with exactly
one real Qwen query-embedding HTTP request.

Hard rules (mirrored from the approval):

- Fresh process: ``EKB_STAGING_INSTANCE=1`` is set before ``src.runtime`` is
  imported or any ``application_*`` factory resolves, so the canonical
  ``runtime_settings()`` resolves to the isolated staging root.
- The probe uses the canonical factory only — it never re-assembles
  ``HybridSearchService`` by hand.
- ``--confirm-paid-call`` plus ``--staging`` are both required; otherwise it
  runs the full pre-call gate locally and STOPS before any network I/O.
- Exactly one real HTTP request; ``max_extra_attempts`` is forced to 0 on the
  already-assembled provider (transient-instrumentation only, no src change);
  a counting transport proves the HTTP total stays 1.
- No LLM, no rerank, no page-side embedding, no query persistence.
- Staging and production databases are snapshotted before/after; any count or
  sha256 change is a stop condition.
- The API key is only reported as present YES/NO, never printed.

Usage::

    python scripts/ai_real_natural_language_probe.py              # dry-run gates
    python scripts/ai_real_natural_language_probe.py --staging    # staging gates
    python scripts/ai_real_natural_language_probe.py --confirm-paid-call --staging
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Establish the staging runtime context in a fresh process BEFORE importing or
# resolving any canonical runtime service.
if os.environ.get("EKB_STAGING_INSTANCE") != "1":
    os.environ["EKB_STAGING_INSTANCE"] = "1"

from src.ai.page_indexer import (  # noqa: E402
    EMBEDDING_CONFIG_VERSION,
    EMBEDDING_DIMENSIONS,
    prepare_page_text,
)
from src.ai.provider import AIError  # noqa: E402
from src.ai.qwen_client import urllib_transport  # noqa: E402
from src.database import Database  # noqa: E402
from src.runtime import (  # noqa: E402
    application_database,
    application_hybrid_search_service,
    application_settings,
)
from src.search_service import SearchService  # noqa: E402

#: The single human-approved natural-language query. Hard-coded on purpose:
#: the probe must never retry with a different query, and must never normalize
#: it by hand — the canonical ``SearchService`` inside the factory owns that.
APPROVED_QUERY: Final[str] = "定时器预分频器和自动重装载寄存器的作用"
QUERY_MODEL: Final[str] = "qwen3.7-text-embedding"
QUERY_DIMENSIONS: Final[int] = EMBEDDING_DIMENSIONS
QUERY_CONFIG_VERSION: Final[int] = EMBEDDING_CONFIG_VERSION
EXPECTED_EMBEDDING_IDS: Final[tuple[int, ...]] = (5, 17, 18, 19, 40)
RECALL_LIMIT: Final[int] = 20
RRF_K: Final[int] = 60

STAGING_DB_PATH: Final[Path] = (
    PROJECT_ROOT / "staging-data" / "data" / "database" / "knowledge.db"
)
PRODUCTION_DB_PATH: Final[Path] = (
    PROJECT_ROOT / "data" / "database" / "knowledge.db"
)


def _database_snapshot(database_path: Path) -> tuple[int, str]:
    """Return ``(page_embeddings count, file sha256)`` with zero writes."""
    if not database_path.exists():
        return -1, "MISSING"
    digest = hashlib.sha256(database_path.read_bytes()).hexdigest()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        count = connection.execute("SELECT COUNT(*) FROM page_embeddings").fetchone()[0]
    finally:
        connection.close()
    return int(count), digest


class _CountingTransport:
    """Wrap the real transport to count wire calls without changing behavior."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url, headers, payload, timeout_seconds):
        self.calls += 1
        return urllib_transport(url, headers, payload, timeout_seconds)


def _gis() -> None:
    """Print git branch/HEAD (never the API key)."""
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--short"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    print(f"branch: {branch}")
    print(f"HEAD: {head}")
    print(f"working tree: {'dirty' if dirty else 'clean'}")


def _pre_call_gates() -> tuple[SearchService, Database, list[str]]:
    """Run every zero-cost pre-call gate; return the canonical lexical source +
    database plus the list of stop reasons (empty means clear to call)."""
    stop: list[str] = []

    settings = application_settings()
    database = application_database()

    # Staging identity gate: the resolved DB must be the staging DB, never
    # ``data/database/knowledge.db``.
    resolved = database.database_path.resolve()
    if resolved != STAGING_DB_PATH.resolve():
        stop.append(
            f"resolved DB 不是 staging DB：{resolved}（期望 {STAGING_DB_PATH}）"
        )

    staging_count, staging_hash = _database_snapshot(STAGING_DB_PATH)
    production_count, production_hash = _database_snapshot(PRODUCTION_DB_PATH)

    print("--- Runtime identity ---")
    print(f"staging mode: YES（EKB_STAGING_INSTANCE=1 -> port {settings.port}）")
    print(f"resolved database path: {database.database_path}")
    print("canonical factory used: YES（application_hybrid_search_service）")

    print("--- Staging pre-call gate ---")
    print(f"staging documents: {database.list_documents() and len(database.list_documents())}")
    print(f"staging page_embeddings count: {staging_count}")
    rows = database.list_page_embeddings(
        model=QUERY_MODEL, dimensions=QUERY_DIMENSIONS,
        config_version=QUERY_CONFIG_VERSION,
    )
    ids = tuple(sorted(row.page_id for row in rows))
    print(f"staging embedding page_ids: {list(ids)}")
    if ids != EXPECTED_EMBEDDING_IDS:
        stop.append(f"embedding IDs {list(ids)} != 期望 {list(EXPECTED_EMBEDDING_IDS)}")
    if len(rows) != 5:
        stop.append(f"staging embeddings {len(rows)} != 5")

    fresh_all = True
    for row in rows:
        page = database.get_page(row.page_id)
        prepared = prepare_page_text(page.searchable_content) if page else None
        current = prepared.sha256 if prepared else None
        fresh = current is not None and current == row.source_text_sha256
        if not fresh:
            fresh_all = False
        print(f"  page_id={row.page_id} dims={row.dimensions} "
              f"cfg={row.config_version} fresh={'YES' if fresh else 'NO'}")
    if not fresh_all:
        stop.append("存在非 fresh embedding")

    print("--- Production gate ---")
    print(f"production page_embeddings: {production_count}")
    print(f"production DB sha256: {production_hash[:16]}...")
    if production_count != 0:
        stop.append(f"production page_embeddings {production_count} != 0")

    print("--- Pre-call lexical proof（纯本地，无 HTTP） ---")
    lexical = SearchService(database)
    lexical_results = lexical.search(APPROVED_QUERY, limit=RECALL_LIMIT)
    print(f"lexical result count: {len(lexical_results)}")
    for rank, r in enumerate(lexical_results, start=1):
        print(f"  lexical rank {rank}: page_id={r.page_id} page={r.page_number}")
    if not lexical_results:
        stop.append("natural-language lexical pre-check 仍为空")

    return lexical, database, stop


def _rrf_expectation(lexical_rank, vector_rank):
    """Return ``(lex_contrib, vec_contrib, total)`` per the RRF k=60 rule."""
    lex = 1.0 / (RRF_K + lexical_rank) if lexical_rank is not None else 0.0
    vec = 1.0 / (RRF_K + vector_rank) if vector_rank is not None else 0.0
    return lex, vec, lex + vec


def _print_hybrid(database: Database, outcome) -> tuple[list[str], bool]:
    """Print hybrid results with RRF verification; return mismatches + both flag."""
    print("--- Hybrid result ---")
    print(f"vector_status: {outcome.vector_status}")
    print(f"invalid_vector_candidates: {outcome.invalid_vector_candidates}")
    print(f"fused result count: {len(outcome.results)}")
    mismatches: list[str] = []
    any_both = False
    for idx, item in enumerate(outcome.results, start=1):
        result = item.result
        lex_c, vec_c, expected = _rrf_expectation(item.lexical_rank, item.vector_rank)
        both = item.lexical_rank is not None and item.vector_rank is not None
        any_both = any_both or both
        if abs(item.fused_score - expected) > 1e-9:
            mismatches.append(
                f"page_id={item.page_id} fused={item.fused_score:.6f} "
                f"expected={expected:.6f}"
            )
        print(
            f"  #{idx}: page_id={result.page_id} lexical_rank={item.lexical_rank} "
            f"vector_rank={item.vector_rank} fused_score={item.fused_score:.6f} "
            f"status={'both' if both else 'single'} match_type={result.match_type!r}"
        )
        print(
            f"      citation: title={result.document_title!r} filename={result.filename!r} "
            f"page={result.page_number} status={result.status}"
        )
        print(f"      image_path={result.image_path}")
        print(f"      document_source_path={result.document_source_path}")
        print(f"      document_sha256={result.document_sha256}")
        print(f"      updated_at={result.updated_at}")
    return mismatches, any_both


def _run_paid() -> int:
    """Execute the single authorized real call through the canonical factory."""
    before_staging = _database_snapshot(STAGING_DB_PATH)
    before_production = _database_snapshot(PRODUCTION_DB_PATH)

    # Canonical assembly — the whole point of Phase 10E-C.
    service = application_hybrid_search_service()

    # Transient instrumentation on the already-assembled provider: force
    # max_extra_attempts=0 and count wire calls. This is not re-assembly.
    vector = service._vector
    provider = vector._query_embedding
    counting = _CountingTransport()
    provider._transport = counting
    provider._max_extra_attempts = 0

    settings = application_settings()
    key_present = bool(settings.ai_api_key.get_secret_value())
    print("--- Real-call authorization ---")
    print(f"API Key present: {'YES' if key_present else 'NO'}")
    print(f"provider: {settings.ai_provider}")
    print(f"model: {QUERY_MODEL}")
    print(f"dimensions: {QUERY_DIMENSIONS}")
    print("max_extra_attempts: 0（已覆写）")
    print(f"exact raw query: {APPROVED_QUERY!r}")

    try:
        outcome = service.search(APPROVED_QUERY, limit=RECALL_LIMIT)
    except AIError as exc:
        print(f"CALL FAIL: {type(exc).__name__}: {exc}")
        print("按成本护栏约定：不重试、不发起第二次请求。")
        return 1

    http_count = counting.calls
    print(f"CALL PASS; real HTTP requests: {http_count}")

    mismatches, any_both = _print_hybrid(application_database(), outcome)

    after_staging = _database_snapshot(STAGING_DB_PATH)
    after_production = _database_snapshot(PRODUCTION_DB_PATH)
    print("--- DB before/after ---")
    print(f"staging page_embeddings: {before_staging[0]} -> {after_staging[0]}")
    print(f"staging DB sha256 unchanged: {before_staging[1] == after_staging[1]}")
    print(f"production page_embeddings: {before_production[0]} -> {after_production[0]}")
    print(f"production DB sha256 unchanged: {before_production[1] == after_production[1]}")

    stop: list[str] = []
    if http_count != 1:
        stop.append(f"HTTP 总数 {http_count} != 1")
    if mismatches:
        stop.append(f"RRF 数学不一致：{mismatches}")
    if after_staging[0] != before_staging[0]:
        stop.append("staging page_embeddings 数量变化（疑似写入）")
    if after_staging[1] != before_staging[1]:
        stop.append("staging DB sha256 变化（疑似写入）")
    if after_production[0] != before_production[0]:
        stop.append("production page_embeddings 数量变化")
    if after_production[1] != before_production[1]:
        stop.append("production DB sha256 变化")
    if not any_both:
        stop.append("Hybrid 最终 lexical_rank 全部为 None（自然语言 lexical 未生效）")

    print("--- Call accounting ---")
    print(f"embedding HTTP: {http_count}")
    print("LLM: 0")
    print("rerank: 0")
    print("retry: 0")
    print("page-side embedding: 0")

    if stop:
        print("--- STOP CONDITION HIT ---")
        for reason in stop:
            print(f"  {reason}")
        return 2

    print("--- PHASE 10E-C VERDICT: PASS ---")
    print("自然语言 query 已通过 canonical runtime assembly 完成")
    print("  normalization → lexical + vector → RRF → hydration")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EKB v0.5.0 受控真实自然语言 hybrid smoke（默认仅本地 gates）"
    )
    parser.add_argument("--confirm-paid-call", action="store_true",
                        help="确认执行恰好一次真实 query embedding HTTP")
    parser.add_argument("--staging", action="store_true",
                        help="staging 隔离实例（本 probe 恒在 staging 运行）")
    args = parser.parse_args(argv)

    if args.confirm_paid_call and not args.staging:
        print("GUARD FAIL: 真实付费调用仅允许 staging（必须显式传 --staging）")
        print("未发起任何网络请求。")
        return 3

    _gis()
    _, database, stop = _pre_call_gates()
    if stop:
        print("--- PRE-CALL STOP ---")
        for reason in stop:
            print(f"  {reason}")
        return 2

    if not args.confirm_paid_call:
        print("--- dry-run（本地 gates 全通过；未发起任何网络请求） ---")
        return 0

    return _run_paid()


if __name__ == "__main__":
    raise SystemExit(main())
