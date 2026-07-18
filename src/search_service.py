"""Chinese-friendly local search orchestration and result presentation."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from src.models import (
    SearchFacetCounts,
    SearchField,
    SearchFilters,
    SearchResult,
    SearchSnippet,
    SearchSort,
)
from src.text_utils import (
    build_context_excerpt,
    build_context_excerpts,
    extract_search_terms,
    highlight_html,
    literal_match_spans,
)

LOGGER = logging.getLogger(__name__)


class SearchDatabase(Protocol):
    """The small database surface required by :class:`SearchService`."""

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        terms: tuple[str, ...] | None = None,
        filters: SearchFilters | None = None,
        sort_by: SearchSort | str = SearchSort.RELEVANCE,
    ) -> list[SearchResult]:
        """Return filtered, ranked page matches for a safe FTS5 expression."""

    def search_facet_counts(
        self,
        *,
        terms: tuple[str, ...] = (),
        filters: SearchFilters | None = None,
    ) -> SearchFacetCounts:
        """Return counts for the complete current search and filter state."""

    def search_document_counts(
        self,
        *,
        terms: tuple[str, ...] = (),
        filters: SearchFilters | None = None,
    ) -> dict[int, int]:
        """Return exact per-document counts for the complete filtered result set."""


class SearchService:
    """Normalize free-form queries and return source-aware local matches."""

    def __init__(
        self,
        database: SearchDatabase,
        *,
        max_query_terms: int = 16,
        snippet_length: int = 180,
        max_results: int = 100,
    ) -> None:
        if max_query_terms < 1:
            raise ValueError("max_query_terms 必须大于 0")
        if snippet_length < 20:
            raise ValueError("snippet_length 不能小于 20")
        if max_results < 1:
            raise ValueError("max_results 必须大于 0")
        self._database = database
        self._max_query_terms = max_query_terms
        self._snippet_length = snippet_length
        self._max_results = max_results

    def query_terms(self, query: str) -> tuple[str, ...]:
        """Expose the same literal terms used for search and highlighting."""

        return extract_search_terms(query, max_terms=self._max_query_terms)

    def normalize_query(self, query: str) -> str:
        """Convert free-form input into an operator-safe FTS5 OR expression."""

        return " OR ".join(f'"{term}"' for term in self.query_terms(query))

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        filters: SearchFilters | None = None,
        sort_by: SearchSort | str = SearchSort.RELEVANCE,
    ) -> list[SearchResult]:
        """Search local pages, safely returning an empty list for empty input."""

        terms = self.query_terms(query)
        if not terms or limit <= 0:
            return []
        normalized_query = " OR ".join(f'"{term}"' for term in terms)
        safe_limit = min(limit, self._max_results)
        try:
            results = self._database.search(
                normalized_query,
                limit=safe_limit,
                terms=terms,
                filters=filters or SearchFilters(),
                sort_by=sort_by,
            )
        except Exception:
            LOGGER.exception("本地全文检索失败")
            raise
        return [self._with_natural_snippet(result, terms) for result in results]

    def build_snippet(
        self,
        content: str,
        terms: tuple[str, ...] | list[str],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Build a compact natural-text excerpt centred on the first match."""

        length = max_chars if max_chars is not None else self._snippet_length
        return build_context_excerpt(content, terms, max_chars=length)

    def facet_counts(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
    ) -> SearchFacetCounts:
        """Count filter values consistently, including when the query is empty."""

        return self._database.search_facet_counts(
            terms=self.query_terms(query),
            filters=filters or SearchFilters(),
        )

    def document_counts(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
    ) -> dict[int, int]:
        """Count complete filtered matches by document without rerunning search."""

        terms = self.query_terms(query)
        if not terms:
            return {}
        return self._database.search_document_counts(
            terms=terms,
            filters=filters or SearchFilters(),
        )

    def highlighted_snippet(self, result: SearchResult, query: str) -> str:
        """Return an escaped HTML snippet containing only safe ``mark`` tags."""

        snippet = result.snippet or self.build_snippet(
            result.content, self.query_terms(query)
        )
        return highlight_html(snippet, self.query_terms(query))

    def _with_natural_snippet(
        self, result: SearchResult, terms: tuple[str, ...]
    ) -> SearchResult:
        source_values = {
            SearchField.MARKDOWN: result.markdown_content,
            SearchField.OCR_TEXT: result.ocr_text,
            SearchField.EXTRACTED_TEXT: result.extracted_text,
            SearchField.DOCUMENT_TITLE: result.document_title,
            SearchField.FILENAME: result.filename,
            SearchField.TAG: "、".join(result.tags),
            SearchField.PROJECT: "、".join(result.projects),
        }
        snippets: list[SearchSnippet] = []
        total_matches = 0
        seen_excerpt_text: set[str] = set()
        for field in result.match_fields:
            source = source_values[field]
            field_matches = len(literal_match_spans(source, terms))
            total_matches += field_matches
            remaining = 3 - len(snippets)
            if remaining <= 0:
                continue
            for excerpt in build_context_excerpts(
                source,
                terms,
                max_chars=self._snippet_length,
                max_excerpts=remaining,
            ):
                normalized = " ".join(excerpt.casefold().split())
                if normalized in seen_excerpt_text:
                    continue
                seen_excerpt_text.add(normalized)
                snippets.append(
                    SearchSnippet(
                        field=field,
                        text=excerpt,
                        match_count=field_matches,
                    )
                )
        fallback_source = result.content.strip() or result.snippet
        fallback = self.build_snippet(fallback_source, terms)
        if not snippets and fallback:
            fallback_field = (
                result.match_fields[0]
                if result.match_fields
                else SearchField.EXTRACTED_TEXT
            )
            snippets.append(
                SearchSnippet(
                    field=fallback_field,
                    text=fallback,
                    match_count=len(literal_match_spans(fallback_source, terms)),
                )
            )
        primary = snippets[0].text if snippets else fallback
        return replace(
            result,
            snippet=primary,
            snippets=tuple(snippets),
            match_count=total_matches,
        )


__all__ = ["SearchDatabase", "SearchService"]
