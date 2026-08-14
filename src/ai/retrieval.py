"""Offline hybrid retrieval fusion kernel (v0.5.0 Phase 5 prototype).

This module proves one thing only: lexical ranked candidates and vector
ranked candidates can be merged into one stable, deterministic, explainable
candidate ordering by reciprocal rank fusion (RRF).

Hard boundaries:

- Pure Python, no I/O: no network, no database, no Streamlit, no AI
  provider imports, no filesystem access.
- The kernel only understands **stable page identity + rank**. It never
  sees raw scores (BM25, relevance_score, cosine similarity are
  deliberately excluded — Phase 4 established the two score scales are
  not directly additive), never sees ``SearchResult``, snippets, or
  citation fields.
- ``VectorRecallSource`` is a minimal protocol for a future vector recall
  path. No real implementation exists in this phase; only test fakes
  implement it. Candidates returned by any future real implementation
  must come from freshness-valid embeddings per the Phase 4 identity
  contract ``(page_id, source_text_sha256, model, dimensions,
  config_version)`` — freshness is intentionally not implemented here.

Failure semantics: invalid input is rejected with ``ValueError``
(fail-closed); nothing is silently corrected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_RRF_K",
    "HybridHit",
    "RankedHit",
    "VectorRecallSource",
    "rrf_fuse",
]

DEFAULT_RRF_K: Final = 60


@dataclass(frozen=True, slots=True)
class RankedHit:
    """One candidate from one retrieval source: page identity + 1-based rank."""

    page_id: int
    rank: int


@dataclass(frozen=True, slots=True)
class HybridHit:
    """One fused candidate with full per-source rank explainability."""

    page_id: int
    fused_score: float
    lexical_rank: int | None
    vector_rank: int | None

    @property
    def best_rank(self) -> int:
        """The best (smallest) rank this page achieved in any source."""

        ranks = [
            rank
            for rank in (self.lexical_rank, self.vector_rank)
            if rank is not None
        ]
        return min(ranks)

    @property
    def source_count(self) -> int:
        """How many retrieval sources recalled this page (1 or 2)."""

        return int(self.lexical_rank is not None) + int(self.vector_rank is not None)


@runtime_checkable
class VectorRecallSource(Protocol):
    """Minimal contract of a future vector recall path.

    Phase 5 ships no real implementation. A future implementation wraps a
    freshness-valid embedding store and returns 1-based ranked page hits;
    it must never leak raw similarity scores into the fusion kernel.
    """

    def recall(self, query: str, *, limit: int) -> tuple[RankedHit, ...]:
        """Return up to ``limit`` ranked page hits for ``query``."""
        ...


def _validate_hits(hits: Sequence[RankedHit], *, source: str) -> None:
    """Fail closed on invalid ranks, invalid ids, or duplicate pages."""

    seen: set[int] = set()
    for hit in hits:
        if not isinstance(hit, RankedHit):
            raise ValueError(f"{source} 候选必须是 RankedHit：{type(hit).__name__}")
        if isinstance(hit.page_id, bool) or not isinstance(hit.page_id, int):
            raise ValueError(f"{source} 候选 page_id 必须是整数：{hit.page_id!r}")
        if hit.page_id < 1:
            raise ValueError(f"{source} 候选 page_id 必须大于 0：{hit.page_id}")
        if isinstance(hit.rank, bool) or not isinstance(hit.rank, int):
            raise ValueError(f"{source} 候选 rank 必须是整数：{hit.rank!r}")
        if hit.rank < 1:
            raise ValueError(f"{source} 候选 rank 必须从 1 开始：{hit.rank}")
        if hit.page_id in seen:
            raise ValueError(f"{source} 候选存在重复 page_id：{hit.page_id}")
        seen.add(hit.page_id)


def rrf_fuse(
    lexical_hits: Sequence[RankedHit],
    vector_hits: Sequence[RankedHit],
    *,
    k: int = DEFAULT_RRF_K,
) -> tuple[HybridHit, ...]:
    """Fuse two ranked candidate lists with reciprocal rank fusion.

    ``RRF(page) = sum(1 / (k + rank))`` over the sources that recalled the
    page, with 1-based ranks. A page recalled by both sources accumulates
    both contributions; a page recalled by only one source is kept as-is
    (vector-only candidates prove the hybrid path can break through the
    LIKE-only recall gate).

    Ordering is deterministic and source-neutral:

    1. ``fused_score`` descending
    2. ``best_rank`` ascending (the page's best rank in any source)
    3. ``page_id`` ascending

    Identical input always produces identical output, and permuting the
    input sequences never changes the result.
    """

    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError(f"k 必须为正整数：{k!r}")
    _validate_hits(lexical_hits, source="lexical")
    _validate_hits(vector_hits, source="vector")

    lexical_ranks = {hit.page_id: hit.rank for hit in lexical_hits}
    vector_ranks = {hit.page_id: hit.rank for hit in vector_hits}
    fused: list[HybridHit] = []
    for page_id in lexical_ranks.keys() | vector_ranks.keys():
        score = 0.0
        lexical_rank = lexical_ranks.get(page_id)
        vector_rank = vector_ranks.get(page_id)
        if lexical_rank is not None:
            score += 1.0 / (k + lexical_rank)
        if vector_rank is not None:
            score += 1.0 / (k + vector_rank)
        fused.append(
            HybridHit(
                page_id=page_id,
                fused_score=score,
                lexical_rank=lexical_rank,
                vector_rank=vector_rank,
            )
        )
    return tuple(
        sorted(fused, key=lambda hit: (-hit.fused_score, hit.best_rank, hit.page_id))
    )
