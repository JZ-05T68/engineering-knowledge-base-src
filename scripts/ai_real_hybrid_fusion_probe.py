"""Controlled real hybrid fusion probe (v0.5.0 Phase 10D).

One frozen FTS-compatible query ``"定时器"`` is embedded once and run through
the existing ``PersistentVectorRecallSource`` -> ``HybridSearchService`` chain
to verify, for the first time with real data, that **both** the lexical and
the vector branch return candidates for the same query and that RRF fusion
accumulates both contributions correctly.

Hard rules (mirrors Phase 10C probe):

- Default (no flag, or ``--dry-run``) is plan-only: ZERO network, ZERO API.
- ``--confirm-paid-call`` plus ``--staging`` are both required for any real
  call: exactly one query, exactly one embedding request, at most one real
  HTTP request, ``max_extra_attempts = 0``, model ``qwen3.7-text-embedding``,
  dimensions 1024, config_version 1.
- The query is the frozen constant ``APPROVED_QUERY`` (``"定时器"`` including
  the double quotes — the FTS-compatible literal form). No CLI override.
- The whole path is read-only: no page embeddings are generated, no query
  embedding is persisted. The script snapshots staging and production before
  and after and treats any count/sha256 change as a stop condition.
- RRF math is verified explicitly per fused candidate:
  ``fused = 1/(60+lexical_rank) + 1/(60+vector_rank)`` (missing branch = 0).
- No LLM, no rerank.
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

#: The single human-approved frozen query of Phase 10D. Hard-coded on purpose.
#: Includes the double quotes: this is the FTS-compatible literal form that
#: ``Database.search`` requires (its ``_QUOTED_TERM`` extraction needs quoted
#: terms). Do NOT strip the quotes — that would silently change the experiment.
APPROVED_QUERY: Final[str] = '"定时器"'
QUERY_MODEL: Final[str] = "qwen3.7-text-embedding"
QUERY_DIMENSIONS: Final[int] = 1024
QUERY_CONFIG_VERSION: Final[int] = EMBEDDING_CONFIG_VERSION
REAL_EXTRA_ATTEMPTS: Final[int] = 0
RECALL_LIMIT: Final[int] = 20
RRF_K: Final[int] = 60

#: Frozen candidate classification from Phase 10C Step 4A approval.
CLASSIFICATION: Final[dict[int, str]] = {
    18: "strong positive",
    17: "near-positive",
    19: "hard negative",
    5: "clear negative",
    40: "clear negative",
}


def _production_database_path() -> Path:
    """Return the well-known production database path (read-only snapshots)."""

    return PROJECT_ROOT / "data" / "database" / "knowledge.db"


def _database_snapshot(database_path: Path) -> tuple[int, str]:
    """Return ``(page_embeddings count, file sha256)`` without any write."""

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


def _freshness_map(database: Database) -> dict[int, bool]:
    """Return ``{page_id: fresh}`` for every stored embedding (local only)."""

    result: dict[int, bool] = {}
    rows = database.list_page_embeddings(
        model=QUERY_MODEL,
        dimensions=QUERY_DIMENSIONS,
        config_version=QUERY_CONFIG_VERSION,
    )
    for row in rows:
        page = database.get_page(row.page_id)
        prepared = prepare_page_text(page.searchable_content) if page else None
        result[row.page_id] = prepared is not None and prepared.sha256 == row.source_text_sha256
    return result


def print_dry_run(database: Database) -> None:
    """Print the zero-cost plan for the selected instance."""

    staging_count, staging_hash = _database_snapshot(database.database_path)
    production_count, _ = _database_snapshot(_production_database_path())
    print("--- Phase 10D dry-run（0 API 调用） ---")
    print(f"exact query: {APPROVED_QUERY!r}（唯一获批 query，含引号的 FTS-compatible 字面形式）")
    print(f"model: {QUERY_MODEL}")
    print(f"dimensions: {QUERY_DIMENSIONS}")
    print(f"config_version: {QUERY_CONFIG_VERSION}")
    print(f"recall limit: {RECALL_LIMIT}")
    print(f"RRF k: {RRF_K}")
    print(f"stored page embeddings: {staging_count}")
    freshness = _freshness_map(database)
    for page_id in sorted(freshness):
        print(
            f"  page_id={page_id} classification={CLASSIFICATION.get(page_id, '?')} "
            f"fresh={'YES' if freshness[page_id] else 'NO'}"
        )
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

    vector recall and hybrid search both need the query vector; caching keeps
    the whole experiment at exactly one real HTTP request.
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
    database: Database,
    vector_hits: tuple[VectorScoredHit, ...],
    recording: _RecordingProvider,
    freshness: dict[int, bool],
) -> None:
    """Print the full 5-candidate standalone vector recall report."""

    print("--- Vector recall（独立报告，全部候选） ---")
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
        page = database.get_page(hit.page_id)
        classification = CLASSIFICATION.get(hit.page_id, "?")
        page_number = page.page_number if page else "?"
        fresh = freshness.get(hit.page_id, False)
        print(
            f"  vector rank {rank}: page_id={hit.page_id} page={page_number} "
            f"classification={classification} cosine_similarity={hit.similarity:.6f} "
            f"fresh={'YES' if fresh else 'NO'}"
        )
    if not vector_hits:
        print("  （无 freshness-valid 候选）")


def _rrf_expectation(
    lexical_rank: int | None, vector_rank: int | None
) -> tuple[float, float, float]:
    """Return ``(lexical_contribution, vector_contribution, expected_total)``."""

    lexical_contribution = (1.0 / (RRF_K + lexical_rank)) if lexical_rank is not None else 0.0
    vector_contribution = (1.0 / (RRF_K + vector_rank)) if vector_rank is not None else 0.0
    return lexical_contribution, vector_contribution, lexical_contribution + vector_contribution


def _print_hybrid_report(database: Database, outcome: HybridSearchOutcome) -> list[str]:
    """Print the hybrid outcome with explicit RRF math and citation metadata."""

    print("--- Hybrid retrieval（RRF 融合 + hydration） ---")
    print(f"vector_status: {outcome.vector_status}")
    print(f"invalid_vector_candidates: {outcome.invalid_vector_candidates}")
    print(f"fused result count: {len(outcome.results)}")
    mismatches: list[str] = []
    for index, item in enumerate(outcome.results, start=1):
        result = item.result
        lex_contribution, vec_contribution, expected = _rrf_expectation(
            item.lexical_rank, item.vector_rank
        )
        diff = abs(expected - item.fused_score)
        ok = diff < 1e-9
        if not ok:
            mismatches.append(
                f"page_id={result.page_id} expected={expected:.10f} actual={item.fused_score:.10f}"
            )
        print(
            f"  #{index}: page_id={result.page_id} "
            f"lexical_rank={item.lexical_rank} vector_rank={item.vector_rank} "
            f"lex_contrib={lex_contribution:.6f} vec_contrib={vec_contribution:.6f} "
            f"expected_total={expected:.6f} actual_fused_score={item.fused_score:.6f} "
            f"RRF_MATH={'PASS' if ok else 'FAIL'}"
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
    return mismatches


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
    freshness = _freshness_map(database)
    _print_vector_report(database, vector_hits, recording, freshness)
    rrf_mismatches = _print_hybrid_report(database, outcome)

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
    if rrf_mismatches:
        stop_reasons.append("RRF fused_score 与公式不一致：" + "; ".join(rrf_mismatches))
    if stop_reasons:
        print("--- STOP CONDITION HIT ---")
        for reason in stop_reasons:
            print(f"  {reason}")
        return 2
    print("--- 全部护栏通过：HTTP=1，双库 0 写入，维度一致，RRF 数学一致 ---")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EKB v0.5.0 受控真实 hybrid 融合探针（默认仅 dry-run）"
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
        help="使用完全隔离的 AI staging 实例（独立数据根目录，端口 8511）",
    )
    arguments = parser.parse_args(argv)

    # 硬护栏：真实付费调用只允许隔离的 staging 实例。
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
