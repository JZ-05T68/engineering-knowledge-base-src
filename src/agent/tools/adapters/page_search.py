"""``page_search`` read-only Tool Adapter (v0.6.0 Phase 1B).

Thin adapter over the existing :class:`SearchService`: it validates the Tool
boundary arguments, calls the lexical local page search, and projects the
existing :class:`SearchResult` rows into a structured ToolResult with page
stable-id references. No ranking, FTS expression generation, fallback, or
hybrid retrieval logic lives here.
"""

from __future__ import annotations

from datetime import datetime

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

ALLOWED_ARGUMENTS = frozenset({"query", "limit"})
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 500

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

    def __init__(self, search_service: SearchService, *, kb_uuid: str) -> None:
        self._service = search_service
        self._kb_uuid = kb_uuid

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
        if not results:
            return empty_result(
                self.tool_name,
                data={"query": query, "limit": limit, "total": 0, "results": []},
            )
        references = tuple(
            ToolReference(
                stable_id=build_stable_id(
                    self._kb_uuid, PAGE_STABLE_TYPE, result.page_id
                ),
                anchor_label=f"{result.document_title} · 第 {result.page_number} 页",
            )
            for result in results
        )
        data = {
            "query": query,
            "limit": limit,
            "total": len(results),
            "results": [_page_result_to_dict(result) for result in results],
        }
        return success_result(self.tool_name, data, references=references)


def _page_result_to_dict(result: SearchResult) -> dict[str, object]:
    return {
        "page_id": result.page_id,
        "document_id": result.document_id,
        "document_title": result.document_title,
        "filename": result.filename,
        "page_number": result.page_number,
        "snippet": result.snippet,
        "match_type": result.match_type,
        "match_fields": [field.value for field in result.match_fields],
        "rank": result.rank,
        "status": result.status.value,
        "tags": list(result.tags),
        "projects": list(result.projects),
        "updated_at": _iso_or_none(result.updated_at),
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["PAGE_SEARCH_DEFINITION", "PageSearchAdapter"]
