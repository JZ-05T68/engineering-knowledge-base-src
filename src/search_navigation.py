"""Validation and query-parameter helpers for search-to-reader navigation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from src.models import Document, Page, SearchResult, SearchViewMode
from src.search_state import SearchPageState, encode_return_state


class NavigationDatabase(Protocol):
    """Database lookups needed to validate a stale search result."""

    def get_document(self, document_id: int) -> Document | None: ...

    def get_page(self, page_id: int) -> Page | None: ...


class SearchNavigationError(RuntimeError):
    """Raised when a result no longer points to the recorded local page."""


@dataclass(frozen=True, slots=True)
class SearchNavigationTarget:
    """Validated local search target and any recoverable file warnings."""

    document: Document
    page: Page
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchDocumentGroup:
    """A stable document group derived from one ordered page-result sequence."""

    document_id: int
    document_title: str
    filename: str
    results: tuple[SearchResult, ...]
    best_result: SearchResult
    total_count: int


@dataclass(frozen=True, slots=True)
class SearchResultPosition:
    """Current result position and safe neighbours inside one loaded scope."""

    index: int
    total: int
    previous: SearchResult | None
    current: SearchResult
    next: SearchResult | None


def validate_search_target(
    database: NavigationDatabase, result: SearchResult
) -> SearchNavigationTarget:
    """Reject missing or mismatched records instead of opening a nearby page."""

    document = database.get_document(result.document_id)
    if document is None:
        raise SearchNavigationError(
            f"搜索结果对应的文档已不存在（文档编号 {result.document_id}）。"
        )
    page = database.get_page(result.page_id)
    if page is None:
        raise SearchNavigationError(
            f"搜索结果对应的页面已不存在（页面编号 {result.page_id}）。"
        )
    if page.document_id != document.id or page.page_number != result.page_number:
        raise SearchNavigationError(
            "搜索结果与当前数据库记录不一致，已停止跳转以避免打开错误页面。"
        )
    warnings: list[str] = []
    if not document.source_path.is_file():
        warnings.append(f"原始 PDF 文件缺失：{document.source_path}")
    if not page.image_path.is_file():
        warnings.append(f"页面图像缺失：{page.image_path}")
    return SearchNavigationTarget(document=document, page=page, warnings=tuple(warnings))


def reader_query_params(
    result: SearchResult,
    query: str,
    *,
    return_state: SearchPageState | None = None,
) -> dict[str, str]:
    """Return exact reader coordinates plus an optional complete return state."""

    params = {
        "document": str(result.document_id),
        "page": str(result.page_number),
        "from_search": "1",
        "search_query": query[:500],
    }
    if return_state is not None:
        params["search_return"] = encode_return_state(return_state)
    return params


def unique_ordered_results(results: Sequence[SearchResult]) -> tuple[SearchResult, ...]:
    """Remove duplicate page IDs while preserving the database result order."""

    unique: list[SearchResult] = []
    seen_page_ids: set[int] = set()
    for result in results:
        if result.page_id <= 0 or result.page_id in seen_page_ids:
            continue
        seen_page_ids.add(result.page_id)
        unique.append(result)
    return tuple(unique)


def locate_result(
    results: Sequence[SearchResult], page_id: int
) -> SearchResultPosition | None:
    """Locate one page in the loaded global result scope without raising."""

    ordered = unique_ordered_results(results)
    for offset, result in enumerate(ordered):
        if result.page_id == page_id:
            return SearchResultPosition(
                index=offset + 1,
                total=len(ordered),
                previous=ordered[offset - 1] if offset > 0 else None,
                current=result,
                next=ordered[offset + 1] if offset + 1 < len(ordered) else None,
            )
    return None


def document_hit_results(
    results: Sequence[SearchResult], document_id: int
) -> tuple[SearchResult, ...]:
    """Return this document's unique hits in ascending page-number order."""

    hits = (
        result
        for result in unique_ordered_results(results)
        if result.document_id == document_id
    )
    return tuple(sorted(hits, key=lambda result: (result.page_number, result.page_id)))


def group_search_results(
    results: Sequence[SearchResult],
    *,
    document_counts: dict[int, int] | None = None,
) -> tuple[SearchDocumentGroup, ...]:
    """Group unique results without changing group or in-group search order."""

    grouped: dict[int, list[SearchResult]] = {}
    for result in unique_ordered_results(results):
        grouped.setdefault(result.document_id, []).append(result)
    counts = document_counts or {}
    groups: list[SearchDocumentGroup] = []
    for document_id, document_results in grouped.items():
        first = document_results[0]
        best = min(
            document_results,
            key=lambda result: (result.rank, result.page_number, result.page_id),
        )
        groups.append(
            SearchDocumentGroup(
                document_id=document_id,
                document_title=first.document_title,
                filename=first.filename,
                results=tuple(document_results),
                best_result=best,
                total_count=max(counts.get(document_id, 0), len(document_results)),
            )
        )
    return tuple(groups)


def state_for_result(
    state: SearchPageState,
    *,
    result_index: int,
    document_id: int,
    results_per_page: int = 10,
) -> SearchPageState:
    """Record a compact return position without serializing result IDs."""

    page = max(1, (max(result_index, 1) - 1) // max(results_per_page, 1) + 1)
    return replace(
        state,
        result_page=page if state.view_mode is SearchViewMode.PAGE else state.result_page,
        expanded_document_id=(
            document_id
            if state.view_mode is SearchViewMode.DOCUMENT
            else state.expanded_document_id
        ),
        focus_result=max(result_index, 1),
    )


__all__ = [
    "SearchNavigationError",
    "SearchNavigationTarget",
    "SearchDocumentGroup",
    "SearchResultPosition",
    "document_hit_results",
    "group_search_results",
    "locate_result",
    "reader_query_params",
    "state_for_result",
    "unique_ordered_results",
    "validate_search_target",
]
