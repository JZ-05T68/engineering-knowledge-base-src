"""``inspect_provenance`` read-only Tool Adapter (v0.6.0 Phase 1C).

Thin adapter over the existing read-only :class:`ContextItemProjector`: it
resolves a canonical stable-id, projects the entity into the unified
``ContextItem`` shape, and extracts only the provenance-relevant surface
(source anchors, relations, revision reference, warnings). It does not copy the
full entity content and never derives provenance itself.
"""

from __future__ import annotations

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
from src.knowledge_context import ContextItemProjector, ContextProjectionError
from src.models import (
    EVIDENCE_STABLE_TYPE,
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    KNOWLEDGE_OBJECT_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    ContextAnchorType,
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    build_stable_id,
)

ALLOWED_ARGUMENTS = frozenset({"stable_id"})
MAX_STABLE_ID_LENGTH = 300

INSPECT_PROVENANCE_DEFINITION = ToolDefinition(
    name="inspect_provenance",
    description="读取一个知识资产已记录的来源锚点与追溯关系，不复制正文。",
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "stable_id": {
            "type": "string",
            "required": True,
            "description": (
                "目标 stable_id，支持 page / knowledge_object / "
                "knowledge_memory / evidence"
            ),
        },
    },
    timeout_seconds=30.0,
)

_STABLE_TYPE_TO_CONTEXT_TYPE = {
    PAGE_STABLE_TYPE: ContextItemType.PAGE,
    KNOWLEDGE_OBJECT_STABLE_TYPE: ContextItemType.KNOWLEDGE_OBJECT,
    KNOWLEDGE_MEMORY_STABLE_TYPE: ContextItemType.KNOWLEDGE_MEMORY,
    EVIDENCE_STABLE_TYPE: ContextItemType.EVIDENCE,
}

# Anchor types that have a canonical stable-id namespace. DOCUMENT / NOTE /
# SELECTION / IMAGE_REGION anchors remain in data but are never fabricated into
# ToolResult.references.
_ANCHOR_STABLE_TYPES = {
    ContextAnchorType.PAGE.value: PAGE_STABLE_TYPE,
    ContextAnchorType.EVIDENCE.value: EVIDENCE_STABLE_TYPE,
    "knowledge_object": KNOWLEDGE_OBJECT_STABLE_TYPE,
}


class InspectProvenanceAdapter:
    """Execute ``inspect_provenance`` through the read-only ContextItemProjector."""

    tool_name = "inspect_provenance"

    def __init__(self, projector: ContextItemProjector, *, kb_uuid: str) -> None:
        self._projector = projector
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
            context_type = _STABLE_TYPE_TO_CONTEXT_TYPE.get(object_type)
            if context_type is None:
                raise AdapterInputError(
                    "stable_id 类型必须是 page / knowledge_object / "
                    "knowledge_memory / evidence"
                )
            item = self._projector.project(context_type, local_id)
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except ContextProjectionError as exc:
            return failed_result(
                self.tool_name,
                ToolErrorCode.NOT_FOUND,
                "目标不存在或无法投影 provenance",
                detail=type(exc).__name__,
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="读取 provenance 失败"
            )
        return self._to_result(stable_id, item)

    def _to_result(self, stable_id: str, item: ContextItem) -> ToolResult:
        anchors = [
            _anchor_to_dict(anchor, self._kb_uuid) for anchor in item.source_anchors
        ]
        references: list[ToolReference] = [
            ToolReference(stable_id=stable_id, anchor_label=item.title)
        ]
        for anchor in item.source_anchors:
            reference = _anchor_reference(anchor, self._kb_uuid)
            if reference is not None:
                references.append(reference)
        warnings: list[str] = []
        for anchor in item.source_anchors:
            if anchor.fingerprint_state == ContextFingerprintState.CHANGED.value:
                warnings.append(
                    f"来源已变化：{anchor.anchor_label}"
                )
            elif anchor.fingerprint_state == ContextFingerprintState.MISSING.value:
                warnings.append(
                    f"来源缺失：{anchor.anchor_label}"
                )
            elif anchor.fingerprint_state == ContextFingerprintState.UNKNOWN.value:
                warnings.append(
                    f"来源状态未知：{anchor.anchor_label}"
                )
        if not item.source_anchors:
            warnings.append("该对象没有可追溯的来源锚点")

        data = {
            "stable_id": stable_id,
            "subject_type": item.type.value,
            "title": item.title,
            "status": item.status,
            "status_label": item.status_label,
            "revision_ref": item.revision_ref,
            "source_anchors": anchors,
            "relation_refs": [
                {
                    "relation_type": relation.relation_type,
                    "relation_label": relation.relation_label,
                    "direction": relation.direction,
                    "target_stable_id": relation.target_stable_id,
                }
                for relation in item.relation_refs
            ],
        }
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


def _anchor_to_dict(anchor: object, kb_uuid: str) -> dict[str, object]:
    anchor_type = str(anchor.anchor_type)
    anchor_id = anchor.anchor_id
    return {
        "anchor_type": anchor_type,
        "anchor_id": anchor_id,
        "anchor_label": anchor.anchor_label,
        "fingerprint_state": anchor.fingerprint_state,
        "stable_id": _anchor_stable_id_or_none(anchor, kb_uuid),
    }


def _anchor_reference(anchor: object, kb_uuid: str) -> ToolReference | None:
    stable_id = _anchor_stable_id_or_none(anchor, kb_uuid)
    if stable_id is None:
        return None
    return ToolReference(
        stable_id=stable_id,
        anchor_label=anchor.anchor_label,
        fingerprint_state=anchor.fingerprint_state,
    )


def _anchor_stable_id_or_none(anchor: object, kb_uuid: str) -> str | None:
    stable_type = _ANCHOR_STABLE_TYPES.get(str(anchor.anchor_type))
    if stable_type is None or anchor.anchor_id is None:
        return None
    return build_stable_id(kb_uuid, stable_type, int(anchor.anchor_id))


__all__ = ["INSPECT_PROVENANCE_DEFINITION", "InspectProvenanceAdapter"]
