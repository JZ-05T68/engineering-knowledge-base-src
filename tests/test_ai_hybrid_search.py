"""Tests for the hybrid lexical+vector integration. Fully offline, fakes only."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.ai.hybrid_search import (
    HybridSearchService,
    LexicalSearch,
    PageHydrationSource,
    VectorPathStatus,
    lexical_ranked_hits,
)
from src.ai.provider import AIExecutionError, AIUnavailableError
from src.ai.retrieval import RankedHit
from src.models import Document, Page, PageStatus, SearchResult

NOW = datetime(2026, 8, 14, 12, 0, 0)


def _document(document_id: int) -> Document:
    return Document(
        id=document_id,
        title=f"文档{document_id}",
        filename=f"doc{document_id}.pdf",
        source_path=Path(f"data/raw/doc{document_id}.pdf"),
        sha256=f"sha256-of-doc-{document_id}",
        page_count=10,
        created_at=NOW,
        updated_at=NOW,
    )


def _page(page_id: int, document_id: int, page_number: int) -> Page:
    return Page(
        id=page_id,
        document_id=document_id,
        page_number=page_number,
        image_path=Path(f"data/pages/p{page_id}.png"),
        extracted_text=f"页面 {page_id} 的提取文本",
        ocr_text="",
        markdown_content="",
        markdown_path=None,
        status=PageStatus.REVIEWED,
        processing_error="",
        created_at=NOW,
        updated_at=NOW,
    )


def _search_result(page_id: int, document_id: int, page_number: int) -> SearchResult:
    document = _document(document_id)
    page = _page(page_id, document_id, page_number)
    return SearchResult(
        page_id=page_id,
        document_id=document_id,
        document_title=document.title,
        filename=document.filename,
        page_number=page_number,
        image_path=page.image_path,
        content=page.extracted_text,
        snippet=f"片段{page_id}",
        rank=-float(page_id),
        status=page.status,
        document_source_path=document.source_path,
        document_sha256=document.sha256,
        extracted_text=page.extracted_text,
        updated_at=NOW,
    )


class FakeLexical:
    """Lexical fake returning a fixed ordered result list."""

    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        self.calls.append((query, limit))
        return list(self._results[:limit])


class FakeVector:
    """Vector recall fake; may be armed to raise."""

    def __init__(
        self, hits: tuple[RankedHit, ...] = (), error: Exception | None = None
    ) -> None:
        self._hits = hits
        self._error = error
        self.calls = 0

    def recall(self, query: str, *, limit: int) -> tuple[RankedHit, ...]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._hits[:limit]


class FakeHydration:
    def __init__(self, page_ids: range | tuple[int, ...]) -> None:
        self._pages = {
            page_id: _page(page_id, document_id=100 + page_id, page_number=page_id)
            for page_id in page_ids
        }
        self._documents = {
            page.document_id: _document(page.document_id)
            for page in self._pages.values()
        }

    def get_page(self, page_id: int) -> Page | None:
        return self._pages.get(page_id)

    def get_document(self, document_id: int) -> Document | None:
        return self._documents.get(document_id)


def _service(
    lexical_results: list[SearchResult],
    vector: FakeVector | None,
    hydration: FakeHydration,
) -> HybridSearchService:
    return HybridSearchService(
        lexical=FakeLexical(lexical_results),
        hydration=hydration,
        vector=vector,
    )


# Lexical fixtures: A=page 1, B=page 2, C=page 3.
LEXICAL = [_search_result(1, 101, 1), _search_result(2, 102, 2), _search_result(3, 103, 3)]


def test_lexical_adapter_ranks_from_one_and_preserves_order() -> None:
    hits = lexical_ranked_hits(LEXICAL)

    assert [(hit.page_id, hit.rank) for hit in hits] == [(1, 1), (2, 2), (3, 3)]


def test_vector_only_candidate_enters_union_and_hydrates_with_citation() -> None:
    """X (page 99) never matched LIKE recall but must become a full result."""

    vector = FakeVector(
        (RankedHit(99, 1), RankedHit(1, 2), RankedHit(4, 3))
    )
    hydration = FakeHydration(range(1, 200))
    outcome = _service(LEXICAL, vector, hydration).search("恢复文档")

    page_ids = [item.result.page_id for item in outcome.results]
    assert 99 in page_ids
    assert outcome.vector_status is VectorPathStatus.OK

    x = next(item for item in outcome.results if item.result.page_id == 99)
    assert x.lexical_rank is None
    assert x.vector_rank == 1
    assert x.fused_score > 0
    result = x.result
    assert result.document_title == "文档199"
    assert result.page_number == 99
    assert result.document_sha256 == "sha256-of-doc-199"
    assert result.image_path == Path("data/pages/p99.png")
    assert result.content == "页面 99 的提取文本"
    assert result.document_source_path == Path("data/raw/doc199.pdf")
    assert result.status is PageStatus.REVIEWED


def test_dual_source_candidate_merges_once_with_both_ranks() -> None:
    vector = FakeVector((RankedHit(1, 1),))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    merged = [item for item in outcome.results if item.result.page_id == 1]
    assert len(merged) == 1
    assert merged[0].lexical_rank == 1
    assert merged[0].vector_rank == 1
    # Lexical-origin results reuse the original SearchResult object.
    assert merged[0].result is LEXICAL[0]


def test_lexical_only_when_vector_source_not_configured() -> None:
    outcome = _service(LEXICAL, None, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.DISABLED
    assert [item.result for item in outcome.results] == LEXICAL


def test_lexical_only_when_vector_returns_empty() -> None:
    vector = FakeVector(())
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.EMPTY
    assert vector.calls == 1
    assert [item.result for item in outcome.results] == LEXICAL


def test_lexical_only_when_vector_unavailable() -> None:
    vector = FakeVector(error=AIUnavailableError("未配置 API Key"))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.UNAVAILABLE
    assert [item.result for item in outcome.results] == LEXICAL


def test_lexical_only_when_vector_execution_fails() -> None:
    vector = FakeVector(error=AIExecutionError("HTTP 500"))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result for item in outcome.results] == LEXICAL


def test_lexical_only_when_vector_output_invalid() -> None:
    vector = FakeVector((RankedHit(1, 1), RankedHit(1, 2)))  # duplicate page
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result for item in outcome.results] == LEXICAL


def test_lexical_only_when_vector_raises_unexpected_error() -> None:
    vector = FakeVector(error=RuntimeError("boom"))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result for item in outcome.results] == LEXICAL


def test_missing_page_candidate_skipped_without_breaking_search() -> None:
    vector = FakeVector((RankedHit(9999, 1), RankedHit(2, 2)))
    hydration = FakeHydration(range(1, 10))  # page 9999 does not exist
    outcome = _service(LEXICAL, vector, hydration).search("q")

    assert outcome.invalid_vector_candidates == 1
    page_ids = [item.result.page_id for item in outcome.results]
    assert 9999 not in page_ids
    assert {1, 2, 3} <= set(page_ids)


def test_degraded_lexical_results_match_plain_lexical_exactly() -> None:
    """Strong acceptance: identity, count and order equal SearchService output."""

    for vector in (
        None,
        FakeVector(()),
        FakeVector(error=AIUnavailableError("off")),
        FakeVector(error=AIExecutionError("down")),
    ):
        outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")
        assert [item.result for item in outcome.results] == LEXICAL
        assert outcome.invalid_vector_candidates == 0


def test_provenance_is_carried_through() -> None:
    vector = FakeVector((RankedHit(3, 1), RankedHit(1, 2)))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    by_id = {item.result.page_id: item for item in outcome.results}
    assert by_id[3].lexical_rank == 3
    assert by_id[3].vector_rank == 1
    assert by_id[3].fused_score == pytest.approx(1.0 / 63 + 1.0 / 61)
    assert by_id[2].vector_rank is None
    assert by_id[2].fused_score == pytest.approx(1.0 / 62)


def test_determinism_same_input_same_output() -> None:
    vector = FakeVector((RankedHit(50, 1), RankedHit(1, 2)))
    hydration = FakeHydration(range(1, 100))
    service = _service(LEXICAL, vector, hydration)

    first = service.search("q")
    second = service.search("q")

    assert [item.result.page_id for item in first.results] == [
        item.result.page_id for item in second.results
    ]
    assert [item.fused_score for item in first.results] == [
        item.fused_score for item in second.results
    ]


def test_protocol_boundaries_accept_fakes() -> None:
    assert isinstance(FakeLexical([]), LexicalSearch)
    assert isinstance(FakeHydration(range(1, 5)), PageHydrationSource)
    assert not isinstance(object(), LexicalSearch)


# --- exception boundary: integration errors must never degrade silently ---


def test_lexical_dependency_error_propagates() -> None:
    class _BrokenLexical:
        def search(self, query: str, limit: int = 20) -> list[SearchResult]:
            raise RuntimeError("lexical implementation bug")

    service = HybridSearchService(
        lexical=_BrokenLexical(), hydration=FakeHydration(range(1, 10))
    )
    with pytest.raises(RuntimeError, match="lexical implementation bug"):
        service.search("q")


def test_hydration_error_propagates_not_degraded() -> None:
    class _BrokenHydration:
        def get_page(self, page_id: int) -> Page | None:
            raise RuntimeError("hydration bug")

        def get_document(self, document_id: int) -> Document | None:
            raise RuntimeError("hydration bug")

    vector = FakeVector((RankedHit(99, 1),))
    service = HybridSearchService(
        lexical=FakeLexical(LEXICAL),
        hydration=_BrokenHydration(),
        vector=vector,
    )
    with pytest.raises(RuntimeError, match="hydration bug"):
        service.search("q")


def test_invalid_rrf_k_fails_at_construction_not_search() -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        HybridSearchService(
            lexical=FakeLexical(LEXICAL),
            hydration=FakeHydration(range(1, 10)),
            rrf_k=0,
        )


def test_vector_contract_violation_still_degrades_but_kernel_errors_would_not() -> None:
    """Duplicate vector page ids degrade; the final fusion call stays unwrapped."""

    vector = FakeVector((RankedHit(1, 1), RankedHit(1, 2)))
    outcome = _service(LEXICAL, vector, FakeHydration(range(1, 10))).search("q")

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result for item in outcome.results] == LEXICAL
