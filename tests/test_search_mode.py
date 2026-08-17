"""Phase 11B tests: hybrid search mode gating, provenance labelling, state, retry.

Fully offline: fake providers and pure functions only. No Qwen, no LLM, no
rerank, no database writes.
"""

from __future__ import annotations

import pytest

from src.ai.hybrid_search import HybridSearchOutcome, HybridSearchResult, VectorPathStatus
from src.models import (
    PageStatus,
    SearchField,
    SearchFilters,
    SearchMode,
    SearchResult,
    SearchSort,
)
from src.search_mode import (
    auto_execute_allowed,
    hybrid_gate_fallback_note,
    hybrid_is_allowed,
    hybrid_status_note,
    hybrid_vector_active,
    provenance_from_outcome,
    result_source,
    result_source_for,
)
from src.search_state import SearchPageState, parse_search_state, search_state_query_params


def _filters(**kwargs: object) -> SearchFilters:
    defaults: dict[str, object] = {}
    defaults.update(kwargs)
    return SearchFilters(**defaults)  # type: ignore[arg-type]


class TestHybridIsAllowed:
    def test_hybrid_requires_hybrid_mode(self) -> None:
        assert hybrid_is_allowed(
            SearchMode.KEYWORD, _filters(), SearchSort.RELEVANCE
        ) is False

    def test_hybrid_allowed_with_empty_filters_relevance(self) -> None:
        assert hybrid_is_allowed(
            SearchMode.HYBRID, _filters(), SearchSort.RELEVANCE
        ) is True

    @pytest.mark.parametrize(
        "filters",
        [
            _filters(document_ids=(1,)),
            _filters(project_ids=(2,)),
            _filters(tag_ids=(3,)),
            _filters(statuses=(PageStatus.REVIEWED,)),
            _filters(match_fields=(SearchField.OCR_TEXT,)),
            _filters(has_note=True),
            _filters(evidence_basket_id=7),
        ],
    )
    def test_any_filter_disallows_hybrid(self, filters: SearchFilters) -> None:
        assert hybrid_is_allowed(SearchMode.HYBRID, filters, SearchSort.RELEVANCE) is False

    @pytest.mark.parametrize(
        "sort",
        [SearchSort.DOCUMENT_PAGE, SearchSort.VIEWED_DESC, SearchSort.UPDATED_DESC],
    )
    def test_non_relevance_sort_disallows_hybrid(self, sort: SearchSort) -> None:
        assert hybrid_is_allowed(SearchMode.HYBRID, _filters(), sort) is False


class TestResultSource:
    def test_both(self) -> None:
        assert result_source(lexical_rank=1, vector_rank=2) == "混合匹配"

    def test_vector_only(self) -> None:
        assert result_source(lexical_rank=None, vector_rank=1) == "语义召回"

    def test_lexical_only(self) -> None:
        assert result_source(lexical_rank=1, vector_rank=None) == "关键词匹配"

    def test_unknown_page_falls_back_to_keyword(self) -> None:
        assert result_source_for(999, {}) == "关键词匹配"


class TestHybridStatusNote:
    def test_disabled(self) -> None:
        assert hybrid_status_note(VectorPathStatus.DISABLED) == (
            "AI 混合检索未启用，当前使用关键词检索"
        )

    def test_unavailable(self) -> None:
        assert "已保留关键词结果" in hybrid_status_note(VectorPathStatus.UNAVAILABLE)

    def test_failed(self) -> None:
        assert "已保留关键词结果" in hybrid_status_note(VectorPathStatus.FAILED)

    def test_ok_is_silent(self) -> None:
        assert hybrid_status_note(VectorPathStatus.OK) == ""


class TestHybridGateFallbackNote:
    def test_keyword_mode_is_silent(self) -> None:
        assert hybrid_gate_fallback_note(
            SearchMode.KEYWORD, _filters(has_note=True), SearchSort.RELEVANCE
        ) == ""

    def test_gate_clean_hybrid_is_silent(self) -> None:
        assert hybrid_gate_fallback_note(
            SearchMode.HYBRID, _filters(), SearchSort.RELEVANCE
        ) == ""

    def test_hybrid_with_filter_shows_note(self) -> None:
        assert hybrid_gate_fallback_note(
            SearchMode.HYBRID, _filters(has_note=True), SearchSort.RELEVANCE
        ) == "当前筛选或排序条件不支持 AI 混合检索，本次搜索已使用关键词检索。"

    def test_hybrid_with_non_relevance_sort_shows_note(self) -> None:
        assert hybrid_gate_fallback_note(
            SearchMode.HYBRID, _filters(), SearchSort.DOCUMENT_PAGE
        ) == "当前筛选或排序条件不支持 AI 混合检索，本次搜索已使用关键词检索。"


class TestHybridVectorActive:
    @pytest.mark.parametrize(
        "status", [VectorPathStatus.OK, VectorPathStatus.EMPTY]
    )
    def test_ran_vector_path_is_active(self, status: VectorPathStatus) -> None:
        assert hybrid_vector_active(status) is True

    @pytest.mark.parametrize(
        "status",
        [
            VectorPathStatus.DISABLED,
            VectorPathStatus.UNAVAILABLE,
            VectorPathStatus.FAILED,
        ],
    )
    def test_degraded_status_is_purely_lexical(self, status: VectorPathStatus) -> None:
        assert hybrid_vector_active(status) is False


class TestAutoExecuteAllowed:
    def test_keyword_not_executed_auto_runs(self) -> None:
        assert auto_execute_allowed(
            SearchMode.KEYWORD, _filters(), SearchSort.RELEVANCE, already_executed=False
        ) is True

    def test_hybrid_never_auto_runs(self) -> None:
        assert auto_execute_allowed(
            SearchMode.HYBRID, _filters(), SearchSort.RELEVANCE, already_executed=False
        ) is False

    def test_keyword_with_filter_auto_runs(self) -> None:
        # A narrowing filter means keyword fallback is active; still free to run.
        assert auto_execute_allowed(
            SearchMode.HYBRID, _filters(has_note=True), SearchSort.RELEVANCE,
            already_executed=False,
        ) is True

    def test_already_executed_never_reruns(self) -> None:
        assert auto_execute_allowed(
            SearchMode.KEYWORD, _filters(), SearchSort.RELEVANCE, already_executed=True
        ) is False
        assert auto_execute_allowed(
            SearchMode.HYBRID, _filters(), SearchSort.RELEVANCE, already_executed=True
        ) is False


def _result(page_id: int) -> SearchResult:
    return SearchResult(
        page_id=page_id,
        document_id=1,
        document_title="t",
        filename="f",
        page_number=page_id,
        image_path=__import__("pathlib").Path(f"p{page_id}.png"),
        content="",
        snippet="",
        rank=0.0,
        status=PageStatus.PENDING,
    )


class TestProvenanceFromOutcome:
    def test_maps_lexical_and_vector_ranks(self) -> None:
        outcome = HybridSearchOutcome(
            results=(
                HybridSearchResult(_result(18), 0.5, 1, 2),
                HybridSearchResult(_result(5), 0.25, None, 4),
                HybridSearchResult(_result(1), 0.1, 5, None),
            ),
            vector_status=VectorPathStatus.OK,
        )
        provenance = provenance_from_outcome(outcome)
        assert provenance == {18: (1, 2), 5: (None, 4), 1: (5, None)}


class TestSearchModeState:
    def test_default_mode_is_keyword(self) -> None:
        state = SearchPageState()
        assert state.mode is SearchMode.KEYWORD

    def test_parse_old_url_without_mode_defaults_keyword(self) -> None:
        state = parse_search_state({"q": "定时器"})
        assert state.mode is SearchMode.KEYWORD

    def test_parse_hybrid_mode_roundtrip(self) -> None:
        state = SearchPageState(
            query="定时器",
            mode=SearchMode.HYBRID,
            filters=SearchFilters(),
            sort=SearchSort.RELEVANCE,
        )
        params = search_state_query_params(state)
        assert params["mode"] == "hybrid"
        assert parse_search_state(params).mode is SearchMode.HYBRID

    def test_keyword_mode_not_serialized(self) -> None:
        state = SearchPageState(query="定时器", mode=SearchMode.KEYWORD)
        assert "mode" not in search_state_query_params(state)
