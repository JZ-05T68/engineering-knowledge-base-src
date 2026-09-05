"""``knowledge_search`` read-only Tool Adapter (v0.6.0 Phase 1B).

Thin adapter over the existing offline :class:`KnowledgeSearchService`. The
service already groups Knowledge Object and Knowledge Memory results and
carries canonical stable-id references; this adapter only validates arguments,
calls the service, and projects results into the ToolResult envelope.
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
from src.knowledge_search_service import KnowledgeSearchService
from src.models import KnowledgeSearchResult

ALLOWED_ARGUMENTS = frozenset({"query", "limit"})
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 500

KNOWLEDGE_SEARCH_DEFINITION = ToolDefinition(
    name="knowledge_search",
    description=(
        "搜索用户的个人知识记录（Knowledge Object 与知识记忆），返回分组结构化结果。"
        "知识记忆条目带有明确类型：保存的问答（raw_qa，只是用户曾主动保存的一问一答，"
        "不是用户经验）或经验（experience）；表述时必须使用返回的类型标签。"
    ),
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "query": {
            "type": "string",
            "required": True,
            "description": "个人知识检索关键词",
        },
        "limit": {
            "type": "integer",
            "default": DEFAULT_LIMIT,
            "min": 1,
            "max": MAX_LIMIT,
            "description": "每个结果类型最多返回的结果数",
        },
    },
    timeout_seconds=30.0,
)


class KnowledgeSearchAdapter:
    """Execute ``knowledge_search`` through the existing offline service."""

    tool_name = "knowledge_search"

    def __init__(self, knowledge_search_service: KnowledgeSearchService) -> None:
        self._service = knowledge_search_service

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
                self.tool_name, exc, safe_message="知识检索执行失败"
            )
        return self._to_result(query, limit, results)

    def _to_result(
        self,
        query: str,
        limit: int,
        results: tuple[KnowledgeSearchResult, ...],
    ) -> ToolResult:
        if not results:
            return empty_result(
                self.tool_name,
                data={"query": query, "limit": limit, "total": 0, "results": []},
            )
        references = tuple(
            ToolReference(
                stable_id=result.stable_id,
                anchor_label=f"{result.result_type.label}：{result.title}",
            )
            for result in results
        )
        data = {
            "query": query,
            "limit": limit,
            "total": len(results),
            "results": [
                _knowledge_result_to_dict(result) for result in results
            ],
        }
        return success_result(self.tool_name, data, references=references)


def _knowledge_result_to_dict(result: KnowledgeSearchResult) -> dict[str, object]:
    return {
        "result_type": result.result_type.value,
        "id": result.id,
        "stable_id": result.stable_id,
        "title": result.title,
        "snippet": result.snippet,
        "status": result.status,
        "status_label": result.status_label,
        "kind": result.kind,
        "kind_label": result.kind_label,
        "updated_at": _iso_or_none(result.updated_at),
        "knowledge_object_id": result.knowledge_object_id,
        "document_id": result.document_id,
        "page_id": result.page_id,
        "source_anchors": [
            {"source_type": source_type, "source_id": source_id}
            for source_type, source_id in result.source_anchors
        ],
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["KNOWLEDGE_SEARCH_DEFINITION", "KnowledgeSearchAdapter"]
