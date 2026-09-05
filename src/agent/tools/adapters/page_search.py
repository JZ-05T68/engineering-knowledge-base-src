"""``page_search`` read-only Tool Adapter (v0.6.0 Phase 1B).

Thin adapter over the existing :class:`SearchService`: it validates the Tool
boundary arguments, calls the lexical local page search, and projects the
existing :class:`SearchResult` rows into a structured ToolResult with page
stable-id references. No ranking, FTS expression generation, fallback, or
hybrid retrieval logic lives here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.agent.tools.adapters._common import (
    AdapterInputError,
    empty_result,
    failed_result,
    internal_failure_result,
    optional_int,
    reject_unknown_arguments,
    require_text,
    success_result,
)
from src.agent.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolInput,
    ToolReference,
    ToolResult,
    ToolSideEffect,
)
from src.models import PAGE_STABLE_TYPE, SearchResult, build_stable_id
from src.search_service import SearchService
from src.text_utils import build_agent_page_text

ALLOWED_ARGUMENTS = frozenset({"query", "limit"})
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 500


class PageReadingView(Protocol):
    """Read-only page-understanding metadata exposed to the search adapter."""

    summary: str
    keywords: tuple[str, ...]
    key_facts: tuple[str, ...]


class PageReadingLookup(Protocol):
    """Minimal freshness surface; deliberately independent of AI providers."""

    def is_page_ready(self, page_id: int, source_text_sha256: str) -> bool: ...

    def page_reading(self, page_id: int) -> PageReadingView | None: ...

PAGE_SEARCH_DEFINITION = ToolDefinition(
    name="page_search",
    description="在用户已导入的页面资料中检索相关内容，返回结构化页面命中。",
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "query": {
            "type": "string",
            "required": True,
            "description": "页面资料检索关键词",
        },
        "limit": {
            "type": "integer",
            "default": DEFAULT_LIMIT,
            "min": 1,
            "max": MAX_LIMIT,
            "description": "最多返回的结果数",
        },
    },
    timeout_seconds=30.0,
)


class PageSearchAdapter:
    """Execute ``page_search`` through the existing lexical SearchService."""

    tool_name = "page_search"

    def __init__(
        self,
        search_service: SearchService,
        *,
        kb_uuid: str,
        page_readings: PageReadingLookup | None = None,
        require_agent_read: bool = False,
    ) -> None:
        self._service = search_service
        self._kb_uuid = kb_uuid
        self._page_readings = page_readings
        self._require_agent_read = require_agent_read

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, ALLOWED_ARGUMENTS)
            query = require_text(
                tool_input.arguments, "query", max_length=MAX_QUERY_LENGTH
            )
            limit = optional_int(
                tool_input.arguments,
                "limit",
                default=DEFAULT_LIMIT,
                min_value=1,
                max_value=MAX_LIMIT,
            )
            results = self._service.search(query, limit=limit)
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="页面检索执行失败"
            )
        return self._to_result(query, limit, results)

    def _to_result(
        self, query: str, limit: int, results: list[SearchResult]
    ) -> ToolResult:
        rows: list[tuple[SearchResult, str, PageReadingView | None]] = []
        unread_hits = 0
        for result in results:
            source_text = _agent_page_text(result) if self._require_agent_read else result.content
            reading = (
                self._page_readings.page_reading(result.page_id)
                if self._page_readings is not None
                else None
            )
            ready = bool(
                source_text
                and self._page_readings is not None
                and self._page_readings.is_page_ready(
                    result.page_id, _source_text_sha256(source_text)
                )
            )
            if self._require_agent_read and not ready:
                unread_hits += 1
                continue
            rows.append((result, source_text or result.content, reading if ready else None))
        if not rows:
            warnings = (
                ("相关页面尚未让 Agent 读完。",)
                if unread_hits
                else ()
            )
            return empty_result(
                self.tool_name,
                data={"query": query, "limit": limit, "total": 0, "results": []},
                warnings=warnings,
            )
        references = tuple(
            ToolReference(
                stable_id=build_stable_id(
                    self._kb_uuid, PAGE_STABLE_TYPE, result.page_id
                ),
                anchor_label=f"{result.document_title} · 第 {result.page_number} 页",
            )
            for result, _, _ in rows
        )
        data = {
            "query": query,
            "limit": limit,
            "total": len(rows),
            "results": [
                _page_result_to_dict(result, source_text=source_text, reading=reading)
                for result, source_text, reading in rows
            ],
        }
        return success_result(self.tool_name, data, references=references)


def _page_result_to_dict(
    result: SearchResult,
    *,
    source_text: str,
    reading: PageReadingView | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "page_id": result.page_id,
        "document_id": result.document_id,
        "document_title": result.document_title,
        "filename": result.filename,
        "page_number": result.page_number,
        # Final Answer consumes this complete original page text.  The snippet
        # remains only a retrieval/display aid and AI-generated summaries stay
        # in separate fields below.
        "content": source_text,
        "snippet": result.snippet,
        "match_type": result.match_type,
        "match_fields": [field.value for field in result.match_fields],
        "rank": result.rank,
        "status": result.status.value,
        "tags": list(result.tags),
        "projects": list(result.projects),
        "updated_at": _iso_or_none(result.updated_at),
    }
    if reading is not None:
        payload["reading_summary"] = reading.summary
        payload["reading_keywords"] = list(reading.keywords)
        payload["reading_key_facts"] = list(reading.key_facts)
    return payload


def _agent_page_text(result: SearchResult) -> str:
    text, _ = build_agent_page_text(
        extracted_text=result.extracted_text,
        ocr_text=result.ocr_text,
        manual_text=result.markdown_content,
    )
    return text


def _source_text_sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["PAGE_SEARCH_DEFINITION", "PageSearchAdapter"]
