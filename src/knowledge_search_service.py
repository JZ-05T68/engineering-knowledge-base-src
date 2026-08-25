"""Offline knowledge search service (v0.5.2 Phase 3C).

Knowledge-scope retrieval closure: query normalization, FTS5 expression
generation, ``Database.search_knowledge`` invocation, snippet generation and
result assembly. This service is strictly offline — no AI provider, no
network, no API key and no embedding path. Page-scope search semantics
(``SearchService`` / ``Database.search``) are untouched: the knowledge scope
uses FTS5 MATCH as its recall gate while the page scope keeps its LIKE gate.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Final

from src.database import Database
from src.models import KnowledgeSearchResult
from src.text_utils import (
    build_context_excerpt,
    extract_fts_search_terms,
    extract_search_terms,
)

_QUOTED_PHRASE: Final[re.Pattern[str]] = re.compile(r'"([^"]+)"')
DEFAULT_MAX_QUERY_TERMS: Final[int] = 16
DEFAULT_SNIPPET_LENGTH: Final[int] = 180
DEFAULT_MAX_RESULTS: Final[int] = 100


class KnowledgeSearchService:
    """Normalize free-form queries and return grouped knowledge search results."""

    def __init__(
        self,
        database: Database,
        *,
        max_query_terms: int = DEFAULT_MAX_QUERY_TERMS,
        snippet_length: int = DEFAULT_SNIPPET_LENGTH,
        max_results: int = DEFAULT_MAX_RESULTS,
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

    def build_match_expression(self, query: str) -> str:
        """Return an operator-safe FTS5 OR expression for ``query``.

        Double-quoted segments become FTS5 phrase queries (their inner text is
        segmented into indexed tokens and rejoined with spaces, so a phrase
        matches adjacent jieba tokens in the shadow columns). The remaining
        text is segmented into FTS tokens and OR-ed. Prefix ``*`` is never
        generated and never implicitly expanded.
        """

        normalized = unicodedata.normalize("NFKC", query).strip()
        if not normalized:
            return ""
        phrases: list[str] = []
        for phrase in _QUOTED_PHRASE.findall(normalized):
            tokens = extract_fts_search_terms(
                phrase, max_terms=self._max_query_terms
            )
            if tokens:
                phrases.append(" ".join(tokens))
        remainder = _QUOTED_PHRASE.sub(" ", normalized)
        tokens = extract_fts_search_terms(
            remainder, max_terms=self._max_query_terms
        )
        parts: list[str] = [f'"{phrase}"' for phrase in phrases]
        parts.extend(f'"{token}"' for token in tokens)
        return " OR ".join(dict.fromkeys(parts))

    def query_terms(self, query: str) -> tuple[str, ...]:
        """Return the literal terms used for snippet highlighting."""

        return extract_search_terms(query, max_terms=self._max_query_terms)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Search both knowledge FTS tables and return grouped results.

        ``limit`` applies per result type (knowledge objects first, then
        memory entries). An empty or invalid query returns an empty tuple;
        invalid FTS expressions are logged by the database layer and degrade
        to an empty result set, never to a page-scope fallback.
        """

        match_expression = self.build_match_expression(query)
        if not match_expression or limit <= 0:
            return ()
        terms = self.query_terms(query)
        safe_limit = min(limit, self._max_results)
        results = self._database.search_knowledge(
            match_expression,
            limit=safe_limit,
            include_archived=include_archived,
            include_superseded=include_superseded,
        )
        return tuple(self._with_snippet(result, terms) for result in results)

    def _with_snippet(
        self, result: KnowledgeSearchResult, terms: tuple[str, ...]
    ) -> KnowledgeSearchResult:
        """Attach a plain-text excerpt centred on the first literal match."""

        source = f"{result.title}\n{result.content}"
        snippet = build_context_excerpt(
            source, terms, max_chars=self._snippet_length
        )
        if not snippet and result.content:
            snippet = result.content[: self._snippet_length].strip()
        return replace(result, snippet=snippet)


__all__ = [
    "DEFAULT_MAX_QUERY_TERMS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_SNIPPET_LENGTH",
    "KnowledgeSearchService",
]
