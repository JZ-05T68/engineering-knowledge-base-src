"""Page embedding indexing orchestration with an explicit cost guard.

v0.5.0 Phase 9: walk every local page, prepare its embedding text under a
fixed prototype policy, reuse freshness-valid stored vectors, and only send
missing/stale pages to the injected ``EmbeddingProvider`` in bounded
batches. The provider is vendor-neutral; tests use fakes and this module
never touches Qwen, HTTP, or an API key itself.

Text preparation policy (``config_version = 1``, PROTOTYPE, page-level):

- source: ``Page.searchable_content`` (reviewed Markdown → OCR → extracted);
- empty / whitespace-only pages are skipped, never embedded;
- the text is truncated to ``MAX_SOURCE_TEXT_CHARS`` characters — an
  explicit, recorded prototype limit, **not** a validated production
  truncation strategy for any real model context window;
- ``source_text_sha256`` is the SHA-256 of the exact UTF-8 text that would
  be embedded (i.e. after truncation), so the stored fingerprint always
  matches what the provider saw.

Cost guard:

- fresh pages cost zero provider calls; dry-run costs zero provider calls;
- batching is explicit and finite (``batch_size`` > 0), page ↔ vector
  order correspondence is validated, a wrong vector count fails the whole
  batch closed, and there are **no retries** — one call per batch, exactly
  counted in the report;
- a failed batch/page never rolls back or corrupts embeddings already
  written, and is never reported as success.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from src.ai.provider import AIError, AIUnavailableError, EmbeddingProvider
from src.database import Database
from src.models import Page

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EMBEDDING_CONFIG_VERSION",
    "MAX_SOURCE_TEXT_CHARS",
    "PageEmbeddingIndexer",
    "PageIndexFailure",
    "PageIndexPlan",
    "PageIndexReport",
    "PageIndexStatus",
    "PreparedPageText",
    "prepare_page_text",
]

LOGGER = logging.getLogger(__name__)

#: Prototype text-preparation version; bump on any policy change.
EMBEDDING_CONFIG_VERSION: Final = 1

#: Explicit prototype truncation limit in characters (not a production
#: model-context policy; real truncation/model-limit handling is deferred).
MAX_SOURCE_TEXT_CHARS: Final = 8000

#: Default provider batch size; always finite and validated positive.
#: Matches the official Qwen (Aliyun Bailian) ``qwen3.7-text-embedding``
#: boundary of at most 20 inputs per batch (verified against the vendor
#: documentation on 2026-08-15). This is an input-count limit only — it is
#: NOT a token-safety guarantee: 20 inputs can still exceed the model's
#: 128,000-token per-batch ceiling, so a per-request payload/token budget
#: guard remains deferred until the real provider wiring phase.
DEFAULT_BATCH_SIZE: Final = 20


@dataclass(frozen=True, slots=True)
class PreparedPageText:
    """The exact text destined for the embedding provider plus its hash."""

    text: str
    sha256: str
    truncated: bool


def prepare_page_text(content: str) -> PreparedPageText | None:
    """Apply the prototype preparation policy; ``None`` means skip (empty).

    The SHA-256 is computed over the final (possibly truncated) UTF-8 text,
    keeping the stored freshness fingerprint identical to the embedded text.
    """

    text = content.strip()
    if not text:
        return None
    truncated = len(text) > MAX_SOURCE_TEXT_CHARS
    if truncated:
        text = text[:MAX_SOURCE_TEXT_CHARS]
    return PreparedPageText(
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        truncated=truncated,
    )


class PageIndexStatus(StrEnum):
    """Per-page outcome of one indexing run."""

    INDEXED = "indexed"  # newly embedded and persisted in this run
    REUSED = "reused"  # fresh stored embedding, zero provider cost
    SKIPPED_EMPTY = "skipped_empty"  # no indexable text
    FAILED = "failed"  # attempted but not persisted; never faked as success


@dataclass(frozen=True, slots=True)
class PageIndexFailure:
    """One failed page with its machine-readable reason."""

    page_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class PageIndexPlan:
    """Dry-run statistics; computed without any provider call."""

    total: int
    reused: int
    missing: int
    stale: int
    skipped_empty: int

    @property
    def to_generate(self) -> int:
        """How many page embeddings an actual run would have to generate."""

        return self.missing + self.stale


@dataclass(frozen=True, slots=True)
class PageIndexReport:
    """Outcome of one real indexing run, with exact provider accounting."""

    total: int
    reused: int
    indexed: int
    failed: int
    skipped_empty: int
    provider_calls: int
    failures: tuple[PageIndexFailure, ...]


@dataclass(frozen=True, slots=True)
class _PageWork:
    """Internal per-page indexing decision."""

    page_id: int
    status: PageIndexStatus
    prepared: PreparedPageText | None


class PageEmbeddingIndexer:
    """Orchestrate page embedding generation with freshness-based reuse."""

    def __init__(
        self,
        *,
        database: Database,
        embedding: EmbeddingProvider,
        model: str,
        dimensions: int,
        config_version: int = EMBEDDING_CONFIG_VERSION,
        batch_size: int = DEFAULT_BATCH_SIZE,
        page_ids: Sequence[int] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("embedding model 不能为空")
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"dimensions 必须为正整数：{dimensions!r}")
        if (
            isinstance(config_version, bool)
            or not isinstance(config_version, int)
            or config_version <= 0
        ):
            raise ValueError(f"config_version 必须为正整数：{config_version!r}")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError(f"batch_size 必须为正整数：{batch_size!r}")
        allowed_ids: frozenset[int] | None = None
        if page_ids is not None:
            for value in page_ids:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"page_ids 必须为正整数：{value!r}")
            allowed_ids = frozenset(page_ids)
        self._database = database
        self._embedding = embedding
        self._model = model.strip()
        self._dimensions = dimensions
        self._config_version = config_version
        self._batch_size = batch_size
        self._page_ids = allowed_ids

    def plan_indexing(self) -> PageIndexPlan:
        """Dry-run: classify every page without touching the provider."""

        work = self._classify_pages()
        return PageIndexPlan(
            total=len(work),
            reused=sum(1 for item in work if item.status is PageIndexStatus.REUSED),
            missing=sum(
                1
                for item in work
                if item.status is PageIndexStatus.INDEXED
                and not self._has_stored(item.page_id)
            ),
            stale=sum(
                1
                for item in work
                if item.status is PageIndexStatus.INDEXED
                and self._has_stored(item.page_id)
            ),
            skipped_empty=sum(
                1 for item in work if item.status is PageIndexStatus.SKIPPED_EMPTY
            ),
        )

    def index_pages(self) -> PageIndexReport:
        """Embed only missing/stale pages in bounded batches and persist them.

        Each page's upsert commits independently, so a later batch failure
        never rolls back embeddings already written. No batch is retried:
        an ``AIUnavailableError`` aborts the run (remaining pending pages
        are marked failed without further calls), any other provider error
        fails only its own batch and the run continues with the next one.
        """

        work = self._classify_pages()
        outcomes: dict[int, PageIndexStatus] = {
            item.page_id: item.status for item in work
        }
        failures: list[PageIndexFailure] = []
        provider_calls = 0
        pending = [item for item in work if item.status is PageIndexStatus.INDEXED]
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            try:
                # pending 项必然带 prepared 文本（_classify_pages 不变量），
                # 文本与页严格一一对应，禁止静默过滤造成 page ↔ vector 错位。
                texts = tuple(
                    item.prepared.text
                    for item in batch
                    if item.prepared is not None
                )
                if len(texts) != len(batch):  # pragma: no cover - 防御不变量
                    raise ValueError("批次文本与页面数量不一致")
                # 精确计数每一次真实调用尝试（含失败），成本审计不允许漏记。
                provider_calls += 1
                result = self._embedding.embed(
                    texts,
                    model=self._model,
                    dimensions=self._dimensions,
                )
            except AIUnavailableError:
                for item in pending[start:]:
                    outcomes[item.page_id] = PageIndexStatus.FAILED
                    failures.append(
                        PageIndexFailure(page_id=item.page_id, reason="provider_unavailable")
                    )
                LOGGER.warning("embedding provider 不可用，本次 indexing 中止")
                break
            except AIError as exc:
                for item in batch:
                    outcomes[item.page_id] = PageIndexStatus.FAILED
                    failures.append(
                        PageIndexFailure(page_id=item.page_id, reason="provider_error")
                    )
                LOGGER.warning("embedding 批次失败（%d 页）：%s", len(batch), exc)
                continue
            vectors = result.embeddings
            if len(vectors) != len(batch):
                for item in batch:
                    outcomes[item.page_id] = PageIndexStatus.FAILED
                    failures.append(
                        PageIndexFailure(
                            page_id=item.page_id,
                            reason=f"vector_count_mismatch:{len(vectors)}!={len(batch)}",
                        )
                    )
                LOGGER.warning(
                    "embedding 返回数量 %d 与批次页数 %d 不一致，整批判失败",
                    len(vectors),
                    len(batch),
                )
                continue
            for item, vector in zip(batch, vectors, strict=True):
                error = self._persist(item, vector)
                if error is None:
                    outcomes[item.page_id] = PageIndexStatus.INDEXED
                else:
                    outcomes[item.page_id] = PageIndexStatus.FAILED
                    failures.append(PageIndexFailure(page_id=item.page_id, reason=error))
        return PageIndexReport(
            total=len(work),
            reused=sum(1 for s in outcomes.values() if s is PageIndexStatus.REUSED),
            indexed=sum(1 for s in outcomes.values() if s is PageIndexStatus.INDEXED),
            failed=sum(1 for s in outcomes.values() if s is PageIndexStatus.FAILED),
            skipped_empty=sum(
                1 for s in outcomes.values() if s is PageIndexStatus.SKIPPED_EMPTY
            ),
            provider_calls=provider_calls,
            failures=tuple(failures),
        )

    # ------------------------------------------------------------- internals
    def _iter_pages(self) -> list[Page]:
        """Enumerate pages deterministically (document id, page number).

        With a ``page_ids`` allowlist only those pages are considered, in
        the same deterministic order; unknown ids fail closed.
        """

        pages: list[Page] = []
        for document in self._database.list_documents():
            pages.extend(self._database.list_pages(document.id))
        if self._page_ids is None:
            return pages
        selected = [page for page in pages if page.id in self._page_ids]
        missing = sorted(self._page_ids - {page.id for page in selected})
        if missing:
            raise ValueError(f"page_ids 包含不存在的页面：{missing}")
        return selected

    def _classify_pages(self) -> list[_PageWork]:
        """Decide per page: skip empty, reuse fresh, or (re)generate."""

        work: list[_PageWork] = []
        for page in self._iter_pages():
            prepared = prepare_page_text(page.searchable_content)
            if prepared is None:
                work.append(
                    _PageWork(page_id=page.id, status=PageIndexStatus.SKIPPED_EMPTY, prepared=None)
                )
                continue
            fresh = self._database.get_fresh_page_embedding(
                page_id=page.id,
                source_text_sha256=prepared.sha256,
                model=self._model,
                dimensions=self._dimensions,
                config_version=self._config_version,
            )
            status = (
                PageIndexStatus.REUSED if fresh is not None else PageIndexStatus.INDEXED
            )
            work.append(_PageWork(page_id=page.id, status=status, prepared=prepared))
        return work

    def _has_stored(self, page_id: int) -> bool:
        """Whether a stored row exists for this page configuration (stale)."""

        return (
            self._database.get_page_embedding(
                page_id=page_id,
                model=self._model,
                dimensions=self._dimensions,
                config_version=self._config_version,
            )
            is not None
        )

    def _persist(self, item: _PageWork, vector: Sequence[float]) -> str | None:
        """Validate and upsert one vector; return a failure reason or None."""

        assert item.prepared is not None  # noqa: S101 - internal invariant
        values = tuple(float(value) for value in vector)
        if len(values) != self._dimensions:
            return f"vector_dimensions:{len(values)}!={self._dimensions}"
        if not all(math.isfinite(value) for value in values):
            return "vector_non_finite"
        self._database.upsert_page_embedding(
            page_id=item.page_id,
            source_text_sha256=item.prepared.sha256,
            model=self._model,
            dimensions=self._dimensions,
            config_version=self._config_version,
            vector=values,
        )
        return None
