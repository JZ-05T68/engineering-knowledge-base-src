"""Hybrid search integration: lexical results + vector recall through RRF.

This module sits **next to** the existing search chain, never inside it:

- ``Database.search`` and ``SearchService`` are not modified, not subclassed,
  and not re-implemented; the lexical side is consumed through the minimal
  ``LexicalSearch`` protocol and its result order is used directly as ranks
  (never re-derived from ``relevance_score``).
- The fusion itself stays in ``src.ai.retrieval`` (pure RRF kernel).
- The vector side is only the ``VectorRecallSource`` protocol; no real
  implementation exists yet, no embedding/HTTP/API key is touched here.
- Vector-only candidates are hydrated read-only through
  ``PageHydrationSource`` (single-page-by-id fetches only), so a page that
  never matched the LIKE recall gate can still become a citation-complete
  ``SearchResult``.

Degradation contract (AI is an optional enhancement, never a dependency):

- vector source missing / unavailable / failing / returning invalid hits
  all degrade to **lexical-only**, preserving the original lexical result
  identity, count and order exactly;
- the degradation is always observable via ``HybridSearchOutcome``'s
  ``vector_status`` — errors are never swallowed silently;
- a vector candidate whose ``page_id`` no longer exists is skipped
  (fail-closed for that candidate, never a fabricated ``SearchResult``)
  and counted in ``invalid_vector_candidates`` without taking the whole
  search down.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from src.ai.provider import AIError, AIUnavailableError
from src.ai.retrieval import DEFAULT_RRF_K, HybridHit, RankedHit, VectorRecallSource, rrf_fuse
from src.models import Document, Page, SearchResult

__all__ = [
    "HybridSearchOutcome",
    "HybridSearchResult",
    "HybridSearchService",
    "LexicalSearch",
    "PageHydrationSource",
    "VectorPathStatus",
]

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class LexicalSearch(Protocol):
    """The minimal lexical surface consumed by the hybrid integration."""

    def search(self, query: str, limit: int = 20) -> Sequence[SearchResult]:
        """Return the existing lexical results in their original order."""
        ...


@runtime_checkable
class PageHydrationSource(Protocol):
    """Read-only, single-identity page/document fetch for hydration."""

    def get_page(self, page_id: int) -> Page | None:
        """Return one page by its stable primary key, or ``None``."""
        ...

    def get_document(self, document_id: int) -> Document | None:
        """Return one document by its stable primary key, or ``None``."""
        ...


class VectorPathStatus(StrEnum):
    """Observable state of the vector recall path for one hybrid search."""

    DISABLED = "disabled"  # no VectorRecallSource configured at all
    OK = "ok"  # vector recall ran and returned candidates
    EMPTY = "empty"  # vector recall ran and returned no candidates
    UNAVAILABLE = "unavailable"  # AI not configured / provider unavailable
    FAILED = "failed"  # execution error or invalid vector output


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """One fused result carrying its full retrieval provenance."""

    result: SearchResult
    fused_score: float
    lexical_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True, slots=True)
class HybridSearchOutcome:
    """Hybrid results plus the observable vector-path state."""

    results: tuple[HybridSearchResult, ...]
    vector_status: VectorPathStatus
    invalid_vector_candidates: int = 0


def lexical_ranked_hits(results: Sequence[SearchResult]) -> tuple[RankedHit, ...]:
    """Adapt lexical results to ranked hits, order-preserving, 1-based."""

    return tuple(
        RankedHit(page_id=result.page_id, rank=rank)
        for rank, result in enumerate(results, start=1)
    )


class HybridSearchService:
    """Fuse lexical results with a vector recall source through RRF."""

    def __init__(
        self,
        *,
        lexical: LexicalSearch,
        hydration: PageHydrationSource,
        vector: VectorRecallSource | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
            raise ValueError(f"rrf_k 必须为正整数：{rrf_k!r}")
        self._lexical = lexical
        self._hydration = hydration
        self._vector = vector
        self._rrf_k = rrf_k

    def search(self, query: str, *, limit: int = 20) -> HybridSearchOutcome:
        """Run one hybrid search; the vector path can never break lexical."""

        lexical_results = list(self._lexical.search(query, limit))
        lexical_hits = lexical_ranked_hits(lexical_results)
        lexical_by_id = {result.page_id: result for result in lexical_results}

        vector_hits: tuple[RankedHit, ...] = ()
        vector_status = VectorPathStatus.OK
        if self._vector is None:
            vector_status = VectorPathStatus.DISABLED
        else:
            try:
                vector_hits = tuple(self._vector.recall(query, limit=limit))
                if not vector_hits:
                    vector_status = VectorPathStatus.EMPTY
            except AIUnavailableError as exc:
                LOGGER.info("向量召回不可用，退化为纯词面检索：%s", exc)
                vector_status = VectorPathStatus.UNAVAILABLE
            except (AIError, ValueError) as exc:
                LOGGER.warning("向量召回失败或输出非法，退化为纯词面检索：%s", exc)
                vector_status = VectorPathStatus.FAILED
            except Exception:
                LOGGER.exception("向量召回发生未预期错误，退化为纯词面检索")
                vector_status = VectorPathStatus.FAILED

        if vector_hits:
            # Validate the vector side alone: an empty lexical input can never
            # fail validation, so a ValueError here is provably a vector
            # contract violation (degrade), never a kernel/lexical bug.
            try:
                rrf_fuse((), vector_hits, k=self._rrf_k)
            except ValueError as exc:
                LOGGER.warning("向量候选未通过融合校验，退化为纯词面检索：%s", exc)
                vector_hits = ()
                vector_status = VectorPathStatus.FAILED

        # Deliberately NOT wrapped: lexical-adapter, kernel or configuration
        # errors (invalid k, invalid lexical hits) must surface, never degrade
        # silently into a lexical-only fallback.
        fused = rrf_fuse(lexical_hits, vector_hits, k=self._rrf_k)

        results: list[HybridSearchResult] = []
        invalid_candidates = 0
        for hit in fused:
            hydrated = self._hydrate(hit, lexical_by_id)
            if hydrated is None:
                invalid_candidates += 1
                continue
            results.append(hydrated)
        return HybridSearchOutcome(
            results=tuple(results),
            vector_status=vector_status,
            invalid_vector_candidates=invalid_candidates,
        )

    def _hydrate(
        self, hit: HybridHit, lexical_by_id: dict[int, SearchResult]
    ) -> HybridSearchResult | None:
        """Resolve one fused hit; lexical hits reuse the original result."""

        provenance = {
            "fused_score": hit.fused_score,
            "lexical_rank": hit.lexical_rank,
            "vector_rank": hit.vector_rank,
        }
        if hit.lexical_rank is not None:
            original = lexical_by_id.get(hit.page_id)
            if original is not None:
                return HybridSearchResult(result=original, **provenance)
        page = self._hydration.get_page(hit.page_id)
        if page is None:
            LOGGER.warning("跳过无法还原的向量候选：page_id=%s", hit.page_id)
            return None
        document = self._hydration.get_document(page.document_id)
        if document is None:
            LOGGER.warning(
                "跳过文档缺失的向量候选：page_id=%s document_id=%s",
                hit.page_id,
                page.document_id,
            )
            return None
        return HybridSearchResult(result=_search_result_from_page(page, document), **provenance)


def _search_result_from_page(page: Page, document: Document) -> SearchResult:
    """Build a citation-complete SearchResult for a vector-only candidate."""

    return SearchResult(
        page_id=page.id,
        document_id=page.document_id,
        document_title=document.title,
        filename=document.filename,
        page_number=page.page_number,
        image_path=page.image_path,
        content=page.searchable_content,
        snippet="",
        rank=0.0,
        status=page.status,
        match_type="语义召回",
        document_source_path=document.source_path,
        document_sha256=document.sha256,
        extracted_text=page.extracted_text,
        ocr_text=page.ocr_text,
        markdown_content=page.markdown_content,
        updated_at=page.updated_at,
    )
