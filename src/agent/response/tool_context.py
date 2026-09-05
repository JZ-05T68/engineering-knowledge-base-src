"""ToolResult → KnowledgeContextPackage projection (v0.6.0 Phase 2C).

This mapper is intentionally a thin data projection, not a second RAG
pipeline. It consumes a frozen Phase 1 ``ToolResult`` and projects its
structured ``data`` + ``references`` into the existing ``ContextItem`` shape
understood by :class:`KnowledgeContextPackager`.

Rules:

- only references that have usable content become context items;
- ordering follows ``ToolResult.references`` / ``data["results"]`` exactly;
- no stable-id is invented: every item stable-id comes from a ToolReference
  or from the ToolResult data's own ``stable_id`` field;
- unsupported stable types (for example ``knowledge_source``) are skipped;
  if nothing remains the caller receives no-evidence behavior.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.agent.tools.contracts import ToolReference, ToolResult
from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackage,
    KnowledgeContextPackager,
)
from src.models import (
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    ContextRelationRef,
    ContextSourceAnchor,
)

_CONTENT_KEYS = ("content", "evidence_text", "snippet")
_SUPPORTED_TYPES = {
    "page": ContextItemType.PAGE,
    "knowledge_object": ContextItemType.KNOWLEDGE_OBJECT,
    "knowledge_memory": ContextItemType.KNOWLEDGE_MEMORY,
    "evidence": ContextItemType.EVIDENCE,
}

__all__ = ["ToolResultContextMapper"]


class ToolResultContextMapper:
    """Project one ToolResult into a KnowledgeContextPackage."""

    def build(
        self,
        tool_result: ToolResult,
        *,
        question: str,
        packager: KnowledgeContextPackager,
    ) -> KnowledgeContextPackage:
        """Build a package, or raise ``KnowledgeContextError`` when empty."""
        if not isinstance(tool_result.data, Mapping):
            raise KnowledgeContextError("ToolResult.data 必须是结构化 mapping。")
        items = self._map_items(tool_result)
        if not items:
            raise KnowledgeContextError(
                "空上下文：ToolResult 没有可投影为 evidence 的内容。"
            )
        return packager.build(items, question=question)

    def _map_items(self, tool_result: ToolResult) -> list[ContextItem]:
        data = tool_result.data
        if not isinstance(data, Mapping):
            return []
        results = data.get("results")
        if isinstance(results, list) and results:
            return self._map_result_rows(tool_result.references, results)
        item = self._map_single_item(data, tool_result.references)
        return [item] if item is not None else []

    def _map_result_rows(
        self, references: Sequence[ToolReference], rows: Sequence[object]
    ) -> list[ContextItem]:
        if len(rows) != len(references):
            raise KnowledgeContextError(
                "ToolResult data.results 与 references 数量不一致。"
            )
        items: list[ContextItem] = []
        for reference, row in zip(references, rows, strict=True):
            if not isinstance(row, Mapping):
                continue
            item = self._item_from_row(reference, row)
            if item is not None:
                items.append(item)
        return items

    def _map_single_item(
        self, data: Mapping[str, object], references: Sequence[ToolReference]
    ) -> ContextItem | None:
        data_stable_id = _first_text(data, "stable_id")
        if not references:
            return None
        stable_id = references[0].stable_id
        if data_stable_id and data_stable_id != stable_id:
            raise KnowledgeContextError(
                "ToolResult lineage 校验失败：data.stable_id 与 reference 不一致。"
            )
        context_type = _context_type(stable_id)
        if context_type is None:
            return None
        reference = references[0] if references else None
        local_id = _int_or_none(data.get("id")) or _local_id(stable_id)
        title = (
            _first_text(data, "title")
            or (reference.anchor_label if reference else "")
            or stable_id
        )
        content = _first_text(data, *_CONTENT_KEYS) or _structured_summary(data)
        if not content:
            return None
        note = _recall_presentation_note(data)
        if note:
            content = f"【表述要求】{note}\n\n{content}"
        anchors = _anchors_from_data(data) or _default_anchor(
            reference, context_type, local_id, title
        )
        relations = _relations_from_data(data)
        return ContextItem(
            type=context_type,
            local_id=local_id,
            stable_id=stable_id,
            title=title,
            content=content,
            kind=_optional_text(data.get("kind")),
            kind_label=_optional_text(data.get("kind_label")),
            status=_optional_text(data.get("status")) or "active",
            status_label=_optional_text(data.get("status_label")) or "现行",
            importance=_optional_text(data.get("importance")),
            updated_at=None,
            revision_ref=_optional_text(data.get("revision_ref")),
            source_anchors=anchors,
            relation_refs=relations,
        )

    def _item_from_row(
        self, reference: ToolReference, row: Mapping[str, object]
    ) -> ContextItem | None:
        row_stable_id = _first_text(row, "stable_id")
        stable_id = reference.stable_id
        if row_stable_id and row_stable_id != stable_id:
            raise KnowledgeContextError(
                "ToolResult lineage 校验失败：result stable_id 与 reference 不一致。"
            )
        context_type = _context_type(stable_id)
        if context_type is None:
            return None
        local_id = _int_or_none(row.get("id")) or _local_id(stable_id)
        title = (
            _first_text(row, "title")
            or _first_text(row, "document_title")
            or reference.anchor_label
            or stable_id
        )
        content = _first_text(row, *_CONTENT_KEYS)
        if not content:
            return None
        note = _recall_presentation_note(row)
        if note:
            content = f"【表述要求】{note}\n\n{content}"
        anchors = _anchors_from_row(row) or _default_anchor(
            reference, context_type, local_id, title
        )
        return ContextItem(
            type=context_type,
            local_id=local_id,
            stable_id=stable_id,
            title=title,
            content=content,
            kind=_optional_text(row.get("kind")),
            kind_label=_optional_text(row.get("kind_label")),
            status=_optional_text(row.get("status")) or "active",
            status_label=_optional_text(row.get("status_label")) or "现行",
            importance=None,
            updated_at=None,
            revision_ref=None,
            source_anchors=anchors,
            relation_refs=(),
        )


def _recall_presentation_note(row: Mapping[str, object]) -> str | None:
    """Return the recall phrasing rule for one memory row (v0.7.1).

    Memory entries carry an authority boundary that must survive into the
    answer-model context: the note is prepended to the projected content so
    the phrasing rule is literally attached to the text it governs.
    """

    note = _optional_text(row.get("presentation_note"))
    if note:
        return note
    kind = _optional_text(row.get("kind"))
    if kind == "experience":
        if row.get("root_cause_confirmed") is True:
            return (
                "这是用户整理过的经验：引用时必须先说明"
                "“你之前整理过一条经验”，可以说“你确认过这个原因”。"
            )
        return (
            "这是用户整理过的经验：引用时必须先说明"
            "“你之前整理过一条经验”，并说明其中的原因判断“未经你确认”。"
        )
    if kind == "raw_qa":
        return (
            "这是用户保存过的问答副本，不是经验：引用时必须写"
            "“你保存过的问答”，语气弱于经验，不得与经验并列成同等权威的依据。"
        )
    return None


def _context_type(stable_id: str) -> ContextItemType | None:
    parts = stable_id.split(":", 2)
    if len(parts) != 3:
        return None
    return _SUPPORTED_TYPES.get(parts[1])


def _local_id(stable_id: str) -> int:
    parts = stable_id.split(":", 2)
    try:
        return int(parts[2])
    except (IndexError, ValueError):
        return 0


def _first_text(mapping: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _default_anchor(
    reference: ToolReference | None,
    context_type: ContextItemType,
    local_id: int,
    title: str,
) -> tuple[ContextSourceAnchor, ...]:
    fingerprint = (
        reference.fingerprint_state
        if reference is not None and reference.fingerprint_state
        else ContextFingerprintState.NOT_APPLICABLE.value
    )
    label = reference.anchor_label if reference is not None and reference.anchor_label else title
    return (
        ContextSourceAnchor(
            anchor_type=context_type.value,
            anchor_id=local_id if local_id > 0 else None,
            anchor_label=label,
            fingerprint_state=fingerprint,
        ),
    )


def _anchors_from_row(row: Mapping[str, object]) -> tuple[ContextSourceAnchor, ...]:
    raw_anchors = row.get("source_anchors")
    if not isinstance(raw_anchors, list):
        return ()
    anchors: list[ContextSourceAnchor] = []
    for raw in raw_anchors:
        if not isinstance(raw, Mapping):
            continue
        source_type = _optional_text(raw.get("source_type"))
        source_id = _int_or_none(raw.get("source_id"))
        if not source_type:
            continue
        anchors.append(
            ContextSourceAnchor(
                anchor_type=source_type,
                anchor_id=source_id,
                anchor_label=f"{source_type} {source_id}" if source_id else source_type,
                fingerprint_state=ContextFingerprintState.NOT_APPLICABLE.value,
            )
        )
    return tuple(anchors)


def _anchors_from_data(
    data: Mapping[str, object],
) -> tuple[ContextSourceAnchor, ...]:
    raw_anchors = data.get("source_anchors")
    if isinstance(raw_anchors, list) and raw_anchors:
        anchors: list[ContextSourceAnchor] = []
        for raw in raw_anchors:
            if not isinstance(raw, Mapping):
                continue
            anchor_type = _optional_text(raw.get("anchor_type")) or _optional_text(
                raw.get("source_type")
            )
            anchor_id = _int_or_none(raw.get("anchor_id")) or _int_or_none(
                raw.get("source_id")
            )
            label = _optional_text(raw.get("anchor_label"))
            if not label and anchor_type:
                label = (
                    f"{anchor_type} {anchor_id}" if anchor_id is not None else anchor_type
                )
            fingerprint = _optional_text(raw.get("fingerprint_state"))
            if not anchor_type:
                continue
            anchors.append(
                ContextSourceAnchor(
                    anchor_type=anchor_type,
                    anchor_id=anchor_id,
                    anchor_label=label or anchor_type,
                    fingerprint_state=fingerprint
                    or ContextFingerprintState.NOT_APPLICABLE.value,
                )
            )
        if anchors:
            return tuple(anchors)

    raw_sources = data.get("sources")
    if isinstance(raw_sources, list) and raw_sources:
        anchors = []
        for raw in raw_sources:
            if not isinstance(raw, Mapping):
                continue
            source_type = _optional_text(raw.get("source_type"))
            source_id = raw.get("source_id")
            source_id_text = str(source_id) if source_id is not None else ""
            if not source_type:
                continue
            anchors.append(
                ContextSourceAnchor(
                    anchor_type=source_type,
                    anchor_id=_int_or_none(raw.get("source_link_id")),
                    anchor_label=f"{source_type} {source_id_text}".strip(),
                    fingerprint_state=_optional_text(raw.get("integrity_state"))
                    or ContextFingerprintState.NOT_APPLICABLE.value,
                )
            )
        if anchors:
            return tuple(anchors)
    return ()


def _relations_from_data(
    data: Mapping[str, object],
) -> tuple[ContextRelationRef, ...]:
    raw_relations = data.get("relation_refs")
    if not isinstance(raw_relations, list):
        return ()
    relations: list[ContextRelationRef] = []
    for raw in raw_relations:
        if not isinstance(raw, Mapping):
            continue
        relation_type = _optional_text(raw.get("relation_type"))
        relation_label = _optional_text(raw.get("relation_label"))
        direction = _optional_text(raw.get("direction"))
        target_stable_id = _optional_text(raw.get("target_stable_id"))
        if not target_stable_id:
            continue
        relations.append(
            ContextRelationRef(
                relation_type=relation_type or "relation",
                relation_label=relation_label or relation_type or "关联",
                direction=direction or "outgoing",
                target_stable_id=target_stable_id,
            )
        )
    return tuple(relations)


def _structured_summary(data: Mapping[str, object]) -> str:
    """Build a compact deterministic text projection for non-content tools."""
    parts: list[str] = []
    for key in (
        "title",
        "subject_type",
        "status",
        "status_label",
        "revision_ref",
        "aggregate_state",
        "total_sources",
    ):
        value = _optional_text(data.get(key))
        if value:
            parts.append(f"{key}: {value}")

    anchors = data.get("source_anchors")
    if isinstance(anchors, list):
        labels = [
            _optional_text(item.get("anchor_label")) or _optional_text(item.get("stable_id"))
            for item in anchors
            if isinstance(item, Mapping)
        ]
        if labels:
            parts.append("source_anchors: " + "、".join(label for label in labels if label))

    relations = data.get("relation_refs")
    if isinstance(relations, list):
        relation_labels = [
            f"{item.get('direction')} {item.get('relation_label')} → {item.get('target_stable_id')}"
            for item in relations
            if isinstance(item, Mapping) and item.get("target_stable_id")
        ]
        if relation_labels:
            parts.append("relation_refs: " + "；".join(relation_labels))

    sources = data.get("sources")
    if isinstance(sources, list):
        source_labels = []
        for item in sources:
            if not isinstance(item, Mapping):
                continue
            source_type = _optional_text(item.get("source_type")) or ""
            source_id = item.get("source_id")
            integrity = _optional_text(item.get("integrity_state")) or ""
            label = f"{source_type} {source_id}".strip()
            if integrity:
                label = f"{label}（{integrity}）"
            if label:
                source_labels.append(label)
        if source_labels:
            parts.append("sources: " + "、".join(source_labels))

    return "；".join(parts)
