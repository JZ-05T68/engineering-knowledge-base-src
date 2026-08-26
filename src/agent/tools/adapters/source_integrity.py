"""``inspect_source_integrity`` read-only Tool Adapter (v0.6.0 Phase 1C).

Thin adapter over the existing read-only source-integrity path of
:class:`KnowledgeObjectService`. It accepts either a Knowledge Object
stable-id (all its source links) or a ``knowledge_source`` stable-id (one
source link). It never refreshes/recaptures fingerprints and never writes to
the database.
"""

from __future__ import annotations

from datetime import datetime

from src.agent.tools.adapters._common import (
    AdapterInputError,
    failed_result,
    internal_failure_result,
    parse_stable_id,
    partial_result,
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
from src.knowledge_object_service import (
    KnowledgeObjectNotFoundError,
    KnowledgeObjectService,
)
from src.models import (
    KNOWLEDGE_OBJECT_STABLE_TYPE,
    KNOWLEDGE_SOURCE_STABLE_TYPE,
    KnowledgeObjectSourceView,
    KnowledgeSourceStatus,
    aggregate_source_state,
    build_stable_id,
)

ALLOWED_ARGUMENTS = frozenset({"stable_id"})
MAX_STABLE_ID_LENGTH = 300

INSPECT_SOURCE_INTEGRITY_DEFINITION = ToolDefinition(
    name="inspect_source_integrity",
    description=(
        "读取来源当前已记录的完整性/指纹状态，不刷新、不重算、不写库。"
    ),
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "stable_id": {
            "type": "string",
            "required": True,
            "description": (
                "knowledge_object stable_id（查看全部来源）或 "
                "knowledge_source stable_id（查看单个来源）"
            ),
        },
    },
    timeout_seconds=30.0,
)


class InspectSourceIntegrityAdapter:
    """Execute ``inspect_source_integrity`` through read-only service methods."""

    tool_name = "inspect_source_integrity"

    def __init__(
        self, knowledge_object_service: KnowledgeObjectService, *, kb_uuid: str
    ) -> None:
        self._service = knowledge_object_service
        self._kb_uuid = kb_uuid

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, ALLOWED_ARGUMENTS)
            stable_id = require_text(
                tool_input.arguments, "stable_id", max_length=MAX_STABLE_ID_LENGTH
            )
            kb_uuid, object_type, local_id = parse_stable_id(stable_id)
            if kb_uuid != self._kb_uuid:
                return failed_result(
                    self.tool_name,
                    ToolErrorCode.NOT_FOUND,
                    "目标不属于当前知识库",
                )
            if object_type == KNOWLEDGE_OBJECT_STABLE_TYPE:
                views = self._service.source_views(local_id)
                subject_type = KNOWLEDGE_OBJECT_STABLE_TYPE
            elif object_type == KNOWLEDGE_SOURCE_STABLE_TYPE:
                views = (self._service.source_view(local_id),)
                subject_type = KNOWLEDGE_SOURCE_STABLE_TYPE
            else:
                raise AdapterInputError(
                    "stable_id 类型必须是 knowledge_object 或 knowledge_source"
                )
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except KnowledgeObjectNotFoundError as exc:
            return failed_result(
                self.tool_name,
                ToolErrorCode.NOT_FOUND,
                "目标不存在",
                detail=type(exc).__name__,
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="读取来源完整性失败"
            )
        return self._to_result(stable_id, subject_type, views)

    def _to_result(
        self,
        stable_id: str,
        subject_type: str,
        views: tuple[KnowledgeObjectSourceView, ...],
    ) -> ToolResult:
        sources = [
            _source_view_to_dict(view, self._kb_uuid) for view in views
        ]
        status_counts = {
            KnowledgeSourceStatus.VALID: 0,
            KnowledgeSourceStatus.CHANGED: 0,
            KnowledgeSourceStatus.MISSING: 0,
            KnowledgeSourceStatus.UNKNOWN: 0,
        }
        for view in views:
            status_counts[view.status] += 1
        aggregate_state = aggregate_source_state(
            status_counts[KnowledgeSourceStatus.VALID],
            status_counts[KnowledgeSourceStatus.CHANGED],
            status_counts[KnowledgeSourceStatus.MISSING],
            status_counts[KnowledgeSourceStatus.UNKNOWN],
        )
        data = {
            "stable_id": stable_id,
            "subject_type": subject_type,
            "total_sources": len(views),
            "aggregate_state": aggregate_state.value,
            "sources": sources,
        }
        references: list[ToolReference] = [
            ToolReference(stable_id=stable_id, anchor_label="完整性检查目标")
        ]
        for view in views:
            references.append(
                ToolReference(
                    stable_id=build_stable_id(
                        self._kb_uuid,
                        KNOWLEDGE_SOURCE_STABLE_TYPE,
                        view.source.id,
                    ),
                    anchor_label=(
                        f"{view.source.source_type.label} {view.source.source_id}"
                    ),
                    fingerprint_state=view.status.value,
                )
            )
        warnings: list[str] = []
        for view in views:
            if view.status is KnowledgeSourceStatus.CHANGED:
                warnings.append(
                    f"来源已变化：{view.source.source_type.label} "
                    f"{view.source.source_id}"
                )
            elif view.status is KnowledgeSourceStatus.MISSING:
                warnings.append(
                    f"来源缺失：{view.source.source_type.label} "
                    f"{view.source.source_id}"
                )
            elif view.status is KnowledgeSourceStatus.UNKNOWN:
                warnings.append(
                    f"来源状态未知：{view.source.source_type.label} "
                    f"{view.source.source_id}"
                )
        if warnings:
            return partial_result(
                self.tool_name,
                data,
                warnings=tuple(warnings),
                references=tuple(references),
            )
        return success_result(
            self.tool_name, data, references=tuple(references)
        )


def _source_view_to_dict(
    view: KnowledgeObjectSourceView, kb_uuid: str
) -> dict[str, object]:
    source = view.source
    return {
        "source_link_id": source.id,
        "source_type": source.source_type.value,
        "source_id": source.source_id,
        "source_note": source.source_note,
        "integrity_state": view.status.value,
        "fingerprint_version": source.fingerprint_version,
        "captured_at": _iso_or_none(source.captured_at),
        "stable_id": build_stable_id(
            kb_uuid, KNOWLEDGE_SOURCE_STABLE_TYPE, source.id
        ),
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "INSPECT_SOURCE_INTEGRITY_DEFINITION",
    "InspectSourceIntegrityAdapter",
]
