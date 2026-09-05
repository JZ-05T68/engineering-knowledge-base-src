"""``get_knowledge_object`` / ``get_knowledge_memory`` read-only Adapters.

Both adapters are deliberately thin: they parse a canonical stable-id, validate
the object type against the current knowledge base, delegate to the existing
domain service read method, and project the existing domain model into a
structured ToolResult. No query SQL, lifecycle rule, provenance calculation,
or source fingerprint refresh is reimplemented here.
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
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import (
    KnowledgeObjectNotFoundError,
    KnowledgeObjectService,
)
from src.models import (
    EVIDENCE_STABLE_TYPE,
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    KNOWLEDGE_OBJECT_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    KnowledgeMemoryEntry,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeObjectView,
    KnowledgeSourceStatus,
    build_stable_id,
)

GET_KNOWLEDGE_OBJECT_DEFINITION = ToolDefinition(
    name="get_knowledge_object",
    description="按 stable_id 读取一个已整理的 Knowledge Object 及其来源状态。",
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "stable_id": {
            "type": "string",
            "required": True,
            "description": "知识对象 stable_id，格式 <kb_uuid>:knowledge_object:<id>",
        },
    },
    timeout_seconds=30.0,
)

GET_KNOWLEDGE_MEMORY_DEFINITION = ToolDefinition(
    name="get_knowledge_memory",
    description=(
        "按 stable_id 读取一条个人知识记录。结果带有明确的记录类型："
        "保存的问答（raw_qa，只是用户曾主动留下的一问一答副本，不是用户经验或已确认事实）"
        "或经验（experience）。必须按返回的类型表述，不得把 raw_qa 称为用户经验。"
    ),
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "stable_id": {
            "type": "string",
            "required": True,
            "description": "知识记忆 stable_id，格式 <kb_uuid>:knowledge_memory:<id>",
        },
    },
    timeout_seconds=30.0,
)

_READ_ALLOWED_ARGUMENTS = frozenset({"stable_id"})
_MAX_STABLE_ID_LENGTH = 300


class KnowledgeObjectAdapter:
    """Execute ``get_knowledge_object`` through KnowledgeObjectService."""

    tool_name = "get_knowledge_object"

    def __init__(
        self, knowledge_object_service: KnowledgeObjectService, *, kb_uuid: str
    ) -> None:
        self._service = knowledge_object_service
        self._kb_uuid = kb_uuid

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, _READ_ALLOWED_ARGUMENTS)
            stable_id = require_text(
                tool_input.arguments, "stable_id", max_length=_MAX_STABLE_ID_LENGTH
            )
            kb_uuid, object_type, local_id = parse_stable_id(stable_id)
            if object_type != KNOWLEDGE_OBJECT_STABLE_TYPE:
                raise AdapterInputError(
                    f"stable_id 类型必须是 {KNOWLEDGE_OBJECT_STABLE_TYPE}"
                )
            if kb_uuid != self._kb_uuid:
                return failed_result(
                    self.tool_name,
                    ToolErrorCode.NOT_FOUND,
                    "知识对象不属于当前知识库",
                )
            view = self._service.get_view(local_id)
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except KnowledgeObjectNotFoundError as exc:
            return failed_result(
                self.tool_name,
                ToolErrorCode.NOT_FOUND,
                "知识对象不存在",
                detail=type(exc).__name__,
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="读取知识对象失败"
            )
        return self._to_result(stable_id, view)

    def _to_result(
        self, stable_id: str, view: KnowledgeObjectView
    ) -> ToolResult:
        knowledge_object = view.knowledge_object
        data = {
            "stable_id": stable_id,
            "id": knowledge_object.id,
            "kind": knowledge_object.kind.value,
            "kind_label": knowledge_object.kind.label,
            "authorship": knowledge_object.authorship.value,
            "epistemic_basis": knowledge_object.epistemic_basis.value,
            "title": knowledge_object.title,
            "content": knowledge_object.content,
            "content_length": len(knowledge_object.content),
            "importance": knowledge_object.importance.value,
            "lifecycle": knowledge_object.lifecycle.value,
            "confirmation_status": knowledge_object.confirmation_status.value,
            "confirmation_is_current": knowledge_object.confirmation_is_current,
            "confirmed_at": _iso_or_none(knowledge_object.confirmed_at),
            "current_revision": knowledge_object.current_revision,
            "confirmed_revision": knowledge_object.confirmed_revision,
            "created_at": _iso_or_none(knowledge_object.created_at),
            "updated_at": _iso_or_none(knowledge_object.updated_at),
            "sources": [
                _source_view_to_dict(source, self._kb_uuid)
                for source in view.sources
            ],
            "outgoing_relations": [
                _relation_to_dict(relation) for relation in view.outgoing_relations
            ],
            "incoming_relations": [
                _relation_to_dict(relation) for relation in view.incoming_relations
            ],
        }
        references: list[ToolReference] = [
            ToolReference(
                stable_id=stable_id,
                anchor_label=knowledge_object.title,
            )
        ]
        warnings: list[str] = []
        for source in view.sources:
            source_reference = _source_reference(self._kb_uuid, source)
            if source_reference is not None:
                references.append(source_reference)
            if source.status is KnowledgeSourceStatus.CHANGED:
                warnings.append(
                    f"来源已变化：{source.source.source_type.label} "
                    f"{source.source.source_id}"
                )
            elif source.status is KnowledgeSourceStatus.MISSING:
                warnings.append(
                    f"来源缺失：{source.source.source_type.label} "
                    f"{source.source.source_id}"
                )
            elif source.status is KnowledgeSourceStatus.UNKNOWN:
                warnings.append(
                    f"来源状态未知：{source.source.source_type.label} "
                    f"{source.source.source_id}"
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


class KnowledgeMemoryAdapter:
    """Execute ``get_knowledge_memory`` through KnowledgeMemoryService."""

    tool_name = "get_knowledge_memory"

    def __init__(
        self, knowledge_memory_service: KnowledgeMemoryService, *, kb_uuid: str
    ) -> None:
        self._service = knowledge_memory_service
        self._kb_uuid = kb_uuid

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, _READ_ALLOWED_ARGUMENTS)
            stable_id = require_text(
                tool_input.arguments, "stable_id", max_length=_MAX_STABLE_ID_LENGTH
            )
            kb_uuid, object_type, local_id = parse_stable_id(stable_id)
            if kb_uuid != self._kb_uuid:
                return failed_result(
                    self.tool_name,
                    ToolErrorCode.NOT_FOUND,
                    "知识记忆不属于当前知识库",
                )
            if object_type != KNOWLEDGE_MEMORY_STABLE_TYPE:
                raise AdapterInputError(
                    f"stable_id 类型必须是 {KNOWLEDGE_MEMORY_STABLE_TYPE}"
                )
            entry = self._service.get(local_id)
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="读取知识记忆失败"
            )
        if entry is None:
            return failed_result(
                self.tool_name,
                ToolErrorCode.NOT_FOUND,
                "知识记忆不存在",
            )
        return self._to_result(stable_id, entry)

    def _to_result(
        self, stable_id: str, entry: KnowledgeMemoryEntry
    ) -> ToolResult:
        data = {
            "stable_id": stable_id,
            "id": entry.id,
            "kind": entry.kind.value,
            "kind_label": entry.kind.label,
            "record_type_note": (
                "保存的问答：用户曾主动保存的一问一答副本，不是用户经验或已确认事实"
                if entry.kind.value == "raw_qa"
                else entry.kind.label
            ),
            "creation_origin": entry.creation_origin,
            "title": entry.title,
            "content": entry.content,
            "content_length": len(entry.content),
            "root_cause": entry.root_cause,
            "lesson": entry.lesson,
            "outcome": entry.outcome,
            "context_conditions": entry.context_conditions,
            "status": entry.status.value,
            "status_label": entry.status.label,
            "root_cause_confirmed": entry.root_cause_confirmed,
            "source_entry_id": entry.source_entry_id,
            "source_title": entry.source_title,
            "knowledge_object_id": entry.knowledge_object_id,
            "document_id": entry.document_id,
            "page_id": entry.page_id,
            "content_revision": entry.content_revision,
            "created_at": _iso_or_none(entry.created_at),
            "updated_at": _iso_or_none(entry.updated_at),
        }
        references: list[ToolReference] = [
            ToolReference(stable_id=stable_id, anchor_label=entry.title)
        ]
        if entry.knowledge_object_id is not None:
            references.append(
                ToolReference(
                    stable_id=build_stable_id(
                        self._kb_uuid,
                        KNOWLEDGE_OBJECT_STABLE_TYPE,
                        entry.knowledge_object_id,
                    ),
                    anchor_label=f"知识对象 {entry.knowledge_object_id}",
                )
            )
        if entry.page_id is not None:
            references.append(
                ToolReference(
                    stable_id=build_stable_id(
                        self._kb_uuid, PAGE_STABLE_TYPE, entry.page_id
                    ),
                    anchor_label=f"页面 {entry.page_id}",
                )
            )
        return success_result(
            self.tool_name, data, references=tuple(references)
        )


def _source_view_to_dict(
    source_view: KnowledgeObjectSourceView, kb_uuid: str
) -> dict[str, object]:
    source = source_view.source
    return {
        "source_type": source.source_type.value,
        "source_id": source.source_id,
        "source_note": source.source_note,
        "status": source_view.status.value,
        "stable_id": _source_stable_id_or_none(kb_uuid, source_view),
    }


def _source_reference(
    kb_uuid: str, source_view: KnowledgeObjectSourceView
) -> ToolReference | None:
    stable_id = _source_stable_id_or_none(kb_uuid, source_view)
    if stable_id is None:
        return None
    source = source_view.source
    return ToolReference(
        stable_id=stable_id,
        anchor_label=f"{source.source_type.label} {source.source_id}",
        fingerprint_state=source_view.status.value,
    )


def _source_stable_id_or_none(
    kb_uuid: str, source_view: KnowledgeObjectSourceView
) -> str | None:
    source = source_view.source
    if source.source_type is KnowledgeObjectSourceType.PAGE:
        return build_stable_id(kb_uuid, PAGE_STABLE_TYPE, source.source_id)
    if source.source_type is KnowledgeObjectSourceType.EVIDENCE:
        return build_stable_id(kb_uuid, EVIDENCE_STABLE_TYPE, source.source_id)
    return None


def _relation_to_dict(relation: object) -> dict[str, object]:
    return {
        "id": relation.id,
        "relation_type": relation.relation_type.value,
        "source_ko_id": relation.source_ko_id,
        "target_ko_id": relation.target_ko_id,
        "description": relation.description,
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "GET_KNOWLEDGE_MEMORY_DEFINITION",
    "GET_KNOWLEDGE_OBJECT_DEFINITION",
    "KnowledgeMemoryAdapter",
    "KnowledgeObjectAdapter",
]
