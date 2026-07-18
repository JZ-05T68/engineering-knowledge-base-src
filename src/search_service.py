"""Chinese-friendly full-text search orchestration.

The database owns SQLite/FTS5 persistence.  This module keeps query handling and
human-readable snippet generation out of the persistence layer so that both can
be tested independently.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from dataclasses import replace
from typing import Protocol

import jieba

from src.models import SearchResult

LOGGER = logging.getLogger(__name__)

_SEARCHABLE_TOKEN = re.compile(r"[\w\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_IMAGE = re.compile(r"!\[[^]]*]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^]]+)]\([^)]*\)")
_MARKDOWN_MARKER = re.compile(r"(?m)^(?:#{1,6}|[-*+] |>+)\s*")
_FTS_OPERATORS = frozenset({"and", "or", "not", "near"})


class SearchDatabase(Protocol):
    """The small database surface required by :class:`SearchService`."""

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Return ranked page matches for a valid SQLite FTS5 expression."""


class SearchService:
    """Normalize user queries and return citation-ready search results."""

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

    def normalize_query(self, query: str) -> str:
        """Convert free-form input into a literal, injection-safe FTS5 query.

        Every term is quoted and terms are combined with ``OR``.  This produces
        useful recall for Chinese engineering terms while preventing punctuation
        or FTS operators in user input from changing the query grammar.
        """

        return " OR ".join(f'"{term}"' for term in self._query_terms(query))

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Search local pages, safely returning an empty list for empty input."""

        terms = self._query_terms(query)
        if not terms or limit <= 0:
            return []

        normalized_query = " OR ".join(f'"{term}"' for term in terms)
        safe_limit = min(limit, self._max_results)
        try:
            results = self._database.search(normalized_query, limit=safe_limit)
        except Exception:
            LOGGER.exception("本地全文检索失败")
            raise

        return [self._with_natural_snippet(result, terms) for result in results]

    def build_snippet(
        self,
        content: str,
        terms: list[str] | tuple[str, ...],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Build a compact natural-text excerpt centred on the first match."""

        text = _plain_text(content)
        if not text:
            return ""

        length = max_chars if max_chars is not None else self._snippet_length
        if length < 1:
            return ""
        if len(text) <= length:
            return text

        folded_text = text.casefold()
        positions = [folded_text.find(term.casefold()) for term in terms if term]
        positions = [position for position in positions if position >= 0]
        match_position = min(positions, default=0)

        start = max(0, match_position - length // 3)
        end = min(len(text), start + length)
        if end - start < length:
            start = max(0, end - length)

        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = f"…{excerpt}"
        if end < len(text):
            excerpt = f"{excerpt}…"
        return excerpt

    def _query_terms(self, query: str) -> list[str]:
        if not isinstance(query, str):
            return []

        normalized = unicodedata.normalize("NFKC", query).strip()
        if not normalized:
            return []

        terms: list[str] = []
        seen: set[str] = set()
        for jieba_token in jieba.lcut(normalized, cut_all=False):
            for match in _SEARCHABLE_TOKEN.finditer(jieba_token):
                term = match.group(0).casefold().strip("_")
                if not term or term in _FTS_OPERATORS or term in seen:
                    continue
                seen.add(term)
                terms.append(term)
                if len(terms) >= self._max_query_terms:
                    return terms
        return terms

    def _with_natural_snippet(
        self, result: SearchResult, terms: list[str]
    ) -> SearchResult:
        source = result.content.strip() or result.snippet
        snippet = self.build_snippet(source, terms)
        return replace(result, snippet=snippet)


def _plain_text(value: str) -> str:
    """Remove common presentation markup while retaining readable source text."""

    text = html.unescape(value or "")
    text = _HTML_TAG.sub(" ", text)
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_MARKER.sub("", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return _WHITESPACE.sub(" ", text).strip()

