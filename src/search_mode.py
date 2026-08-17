"""Thin product-boundary helpers for the AI hybrid search mode.

This module owns the single product decision behind Phase 11B: whether one
search state may execute the hybrid path, and how a hybrid result's retrieval
provenance is labelled for the UI. It contains no Streamlit, no I/O, and no
ranking logic — it only encodes the declared filter/sort/labelling contract and
is fully unit-testable.

Rules (frozen in Phase 11B):

- Hybrid may run only when ``mode`` is ``HYBRID``, every ``SearchFilters``
  field is empty/default, and ``sort`` is ``RELEVANCE``. Any narrowing filter or
  a non-relevance sort silently falls back to keyword execution — never a
  post-filter over a hybrid top-k.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.ai.hybrid_search import HybridSearchOutcome, VectorPathStatus
from src.models import SearchFilters, SearchMode, SearchSort


def hybrid_is_allowed(
    mode: SearchMode,
    filters: SearchFilters,
    sort: SearchSort,
) -> bool:
    """Return whether the hybrid path is permitted for this exact state.

    ``HYBRID`` is only honoured with empty/default filters and relevance sort;
    any other combination is keyword-only. This is a pure product gate, never a
    post-filter, and mirrors the Phase 11B frozen contract verbatim.
    """

    if mode is not SearchMode.HYBRID:
        return False
    if sort is not SearchSort.RELEVANCE:
        return False
    return not (
        filters.document_ids
        or filters.project_ids
        or filters.tag_ids
        or filters.statuses
        or filters.match_fields
        or filters.has_note
        or filters.evidence_basket_id is not None
    )


def result_source(*, lexical_rank: int | None, vector_rank: int | None) -> str:
    """Return the lightweight user-facing source label for one hybrid hit.

    Never exposes raw ranks, cosine similarity, or RRF scores — only the coarse
    "关键词匹配 / 语义召回 / 混合匹配" semantics the product allows.
    """

    if lexical_rank is not None and vector_rank is not None:
        return "混合匹配"
    if vector_rank is not None:
        return "语义召回"
    return "关键词匹配"


def hybrid_status_note(vector_status: VectorPathStatus) -> str:
    """Return the lightweight degradation note for a hybrid outcome status."""

    if vector_status is VectorPathStatus.DISABLED:
        return "AI 混合检索未启用，当前使用关键词检索"
    if vector_status is VectorPathStatus.UNAVAILABLE:
        return "语义检索暂不可用，已保留关键词结果"
    if vector_status is VectorPathStatus.FAILED:
        return "语义检索暂时失败，已保留关键词结果"
    return ""


def hybrid_gate_fallback_note(
    mode: SearchMode,
    filters: SearchFilters,
    sort: SearchSort,
) -> str:
    """Return the per-search notice for a gate-forced keyword execution.

    Only non-empty when the user explicitly selected ``HYBRID`` but the
    filter/sort gate forced this particular search onto the keyword path;
    pure keyword users and gate-clean hybrid states get an empty string.
    """

    if mode is SearchMode.HYBRID and not hybrid_is_allowed(mode, filters, sort):
        return "当前筛选或排序条件不支持 AI 混合检索，本次搜索已使用关键词检索。"
    return ""


def hybrid_vector_active(vector_status: VectorPathStatus) -> bool:
    """Return whether a hybrid outcome actually used the vector recall path.

    ``OK`` and ``EMPTY`` both mean the vector path ran; ``DISABLED``,
    ``UNAVAILABLE`` and ``FAILED`` mean the executed result set is purely
    lexical, so the UI must present it as a keyword result set.
    """

    return vector_status in (VectorPathStatus.OK, VectorPathStatus.EMPTY)


def provenance_from_outcome(
    outcome: HybridSearchOutcome,
) -> dict[int, tuple[int | None, int | None]]:
    """Extract ``page_id -> (lexical_rank, vector_rank)`` provenance from an outcome."""

    return {
        hit.result.page_id: (hit.lexical_rank, hit.vector_rank)
        for hit in outcome.results
    }


def result_source_for(
    page_id: int,
    provenance: Mapping[int, tuple[int | None, int | None]],
) -> str:
    """Return the source label for one page given its hybrid provenance map."""

    lexical_rank, vector_rank = provenance.get(page_id, (None, None))
    return result_source(lexical_rank=lexical_rank, vector_rank=vector_rank)


def auto_execute_allowed(
    mode: SearchMode,
    filters: SearchFilters,
    sort: SearchSort,
    *,
    already_executed: bool,
) -> bool:
    """Whether a URL/state restore may auto-run a search without an explicit click.

    Only a **free** keyword search is ever auto-executed (preserving the legacy
    deep-link / browser-history behaviour). A hybrid state is never auto-run:
    an implicit paid embedding from a URL navigation is forbidden — it must be
    an explicit user click. An already-executed state is never re-run at all.
    """

    if already_executed:
        return False
    return not hybrid_is_allowed(mode, filters, sort)


__all__ = [
    "auto_execute_allowed",
    "hybrid_gate_fallback_note",
    "hybrid_is_allowed",
    "hybrid_status_note",
    "hybrid_vector_active",
    "provenance_from_outcome",
    "result_source",
    "result_source_for",
]
