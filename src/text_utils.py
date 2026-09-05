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


def build_agent_page_text(
    *, extracted_text: str, ocr_text: str, manual_text: str = ""
) -> tuple[str, str]:
    """Combine immutable source text with an explicitly labelled correction."""

    if ocr_text.strip():
        original = ocr_text.strip()
        source_kind = "ocr_text"
    elif extracted_text.strip():
        original = extracted_text.strip()
        source_kind = "pdf_text"
    else:
        original = ""
        source_kind = "none"
    correction = manual_text.strip()
    if original and correction:
        return (
            f"【原始页面文字】\n{original}\n\n"
            f"【用户人工校对或补充】\n{correction}",
            f"{source_kind}+manual",
        )
    if correction:
        return f"【用户人工校对或补充】\n{correction}", "manual"
    return original, source_kind


def literal_match_spans(text: str, terms: Sequence[str]) -> tuple[tuple[int, int], ...]:
    """Return non-overlapping literal matches, preferring longer overlapping terms."""

    unique_terms = sorted(
        {term for term in terms if term},
        key=lambda value: (-len(value), value.casefold()),
    )
    if not text or not unique_terms:
        return ()
    pattern = re.compile(
        "|".join(re.escape(term) for term in unique_terms),
        flags=re.IGNORECASE,
    )
    return tuple((match.start(), match.end()) for match in pattern.finditer(text))


def build_context_excerpts(
    content: str,
    terms: Sequence[str],
    *,
    max_chars: int = 180,
    max_excerpts: int = 3,
) -> tuple[str, ...]:
    """Build distinct excerpts around literal matches without interpreting markup."""

    text = to_plain_text(content)
    if not text or max_chars < 1 or max_excerpts < 1:
        return ()
    spans = literal_match_spans(text, terms)
    if not spans:
        fallback = build_context_excerpt(text, terms, max_chars=max_chars)
        return (fallback,) if fallback else ()

    excerpts: list[str] = []
    normalized_excerpts: list[str] = []
    for match_start, _ in spans:
        start = max(0, match_start - max_chars // 3)
        end = min(len(text), start + max_chars)
        if end - start < max_chars:
            start = max(0, end - max_chars)
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = f"…{excerpt}"
        if end < len(text):
            excerpt = f"{excerpt}…"
        normalized = _WHITESPACE.sub(" ", excerpt).casefold()
        if any(
            normalized in existing or existing in normalized
            for existing in normalized_excerpts
        ):
            continue
        excerpts.append(excerpt)
        normalized_excerpts.append(normalized)
        if len(excerpts) >= max_excerpts:
            break
    return tuple(excerpts)


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
    if any(_ONLY_CJK.fullmatch(term) and len(term) > 1 for term in terms):
        # Multi-term OR queries should not be dominated by low-information
        # segmentation fragments such as “的” or “不”. A deliberate one-character
        # Chinese query remains supported because it has no longer CJK companion.
        terms = [
            term
            for term in terms
            if not (_ONLY_CJK.fullmatch(term) and len(term) == 1)
        ]
    return tuple(terms)


def extract_fts_search_terms(
    query: str,
    *,
    max_terms: int = 16,
    max_term_chars: int = 128,
) -> tuple[str, ...]:
    """Return deduplicated FTS5 tokens for knowledge-scope FTS recall.

    Unlike :func:`extract_search_terms`, a continuous CJK group is **never**
    kept whole: the knowledge shadow columns are jieba token sequences, so a
    whole-group literal would not exist in the FTS index. Every searchable
    group is therefore segmented with ``jieba.cut_for_search`` and each token
    is cleaned (lowercase, ``_FTS_OPERATORS`` removed, deduplicated, bounded)
    exactly like the page-search term path. Page-search semantics are not
    changed: ``extract_search_terms`` is untouched.
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
    "build_agent_page_text",
    "build_context_excerpt",
    "build_context_excerpts",
    "extract_fts_search_terms",
    "extract_search_terms",
    "highlight_html",
    "literal_match_spans",
    "to_plain_text",
]
