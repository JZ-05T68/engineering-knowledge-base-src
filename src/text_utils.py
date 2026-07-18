"""Reusable, safe text normalization, excerpt, and highlighting helpers."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Sequence
from typing import Final

import jieba

_SEARCHABLE_GROUP: Final[re.Pattern[str]] = re.compile(
    r"[\w\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE
)
_ONLY_CJK: Final[re.Pattern[str]] = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_MARKDOWN_IMAGE: Final[re.Pattern[str]] = re.compile(r"!\[[^]]*]\([^)]*\)")
_MARKDOWN_LINK: Final[re.Pattern[str]] = re.compile(r"\[([^]]+)]\([^)]*\)")
_MARKDOWN_MARKER: Final[re.Pattern[str]] = re.compile(
    r"(?m)^(?:#{1,6}|[-*+] |>+)\s*"
)
_FTS_OPERATORS: Final[frozenset[str]] = frozenset({"and", "or", "not", "near"})


def extract_search_terms(
    query: str,
    *,
    max_terms: int = 16,
    max_term_chars: int = 128,
) -> tuple[str, ...]:
    """Return deduplicated literal terms from free-form Chinese or English input.

    Punctuation and FTS5 grammar characters never survive into the returned
    terms. A continuous Chinese group is retained before jieba-derived tokens so
    exact phrases remain discoverable without weakening existing token search.
    """

    if not isinstance(query, str) or max_terms < 1 or max_term_chars < 1:
        return ()
    normalized = unicodedata.normalize("NFKC", query).strip()
    if not normalized:
        return ()

    terms: list[str] = []
    seen: set[str] = set()

    def append(value: str) -> bool:
        term = value.casefold().strip("_")[:max_term_chars]
        if not term or term in _FTS_OPERATORS or term in seen:
            return False
        seen.add(term)
        terms.append(term)
        return len(terms) >= max_terms

    for group_match in _SEARCHABLE_GROUP.finditer(normalized):
        group = group_match.group(0)
        if _ONLY_CJK.fullmatch(group) and len(group) > 1 and append(group):
            break
        for jieba_token in jieba.cut_for_search(group):
            for token_match in _SEARCHABLE_GROUP.finditer(jieba_token):
                if append(token_match.group(0)):
                    break
            if len(terms) >= max_terms:
                break
        if len(terms) >= max_terms:
            break
    return tuple(terms)


def to_plain_text(value: str) -> str:
    """Remove common Markdown presentation markers without interpreting HTML."""

    text = value or ""
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_MARKER.sub("", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return _WHITESPACE.sub(" ", text).strip()


def build_context_excerpt(
    content: str,
    terms: Sequence[str],
    *,
    max_chars: int = 180,
) -> str:
    """Build a stable excerpt centred on the earliest literal term match."""

    text = to_plain_text(content)
    if not text or max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text

    positions: list[int] = []
    folded_text = text.casefold()
    for term in terms:
        if term:
            position = folded_text.find(term.casefold())
            if position >= 0:
                positions.append(position)
    match_position = min(positions, default=0)

    start = max(0, match_position - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = f"…{excerpt}"
    if end < len(text):
        excerpt = f"{excerpt}…"
    return excerpt


def highlight_html(text: str, terms: Sequence[str]) -> str:
    """Escape all source text and wrap literal matches in safe ``mark`` tags.

    Removing the generated ``mark`` tags and HTML-unescaping the result always
    reconstructs ``text`` exactly. No page content is inserted as executable
    HTML.
    """

    unique_terms = sorted(
        {term for term in terms if term},
        key=lambda value: (-len(value), value.casefold()),
    )
    if not unique_terms:
        return html.escape(text, quote=True)
    pattern = re.compile(
        "|".join(re.escape(term) for term in unique_terms),
        flags=re.IGNORECASE,
    )
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[cursor : match.start()], quote=True))
        parts.append("<mark>")
        parts.append(html.escape(match.group(0), quote=True))
        parts.append("</mark>")
        cursor = match.end()
    parts.append(html.escape(text[cursor:], quote=True))
    return "".join(parts)


__all__ = [
    "build_context_excerpt",
    "extract_search_terms",
    "highlight_html",
    "to_plain_text",
]
