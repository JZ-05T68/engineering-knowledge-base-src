"""Persistent vector recall over stored page embeddings (v0.5.0 Phase 8).

This module is the first **real** ``VectorRecallSource``: it reads fake or
real page vectors already persisted in the SQLite ``page_embeddings`` table
(schema v8), verifies each candidate against the current source-text
fingerprint, computes cosine similarity against an injected query vector,
and returns deterministic Top-K ``RankedHit`` candidates for the existing
``HybridSearchService`` RRF fusion.

Hard boundaries:

- This module **never generates page embeddings**. It only consumes the
  store; a missing embedding is simply missing (zero API cost contract).
- It does not know Qwen, endpoints, API keys, or HTTP. The query vector
  comes from the vendor-neutral ``EmbeddingProvider`` contract; tests
  inject fakes.
- A stored row is **not** automatically fresh: every candidate must match
  ``(page_id, source_text_sha256, model, dimensions, config_version)``
  against the *current* fingerprint source before it may be recalled.
- No similarity threshold is invented here; ranking is pure descending
  cosine with ``page_id`` ascending as the deterministic tie-break.

Degradation semantics (aligned with the Phase 6 hybrid boundary):

- query vector unavailable → ``AIUnavailableError`` propagates so the
  hybrid layer degrades to lexical-only with ``UNAVAILABLE``;
- query vector malformed (wrong count/length, non-finite, zero norm) →
  ``ValueError`` so the hybrid layer degrades with ``FAILED``;
- persistence read errors (``DatabaseError``) propagate untouched and are
  never disguised as AI unavailability;
- one invalid stored candidate (for example a zero vector) is skipped with
  a warning log, never silently mixed into the ranking and never allowed
  to take the whole recall down.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.ai.hybrid_search import PageHydrationSource
from src.ai.provider import EmbeddingProvider
from src.ai.retrieval import RankedHit
from src.models import PageEmbedding

__all__ = [
    "CurrentFingerprintSource",
    "PersistentVectorRecallSource",
    "SearchableContentFingerprintSource",
    "StoredPageEmbeddingSource",
    "VectorScoredHit",
    "cosine_similarity",
]

LOGGER = logging.getLogger(__name__)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two equal-length, non-zero vectors.

    Fail-closed: both vectors must be non-empty, equal length, finite in
    every component, and non-zero in norm. ``math.fsum`` keeps the result
    deterministic and platform-independent.
    """

    if len(a) != len(b):
        raise ValueError(f"向量维度不一致：{len(a)} vs {len(b)}")
    if not a:
        raise ValueError("向量不能为空")
    values_a = tuple(float(x) for x in a)
    values_b = tuple(float(x) for x in b)
    for value in (*values_a, *values_b):
        if not math.isfinite(value):
            raise ValueError(f"向量分量必须是有限数值：{value!r}")
    dot = math.fsum(x * y for x, y in zip(values_a, values_b, strict=True))
    norm_a = math.sqrt(math.fsum(x * x for x in values_a))
    norm_b = math.sqrt(math.fsum(y * y for y in values_b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("零向量无法计算余弦相似度")
    return dot / (norm_a * norm_b)


@dataclass(frozen=True, slots=True)
class VectorScoredHit:
    """One freshness-valid candidate with its raw similarity (debug/test view).

    The RRF kernel only ever receives ranks; raw cosine similarity stays on
    this observable surface for tests and future retrieval evaluation.
    """

    page_id: int
    similarity: float


@runtime_checkable
class StoredPageEmbeddingSource(Protocol):
    """Read-only listing of persisted embeddings for one configuration."""

    def list_page_embeddings(
        self,
        *,
        model: str,
        dimensions: int,
        config_version: int,
    ) -> Sequence[PageEmbedding]:
        """Return the current stored rows for one configuration.

        Being stored does **not** imply fresh; freshness is decided by the
        recall layer together with the current fingerprint source.
        """
        ...


@runtime_checkable
class CurrentFingerprintSource(Protocol):
    """Current source-text fingerprint of one page, for freshness checks."""

    def current_source_sha256(self, page_id: int) -> str | None:
        """Return the current fingerprint, or ``None`` when unavailable."""
        ...


class SearchableContentFingerprintSource:
    """Prototype fingerprint policy: SHA-256 of ``Page.searchable_content``.

    PROTOTYPE POLICY ONLY (offline fake-vector phase): page-level text,
    ``Page.searchable_content`` preference order, **no truncation**, no
    normalization beyond what the page record already carries. This maps to
    embedding ``config_version = 1``. It must not be quoted as the future
    production text-preparation policy — truncation/model-limit handling is
    deliberately deferred.
    """

    def __init__(self, pages: PageHydrationSource) -> None:
        self._pages = pages

    def current_source_sha256(self, page_id: int) -> str | None:
        """Hash the page's current searchable content; ``None`` if missing."""

        page = self._pages.get_page(page_id)
        if page is None:
            return None
        return hashlib.sha256(page.searchable_content.encode("utf-8")).hexdigest()


class PersistentVectorRecallSource:
    """Cosine Top-K recall over freshness-valid persisted page embeddings.

    Implements the Phase 5 ``VectorRecallSource`` protocol. All sources are
    injected: the query vector comes from a vendor-neutral
    ``EmbeddingProvider``, stored rows from a ``StoredPageEmbeddingSource``
    and current fingerprints from a ``CurrentFingerprintSource``.
    """

    def __init__(
        self,
        *,
        query_embedding: EmbeddingProvider,
        embeddings: StoredPageEmbeddingSource,
        fingerprints: CurrentFingerprintSource,
        model: str,
        dimensions: int,
        config_version: int,
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
        self._query_embedding = query_embedding
        self._embeddings = embeddings
        self._fingerprints = fingerprints
        self._model = model.strip()
        self._dimensions = dimensions
        self._config_version = config_version

    def recall(self, query: str, *, limit: int) -> tuple[RankedHit, ...]:
        """Return freshness-valid Top-K ranked hits for the hybrid fusion."""

        return tuple(
            RankedHit(page_id=hit.page_id, rank=rank)
            for rank, hit in enumerate(self.recall_scored(query, limit=limit), start=1)
        )

    def recall_scored(self, query: str, *, limit: int) -> tuple[VectorScoredHit, ...]:
        """Return freshness-valid Top-K candidates with raw similarities."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"limit 必须为正整数：{limit!r}")
        query_vector = self._query_vector(query)
        scored: list[VectorScoredHit] = []
        for stored in self._embeddings.list_page_embeddings(
            model=self._model,
            dimensions=self._dimensions,
            config_version=self._config_version,
        ):
            current = self._fingerprints.current_source_sha256(stored.page_id)
            if current is None or current != stored.source_text_sha256:
                continue
            try:
                similarity = cosine_similarity(query_vector, stored.vector)
            except ValueError as exc:
                LOGGER.warning(
                    "跳过非法的已存向量候选：page_id=%s（%s）", stored.page_id, exc
                )
                continue
            scored.append(
                VectorScoredHit(page_id=stored.page_id, similarity=similarity)
            )
        scored.sort(key=lambda hit: (-hit.similarity, hit.page_id))
        return tuple(scored[:limit])

    def _query_vector(self, query: str) -> tuple[float, ...]:
        """Embed the query via the injected provider, validating fail-closed."""

        result = self._query_embedding.embed(
            (query,), model=self._model, dimensions=self._dimensions
        )
        if len(result.embeddings) != 1:
            raise ValueError(
                f"query embedding 必须恰好返回 1 个向量：{len(result.embeddings)}"
            )
        vector = tuple(float(value) for value in result.embeddings[0])
        if len(vector) != self._dimensions:
            raise ValueError(
                f"query 向量维度 {len(vector)} 与配置 dimensions={self._dimensions} 不一致"
            )
        for value in vector:
            if not math.isfinite(value):
                raise ValueError(f"query 向量分量必须是有限数值：{value!r}")
        # Reject a zero-norm query here so every candidate is spared a
        # per-candidate divide-by-zero error path.
        if math.fsum(value * value for value in vector) == 0:
            raise ValueError("query 零向量无法计算余弦相似度")
        return vector
