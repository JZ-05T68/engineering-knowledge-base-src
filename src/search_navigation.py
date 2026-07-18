"""Validation and query-parameter helpers for search-to-reader navigation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.models import Document, Page, SearchResult


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


def reader_query_params(result: SearchResult, query: str) -> dict[str, str]:
    """Return explicit reader coordinates and a bounded query hint."""

    return {
        "document": str(result.document_id),
        "page": str(result.page_number),
        "from_search": "1",
        "search_query": query[:500],
    }


__all__ = [
    "SearchNavigationError",
    "SearchNavigationTarget",
    "reader_query_params",
    "validate_search_target",
]
