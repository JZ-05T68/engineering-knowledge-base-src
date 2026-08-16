"""Controlled real query embedding probe (v0.5.0 Phase 10C Step 3).

First real use of ``qwen3.7-text-embedding`` on the **query side**: one
approved exact query is embedded once, compared by cosine similarity
against the real page embeddings already persisted in the staging
``page_embeddings`` table (schema v8), and run through the existing
``PersistentVectorRecallSource`` → ``HybridSearchService`` chain to verify
retrieval, hydration and citation metadata end to end.

Hard rules:

- Default (no flag, or ``--dry-run``) is plan-only: ZERO network, ZERO API.
- ``--confirm-paid-call`` plus ``--staging`` are both required for any real
  call, and even then: exactly one query, exactly one embedding request,
  at most one real HTTP request, ``max_extra_attempts = 0``, model
  ``qwen3.7-text-embedding``, dimensions 1024, config_version 1.
- The query is the approved constant ``APPROVED_QUERY``; there is no CLI
  option to change it, so the probe can never silently widen the experiment.
- The whole path is read-only: no page embeddings are generated, no query
  embedding is persisted, no search history exists to write. The script
  snapshots the staging and production databases before and after and
  treats any count change as a stop condition.
- No LLM, no rerank: the rerank channel is never touched (it raises
  ``AIUnavailableError`` by construction) and no chat endpoint exists on
  this path.

Usage::

    python scripts/ai_real_query_probe.py                      # dry-run plan
    python scripts/ai_real_query_probe.py --dry-run            # same
    python scripts/ai_real_query_probe.py --staging            # staging plan
    python scripts/ai_real_query_probe.py --confirm-paid-call --staging
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.hybrid_search import (  # noqa: E402
    HybridSearchOutcome,
    HybridSearchService,
)
from src.ai.page_indexer import EMBEDDING_CONFIG_VERSION, prepare_page_text  # noqa: E402
from src.ai.provider import AIError, EmbeddingResult  # noqa: E402
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.ai.vector_recall import (  # noqa: E402
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
    VectorScoredHit,
)
from src.config import (  # noqa: E402
    OfficialEndpointError,
    Settings,
    get_settings,
    staging_settings,
)
from src.database import Database  # noqa: E402

#: The single human-approved query of this controlled experiment. Hard-coded
#: on purpose: the probe must never retry with a different query.
APPROVED_QUERY: Final[str] = "定时器预分频器和自动重装载寄存器的作用"
QUERY_MODEL: Final[str] = "qwen3.7-text-embedding"
QUERY_DIMENSIONS: Final[int] = 1024
QUERY_CONFIG_VERSION: Final[int] = EMBEDDING_CONFIG_VERSION
REAL_EXTRA_ATTEMPTS: Final[int] = 0
RECALL_LIMIT: Final[int] = 20


def _production_database_path() -> Path:
    """Return the well-known production database path (read-only snapshots)."""

    return PROJECT_ROOT / "data" / "database" / "knowledge.db"


def _database_snapshot(database_path: Path) -> tuple[int, str]:
    """Return ``(page_embeddings count, file sha256)`` without any write.

    A raw read-only SQLite connection is used deliberately: instantiating
    ``Database`` could run schema migrations, which must never happen on a
    database this probe only observes.
    """

    if not database_path.exists():
        return -1, "MISSING"
    digest = hashlib.sha256(database_path.read_bytes()).hexdigest()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        count = connection.execute("SELECT COUNT(*) FROM page_embeddings").fetchone()[0]
    finally:
        connection.close()
    return int(count), digest


def check_guards(settings: Settings) -> list[str]:
    """Return every violated paid-call guard; empty means the call is allowed."""

    problems: list[str] = []
    if not APPROVED_QUERY.strip():
        problems.append("APPROVED_QUERY 不能为空")
    if settings.ai_mode != "api":
        problems.append(f'ai_mode 必须为 "api"（当前 {settings.ai_mode}）')
    if settings.ai_provider != "qwen":
        problems.append(f'ai_provider 必须为 "qwen"（当前 {settings.ai_provider}）')
    if not settings.ai_api_key.get_secret_value():
        problems.append("EKB_AI_API_KEY is not configured")
    if settings.ai_embedding_model != QUERY_MODEL:
        problems.append(
            f"ai_embedding_model 必须为 {QUERY_MODEL}"
            f"（当前 {settings.ai_embedding_model}）"
        )
    return problems


def _stored_row_lines(database: Database) -> list[str]:
    """Describe the stored embeddings and their freshness (local only)."""

    lines: list[str] = []
    rows = database.list_page_embeddings(
        model=QUERY_MODEL,
        dimensions=QUERY_DIMENSIONS,
        config_version=QUERY_CONFIG_VERSION,
    )
    for row in rows:
        page = database.get_page(row.page_id)
        prepared = prepare_page_text(page.searchable_content) if page else None
        current = prepared.sha256 if prepared else None
        fresh = current is not None and current == row.source_text_sha256
        title = "?"
        page_number = "?"
        if page is not None:
            document = database.get_document(page.document_id)
            title = document.title if document else "?"
            page_number = str(page.page_number)
        lines.append(
            f"  page_id={row.page_id} title={title!r} page={page_number} "
            f"dimensions={row.dimensions} fresh={'YES' if fresh else 'NO'}"
        )
    if not lines:
        lines.append("  （无存储向量）")
    return lines


def print_dry_run(database: Database) -> None:
    """Print the zero-cost plan for the selected instance."""

    staging_count, staging_hash = _database_snapshot(database.database_path)
    production_count, _ = _database_snapshot(_production_database_path())
    print("--- Phase 10C Step 3 dry-run（0 API 调用） ---")
    print(f"exact query: {APPROVED_QUERY!r}（唯一获批 query，不可更换）")
    print(f"model: {QUERY_MODEL}")
    print(f"dimensions: {QUERY_DIMENSIONS}")
    print(f"config_version: {QUERY_CONFIG_VERSION}")
    print(f"recall limit: {RECALL_LIMIT}")
    print(f"stored page embeddings: {staging_count}")
    for line in _stored_row_lines(database):
        print(line)
    print(f"production page_embeddings: {production_count}")
    print(f"instance db sha256: {staging_hash}")
    print("planned real HTTP requests: 1（单次 query embedding）")
    print("planned max_extra_attempts: 0")
    print("planned DB writes: 0（query 路径只读）")
    print("planned LLM / rerank calls: 0（此路径物理上不存在）")


class _RecordingProvider:
    """Script-level adapter counting real provider calls for the audit."""

    def __init__(self, inner: QwenProvider) -> None:
        self._inner = inner
        self.calls: list[tuple[tuple[str, ...], str | None, int | None]] = []
        self.last_result: EmbeddingResult | None = None

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        result = self._inner.embed(texts, model=model, dimensions=dimensions)
        self.calls.append((tuple(texts), model, dimensions))
        self.last_result = result
        return result


class _CachedQueryProvider:
    """Serve repeated identical embed requests from memory, never re-hitting HTTP.

    The probe reports the vector recall detail and the hybrid outcome from
    the same real code paths; both need the query vector. Caching the single
    approved query keeps the whole experiment at exactly one real HTTP
    request while every layer still runs its real logic.
    """

    def __init__(self, inner: _RecordingProvider) -> None:
        self._inner = inner
        self._cache: dict[tuple[tuple[str, ...], str | None, int | None], EmbeddingResult] = {}

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        key = (tuple(texts), model, dimensions)
        if key not in self._cache:
            self._cache[key] = self._inner.embed(texts, model=model, dimensions=dimensions)
        return self._cache[key]


def _print_pre_call_audit(settings: Settings, *, key_present: bool) -> None:
    """Print the pre-call audit. The API key itself is never shown."""

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    print("--- Pre-call safety audit ---")
    print(f"branch: {branch}")
    print(f"HEAD: {head}")
    print(f"ai_mode: {settings.ai_mode}")
    print(f"provider: {settings.ai_provider}")
    print(f"API Key present: {'YES' if key_present else 'NO'}")
    print(f"exact query: {APPROVED_QUERY!r}")
    print(f"embedding model: {QUERY_MODEL}")
    print(f"dimensions: {QUERY_DIMENSIONS}")
    print(f"config_version: {QUERY_CONFIG_VERSION}")
    print("input count: 1（single query）")
    print(f"max_extra_attempts: {REAL_EXTRA_ATTEMPTS}")
    print("estimated maximum real HTTP requests: 1")
    print("-----------------------------")


def _print_vector_report(
    vector_hits: tuple[VectorScoredHit, ...], recording: _RecordingProvider
) -> None:
    """Print the standalone vector recall report."""

    print("--- Vector recall（独立报告） ---")
    if recording.last_result is not None and recording.last_result.embeddings:
        print(f"query embedding dimensions: {len(recording.last_result.embeddings[0])}")
        print(f"returned model: {recording.last_result.model}")
        usage = recording.last_result.usage
        if usage is None:
            print("usage: unavailable")
        else:
            print(
                f"usage: prompt_tokens={usage.prompt_tokens} "
                f"total_tokens={usage.total_tokens}"
            )
    print(f"vector candidate count: {len(vector_hits)}")
    for rank, hit in enumerate(vector_hits, start=1):
        print(
            f"  vector rank {rank}: page_id={hit.page_id} "
            f"cosine_similarity={hit.similarity:.6f}"
        )
    if not vector_hits:
        print("  （无 freshness-valid 候选）")


def _print_hybrid_report(database: Database, outcome: HybridSearchOutcome) -> None:
    """Print the hybrid retrieval outcome with citation metadata."""

    print("--- Hybrid retrieval（RRF 融合 + hydration） ---")
    print(f"vector_status: {outcome.vector_status}")
    print(f"invalid_vector_candidates: {outcome.invalid_vector_candidates}")
    print(f"fused result count: {len(outcome.results)}")
    for index, item in enumerate(outcome.results, start=1):
        result = item.result
        print(
            f"  #{index}: page_id={result.page_id} "
            f"lexical_rank={item.lexical_rank} vector_rank={item.vector_rank} "
            f"fused_score={item.fused_score:.6f}"
        )
        print(
            f"      citation: title={result.document_title!r} "
            f"filename={result.filename!r} page={result.page_number} "
            f"status={result.status} match_type={result.match_type!r}"
        )
        print(f"      image_path={result.image_path}")
        print(f"      document_source_path={result.document_source_path}")
        print(f"      document_sha256={result.document_sha256}")
        print(f"      updated_at={result.updated_at}")


def run_paid(settings: Settings, database: Database) -> int:
    """Execute exactly one paid query embedding and verify the whole chain."""

    staging_path = database.database_path
    production_path = _production_database_path()
    before_staging = _database_snapshot(staging_path)
    before_production = _database_snapshot(production_path)
    print("--- DB snapshot BEFORE ---")
    print(f"staging page_embeddings: {before_staging[0]} (sha256 {before_staging[1]})")
    print(f"production page_embeddings: {before_production[0]}")

    key_present = bool(settings.ai_api_key.get_secret_value())
    _print_pre_call_audit(settings, key_present=key_present)

    recording = _RecordingProvider(
        QwenProvider(
            api_key=settings.ai_api_key.get_secret_value(),
            llm_model=settings.ai_llm_model,
            llm_model_hard=settings.ai_llm_model_hard,
            embedding_model=settings.ai_embedding_model,
            rerank_model=settings.ai_rerank_model,
            timeout_seconds=settings.ai_timeout_seconds,
            max_extra_attempts=REAL_EXTRA_ATTEMPTS,
            transport=urllib_transport,
        )
    )
    cached = _CachedQueryProvider(recording)
    recall = PersistentVectorRecallSource(
        query_embedding=cached,
        embeddings=database,
        fingerprints=SearchableContentFingerprintSource(database),
        model=QUERY_MODEL,
        dimensions=QUERY_DIMENSIONS,
        config_version=QUERY_CONFIG_VERSION,
    )
    hybrid = HybridSearchService(lexical=database, hydration=database, vector=recall)

    try:
        vector_hits = recall.recall_scored(APPROVED_QUERY, limit=RECALL_LIMIT)
        outcome = hybrid.search(APPROVED_QUERY, limit=RECALL_LIMIT)
    except AIError as exc:
        print(f"CALL FAIL: {type(exc).__name__}: {exc}")
        print("按成本护栏约定：不重试、不发起第二次请求。")
        return 1

    print("CALL PASS")
    print(f"real provider embed calls: {len(recording.calls)}")
    _print_vector_report(vector_hits, recording)
    _print_hybrid_report(database, outcome)

    after_staging = _database_snapshot(staging_path)
    after_production = _database_snapshot(production_path)
    print("--- DB snapshot AFTER ---")
    print(f"staging page_embeddings: {after_staging[0]} (sha256 {after_staging[1]})")
    print(f"production page_embeddings: {after_production[0]}")

    stop_reasons: list[str] = []
    if len(recording.calls) != 1:
        stop_reasons.append(f"真实 provider 调用次数 {len(recording.calls)} != 1")
    if after_staging[0] != before_staging[0]:
        stop_reasons.append(
            f"staging page_embeddings 发生变化：{before_staging[0]} → {after_staging[0]}"
        )
    if after_production[0] != before_production[0]:
        stop_reasons.append(
            f"production page_embeddings 发生变化：{before_production[0]} → {after_production[0]}"
        )
    if after_staging[1] != before_staging[1]:
        stop_reasons.append("staging 数据库文件 sha256 发生变化（疑似意外写入）")
    if after_production[1] != before_production[1]:
        stop_reasons.append("production 数据库文件 sha256 发生变化（疑似意外写入）")
    if recording.last_result is not None and recording.last_result.embeddings:
        if len(recording.last_result.embeddings[0]) != QUERY_DIMENSIONS:
            stop_reasons.append("query embedding 维度与配置不一致")
    if stop_reasons:
        print("--- STOP CONDITION HIT ---")
        for reason in stop_reasons:
            print(f"  {reason}")
        return 2
    print("--- 全部护栏通过：HTTP=1，双库 0 写入，维度一致 ---")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EKB v0.5.0 受控真实 query embedding 探针（默认仅 dry-run）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出探测计划（默认行为；0 API 调用）",
    )
    parser.add_argument(
        "--confirm-paid-call",
        action="store_true",
        help="确认执行恰好一次按量付费的真实 query embedding 请求",
    )
    parser.add_argument(
        "--staging",
        action="store_true",
        help="使用完全隔离的 AI staging 实例（独立数据根目录，端口 8502）",
    )
    arguments = parser.parse_args(argv)

    # 硬护栏：真实付费调用只允许隔离的 staging 实例。该检查先于任何
    # 配置读取、provider 装配、HTTP 或 embedding 调用。
    if arguments.confirm_paid_call and not arguments.staging:
        print("GUARD FAIL: 真实付费调用仅允许在隔离的 staging 实例执行"
              "（必须同时显式传入 --staging）。")
        print("未发起任何网络请求。")
        return 3

    if arguments.staging:
        settings = staging_settings()
        print(f"instance: staging（root={settings.data_dir.parent}，port={settings.port}）")
    else:
        try:
            settings = get_settings()
        except OfficialEndpointError as exc:
            print(f"配置错误：{exc}")
            return 3
        print(f"instance: production（db={settings.database_path.name}）")
    settings.ensure_directories()
    database = Database(settings.database_path)

    if not arguments.confirm_paid_call:
        print_dry_run(database)
        return 0

    problems = check_guards(settings)
    if problems:
        for problem in problems:
            print(f"GUARD FAIL: {problem}")
        print("未发起任何网络请求。")
        return 3
    return run_paid(settings, database)


if __name__ == "__main__":
    raise SystemExit(main())
