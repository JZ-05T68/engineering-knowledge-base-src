"""Controlled real page embedding index build (v0.5.0 Phase 10).

First real use of ``qwen3.7-text-embedding`` against a tiny, deterministic
sample of real EKB pages, written through the Phase 9
``PageEmbeddingIndexer`` into the schema v8 ``page_embeddings`` table.

Hard rules:

- Default (no flag, or ``--dry-run``) is plan-only: ZERO network, ZERO API.
- ``--confirm-paid-call`` is required for any real call, and even then:
  at most 5 pages, exactly one batch, at most one real HTTP request,
  ``max_extra_attempts = 0``, model ``qwen3.7-text-embedding``,
  dimensions 1024, config_version 1.
- Only the selected pages' ``PreparedPageText.text`` is ever sent. No
  notes, evidence, prompts, PDFs, images, paths, or other pages. Page
  bodies are never printed to console/logs — metadata only.
- A tiny local heuristic skips pages whose text looks like credentials or
  private configuration; it is a safety heuristic, not a DLP system.

Usage::

    python scripts/ai_real_page_index.py                      # dry-run plan
    python scripts/ai_real_page_index.py --dry-run            # same
    python scripts/ai_real_page_index.py --confirm-paid-call  # one paid call
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.page_indexer import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    EMBEDDING_CONFIG_VERSION,
    MAX_SOURCE_TEXT_CHARS,
    PageEmbeddingIndexer,
    PreparedPageText,
    prepare_page_text,
)
from src.ai.provider import AIError, EmbeddingResult  # noqa: E402
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.config import (  # noqa: E402
    OfficialEndpointError,
    Settings,
    get_settings,
    staging_settings,
)
from src.database import Database  # noqa: E402

INDEX_MODEL: Final[str] = "qwen3.7-text-embedding"
INDEX_DIMENSIONS: Final[int] = 1024
INDEX_CONFIG_VERSION: Final[int] = EMBEDDING_CONFIG_VERSION
MAX_REAL_PAGES: Final[int] = 5
REAL_EXTRA_ATTEMPTS: Final[int] = 0

#: Minimal local heuristic markers for content that must never leave the
#: machine. Case-insensitive substring match; a safety heuristic, not DLP.
_SENSITIVE_MARKERS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "api key",
    "secret",
    "password",
    "passwd",
    "private key",
    "sk-",
)


@dataclass(frozen=True, slots=True)
class CandidatePage:
    """One selected page: metadata for reporting plus the prepared text."""

    page_id: int
    document_title: str
    page_number: int
    state: str  # "MISSING" or "STALE"
    prepared: PreparedPageText

    @property
    def char_count(self) -> int:
        return len(self.prepared.text)

    @property
    def truncated(self) -> bool:
        return self.prepared.truncated


def check_guards(settings: Settings) -> list[str]:
    """Return every violated paid-call guard; empty means the call is allowed."""

    problems: list[str] = []
    if settings.ai_mode != "api":
        problems.append(f'ai_mode 必须为 "api"（当前 {settings.ai_mode}）')
    if settings.ai_provider != "qwen":
        problems.append(f'ai_provider 必须为 "qwen"（当前 {settings.ai_provider}）')
    if not settings.ai_api_key.get_secret_value():
        problems.append("EKB_AI_API_KEY is not configured")
    if settings.ai_embedding_model != INDEX_MODEL:
        problems.append(
            f"ai_embedding_model 必须为 {INDEX_MODEL}"
            f"（当前 {settings.ai_embedding_model}）"
        )
    return problems


def _looks_sensitive(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def select_candidates(
    database: Database, *, limit: int = MAX_REAL_PAGES
) -> tuple[tuple[CandidatePage, ...], tuple[int, ...]]:
    """Pick the first ``limit`` non-fresh, non-empty pages deterministically.

    Selection follows the existing document/page order (document id, then
    page number) — never hand-picked. Pages whose text hits the sensitive
    heuristic are skipped and their ids reported separately. Fresh pages
    are never selected. Returns ``(candidates, sensitive_page_ids)``.
    """

    candidates: list[CandidatePage] = []
    sensitive: list[int] = []
    for document in database.list_documents():
        for page in database.list_pages(document.id):
            prepared = prepare_page_text(page.searchable_content)
            if prepared is None:
                continue
            if _looks_sensitive(prepared.text):
                sensitive.append(page.id)
                continue
            fresh = database.get_fresh_page_embedding(
                page_id=page.id,
                source_text_sha256=prepared.sha256,
                model=INDEX_MODEL,
                dimensions=INDEX_DIMENSIONS,
                config_version=INDEX_CONFIG_VERSION,
            )
            if fresh is not None:
                continue
            state = (
                "STALE"
                if database.get_page_embedding(
                    page_id=page.id,
                    model=INDEX_MODEL,
                    dimensions=INDEX_DIMENSIONS,
                    config_version=INDEX_CONFIG_VERSION,
                )
                is not None
                else "MISSING"
            )
            candidates.append(
                CandidatePage(
                    page_id=page.id,
                    document_title=document.title,
                    page_number=page.page_number,
                    state=state,
                    prepared=prepared,
                )
            )
            if len(candidates) >= limit:
                return tuple(candidates), tuple(sensitive)
    return tuple(candidates), tuple(sensitive)


def print_dry_run(database: Database) -> None:
    """Print the zero-cost plan for the formal database."""

    indexer = PageEmbeddingIndexer(
        database=database,
        embedding=_RefusingProvider(),
        model=INDEX_MODEL,
        dimensions=INDEX_DIMENSIONS,
        config_version=INDEX_CONFIG_VERSION,
    )
    plan = indexer.plan_indexing()
    existing_rows = len(
        database.list_page_embeddings(
            model=INDEX_MODEL,
            dimensions=INDEX_DIMENSIONS,
            config_version=INDEX_CONFIG_VERSION,
        )
    )
    candidates, sensitive = select_candidates(database)
    print("--- Phase 10 dry-run（0 API 调用） ---")
    print(f"model: {INDEX_MODEL}")
    print(f"dimensions: {INDEX_DIMENSIONS}")
    print(f"config_version: {INDEX_CONFIG_VERSION}")
    print(f"text max chars: {MAX_SOURCE_TEXT_CHARS}（prototype 截断，非模型 Token 策略）")
    print(f"default batch size: {DEFAULT_BATCH_SIZE}")
    print(f"total pages: {plan.total}")
    print(f"reused(fresh): {plan.reused}")
    print(f"missing: {plan.missing}")
    print(f"stale: {plan.stale}")
    print(f"skipped_empty: {plan.skipped_empty}")
    print(f"to_generate: {plan.to_generate}")
    print(f"existing embedding rows: {existing_rows}")
    if sensitive:
        print(f"sensitive-skipped page_ids（仅启发式，不上传）: {list(sensitive)}")
    if not candidates:
        print("proposed paid sample: EMPTY — Stage 2 将为 NO-OP，不得调用 API。")
        return
    batches = -(-len(candidates) // DEFAULT_BATCH_SIZE)
    print(f"proposed paid sample（最多 {MAX_REAL_PAGES} 页，仅 metadata）:")
    for candidate in candidates:
        print(
            f"  page_id={candidate.page_id} title={candidate.document_title!r} "
            f"page={candidate.page_number} chars={candidate.char_count} "
            f"truncated={'YES' if candidate.truncated else 'NO'} "
            f"state={candidate.state}"
        )
    print(f"planned embedding inputs: {len(candidates)}")
    print(f"planned batches: {batches}（<=5 页恒为 1）")
    print("planned real HTTP requests: 1")
    print("planned max_extra_attempts: 0")


class _RefusingProvider:
    """Dry-run placeholder: any embed call is a hard error."""

    def embed(self, texts: object, *, model: object = None, dimensions: object = None) -> None:
        raise AssertionError("dry-run 不得调用 embedding provider")


class _RecordingProvider:
    """Script-level adapter capturing the last result for the audit print."""

    def __init__(self, inner: QwenProvider) -> None:
        self._inner = inner
        self.last_result: EmbeddingResult | None = None

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        result = self._inner.embed(texts, model=model, dimensions=dimensions)
        self.last_result = result
        return result


def _print_pre_call_audit(settings: Settings, *, key_present: bool, inputs: int) -> None:
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
    print(f"embedding model: {INDEX_MODEL}")
    print(f"dimensions: {INDEX_DIMENSIONS}")
    print(f"config_version: {INDEX_CONFIG_VERSION}")
    print(f"input count: {inputs}（single batch）")
    print(f"max_extra_attempts: {REAL_EXTRA_ATTEMPTS}")
    print("estimated maximum real HTTP requests: 1")
    print("-----------------------------")


def run_paid(settings: Settings, database: Database) -> int:
    """Execute at most one paid batch call for the selected <=5 pages."""

    candidates, sensitive = select_candidates(database)
    if sensitive:
        print(f"sensitive-skipped page_ids（仅启发式，不上传）: {list(sensitive)}")
    if not candidates:
        print("NO-OP: 没有需要生成的页面 embedding，未发起任何网络请求。")
        return 0
    key_present = bool(settings.ai_api_key.get_secret_value())
    _print_pre_call_audit(settings, key_present=key_present, inputs=len(candidates))

    provider = _RecordingProvider(
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
    indexer = PageEmbeddingIndexer(
        database=database,
        embedding=provider,
        model=INDEX_MODEL,
        dimensions=INDEX_DIMENSIONS,
        config_version=INDEX_CONFIG_VERSION,
        batch_size=DEFAULT_BATCH_SIZE,
        page_ids=[candidate.page_id for candidate in candidates],
    )
    try:
        report = indexer.index_pages()
    except AIError as exc:
        print(f"CALL FAIL: {type(exc).__name__}: {exc}")
        print("按成本护栏约定：不重试、不发起第二次请求。")
        return 1

    print("CALL PASS")
    print(f"provider_calls: {report.provider_calls}")
    if provider.last_result is not None:
        print(f"returned model: {provider.last_result.model}")
        print(f"vector count: {len(provider.last_result.embeddings)}")
        if provider.last_result.embeddings:
            print(f"vector dimension: {len(provider.last_result.embeddings[0])}")
        usage = provider.last_result.usage
        if usage is None:
            print("usage: unavailable")
        else:
            print(
                f"usage: prompt_tokens={usage.prompt_tokens} "
                f"total_tokens={usage.total_tokens}"
            )
    print(
        f"indexed: {report.indexed} reused: {report.reused} "
        f"failed: {report.failed} skipped_empty: {report.skipped_empty}"
    )
    if report.failures:
        for failure in report.failures:
            print(f"  FAILED page_id={failure.page_id} reason={failure.reason}")
    # Post-index verification: pure re-read, zero additional API calls.
    verification = indexer.plan_indexing()
    print(
        f"post-index dry-run: total={verification.total} "
        f"reused={verification.reused} to_generate={verification.to_generate}"
    )
    return 0 if report.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EKB v0.5.0 受控真实页面 embedding 索引（默认仅 dry-run）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出索引计划（默认行为；0 API 调用）",
    )
    parser.add_argument(
        "--confirm-paid-call",
        action="store_true",
        help="确认执行至多一次按量付费的真实 batch embedding 请求",
    )
    parser.add_argument(
        "--staging",
        action="store_true",
        help="使用完全隔离的 AI staging 实例（独立数据根目录，端口 8511）",
    )
    arguments = parser.parse_args(argv)

    # 硬护栏：真实付费索引只允许隔离的 staging 实例。该检查先于任何
    # 配置读取、provider 装配、HTTP 或 embedding 调用。
    if arguments.confirm_paid_call and not arguments.staging:
        print("GUARD FAIL: 真实付费索引仅允许在隔离的 staging 实例执行"
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
