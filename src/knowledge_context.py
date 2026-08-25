"""Read-only ContextItem projections for the RAG context layer (v0.5.3 Phase 2-B).

The projector turns the four first-class local entities — page, knowledge
object, knowledge memory and evidence — into the single frozen ``ContextItem``
shape defined in ``src.models``. Projection is strictly read-only:

- it never writes to the database (no fingerprint recapture, no backfill);
- it never infers, summarises, or fabricates missing fields;
- it never creates relations — ``relation_refs`` only mirrors already-stored
  direct relations;
- ``stable_id`` always comes from the canonical ``build_stable_id`` namespace.
"""

from __future__ import annotations

from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_object_service import KnowledgeObjectService
from src.models import (
    EVIDENCE_STABLE_TYPE,
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    ContextAnchorType,
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    ContextRelationRef,
    ContextSourceAnchor,
    build_stable_id,
)


class ContextProjectionError(ValueError):
    """Raised when an entity cannot be projected into a ContextItem."""


class ContextItemProjector:
    """Project local entities into the unified, read-only ContextItem shape."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._evidence_service = EvidenceBasketService(database)

    def project(self, item_type: ContextItemType | str, local_id: int) -> ContextItem:
        """Dispatch one projection by entity type and local primary key."""

        normalized = ContextItemType(item_type)
        if normalized is ContextItemType.PAGE:
            return self.project_page(local_id)
        if normalized is ContextItemType.KNOWLEDGE_OBJECT:
            return self.project_knowledge_object(local_id)
        if normalized is ContextItemType.KNOWLEDGE_MEMORY:
            return self.project_knowledge_memory(local_id)
        if normalized is ContextItemType.EVIDENCE:
            return self.project_evidence(local_id)
        raise ContextProjectionError(f"不支持的上下文类型：{item_type}")

    # ------------------------------------------------------------------ page
    def project_page(self, page_id: int) -> ContextItem:
        page = self._database.get_page(page_id)
        if page is None:
            raise ContextProjectionError(f"页面不存在：{page_id}")
        document = self._database.get_document(page.document_id)
        if document is None:
            raise ContextProjectionError(f"页面 {page_id} 的文档不存在：{page.document_id}")

        content = (
            page.extracted_text.strip()
            or page.ocr_text.strip()
            or page.markdown_content.strip()
        )
        anchors = (
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.DOCUMENT.value,
                anchor_id=document.id,
                anchor_label=f"文档 {document.id}：{document.title}",
                fingerprint_state=ContextFingerprintState.NOT_APPLICABLE.value,
            ),
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.PAGE.value,
                anchor_id=page.id,
                anchor_label=f"第 {page.page_number} 页",
                fingerprint_state=ContextFingerprintState.NOT_APPLICABLE.value,
            ),
        )
        return ContextItem(
            type=ContextItemType.PAGE,
            local_id=page.id,
            stable_id=build_stable_id(
                self._database.get_knowledge_base_uuid(),
                PAGE_STABLE_TYPE,
                page.id,
            ),
            title=f"{document.title} · 第 {page.page_number} 页",
            content=content,
            kind=None,
            kind_label=None,
            status=page.status.value,
            status_label=page.status.label,
            importance=None,
            updated_at=page.updated_at,
            revision_ref=None,
            source_anchors=anchors,
            relation_refs=(),
        )

    # ------------------------------------------------------- knowledge object
    def project_knowledge_object(self, knowledge_object_id: int) -> ContextItem:
        knowledge_object = self._database.get_knowledge_object(knowledge_object_id)
        if knowledge_object is None:
            raise ContextProjectionError(f"知识对象不存在：{knowledge_object_id}")
        service = KnowledgeObjectService(self._database)
        source_views = service.source_views(knowledge_object_id)
        relations = self._database.list_knowledge_relations(knowledge_object_id)

        anchors = tuple(
            ContextSourceAnchor(
                anchor_type=view.source.source_type.value,
                anchor_id=view.source.source_id,
                anchor_label=(
                    f"{view.source.source_type.label} {view.source.source_id}"
                    f"（{view.source.source_note}）"
                    if view.source.source_note.strip()
                    else f"{view.source.source_type.label} {view.source.source_id}"
                ),
                fingerprint_state=view.status.value,
            )
            for view in source_views
        )
        relation_refs = tuple(
            ContextRelationRef(
                relation_type=relation.relation_type.value,
                relation_label=relation.relation_type.label,
                direction=(
                    "outgoing"
                    if relation.source_ko_id == knowledge_object_id
                    else "incoming"
                ),
                target_stable_id=self._database.knowledge_object_stable_id(
                    relation.target_ko_id
                    if relation.source_ko_id == knowledge_object_id
                    else relation.source_ko_id
                ),
            )
            for relation in relations
        )
        revision_ref = (
            f"当前第 {knowledge_object.current_revision} 版"
            f"（确认第 {knowledge_object.confirmed_revision} 版）"
            if knowledge_object.confirmed_revision is not None
            else f"当前第 {knowledge_object.current_revision} 版"
        )
        return ContextItem(
            type=ContextItemType.KNOWLEDGE_OBJECT,
            local_id=knowledge_object.id,
            stable_id=self._database.knowledge_object_stable_id(knowledge_object.id),
            title=knowledge_object.title,
            content=knowledge_object.content,
            kind=knowledge_object.kind.value,
            kind_label=knowledge_object.kind.label,
            status=knowledge_object.lifecycle.value,
            status_label=knowledge_object.lifecycle.label,
            importance=knowledge_object.importance.value,
            updated_at=knowledge_object.updated_at,
            revision_ref=revision_ref,
            source_anchors=anchors,
            relation_refs=relation_refs,
        )

    # -------------------------------------------------------- knowledge memory
    def project_knowledge_memory(self, entry_id: int) -> ContextItem:
        entry = self._database.get_knowledge_memory_entry(entry_id)
        if entry is None:
            raise ContextProjectionError(f"知识记忆不存在：{entry_id}")
        anchors: list[ContextSourceAnchor] = []
        if entry.knowledge_object_id is not None:
            anchors.append(
                ContextSourceAnchor(
                    anchor_type="knowledge_object",
                    anchor_id=entry.knowledge_object_id,
                    anchor_label=f"知识对象 {entry.knowledge_object_id}",
                )
            )
        if entry.document_id is not None:
            anchors.append(
                ContextSourceAnchor(
                    anchor_type=ContextAnchorType.DOCUMENT.value,
                    anchor_id=entry.document_id,
                    anchor_label=f"文档 {entry.document_id}",
                )
            )
        if entry.page_id is not None:
            anchors.append(
                ContextSourceAnchor(
                    anchor_type=ContextAnchorType.PAGE.value,
                    anchor_id=entry.page_id,
                    anchor_label=f"页面 {entry.page_id}",
                )
            )
        parts = [entry.content.strip()]
        if entry.root_cause.strip():
            parts.append(f"根因：{entry.root_cause.strip()}")
        if entry.lesson.strip():
            parts.append(f"教训：{entry.lesson.strip()}")
        if entry.outcome.strip():
            parts.append(f"结果：{entry.outcome.strip()}")
        if entry.context_conditions.strip():
            parts.append(f"适用条件：{entry.context_conditions.strip()}")
        return ContextItem(
            type=ContextItemType.KNOWLEDGE_MEMORY,
            local_id=entry.id,
            stable_id=build_stable_id(
                self._database.get_knowledge_base_uuid(),
                KNOWLEDGE_MEMORY_STABLE_TYPE,
                entry.id,
            ),
            title=entry.title,
            content="\n".join(part for part in parts if part),
            kind=entry.kind.value,
            kind_label=entry.kind.label,
            status=entry.status.value,
            status_label=entry.status.label,
            importance=None,
            updated_at=entry.updated_at,
            revision_ref=f"第 {entry.content_revision} 版",
            source_anchors=tuple(anchors),
            relation_refs=(),
        )

    # --------------------------------------------------------------- evidence
    def project_evidence(self, evidence_id: int) -> ContextItem:
        evidence = self._evidence_service.get_item(evidence_id)
        if evidence is None:
            raise ContextProjectionError(f"证据条目不存在：{evidence_id}")
        document = self._database.get_document(evidence.document_id)
        page = self._database.get_page(evidence.page_id)
        page_label = (
            f"第 {page.page_number} 页"
            if page is not None
            else f"页面 {evidence.page_id}"
        )
        anchors = (
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.DOCUMENT.value,
                anchor_id=evidence.document_id,
                anchor_label=(
                    f"文档 {evidence.document_id}"
                    + (f"：{document.title}" if document is not None else "")
                ),
            ),
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.PAGE.value,
                anchor_id=evidence.page_id,
                anchor_label=page_label,
            ),
        )
        return ContextItem(
            type=ContextItemType.EVIDENCE,
            local_id=evidence.id,
            stable_id=build_stable_id(
                self._database.get_knowledge_base_uuid(),
                EVIDENCE_STABLE_TYPE,
                evidence.id,
            ),
            title=evidence.evidence_text.strip()[:120] or "证据条目",
            content=evidence.evidence_text,
            kind=evidence.evidence_type.value,
            kind_label=evidence.evidence_type.label,
            status=evidence.confirmation_status.value,
            status_label=evidence.confirmation_status.label,
            importance=None,
            updated_at=evidence.added_at,
            revision_ref=None,
            source_anchors=anchors,
            relation_refs=(),
        )


__all__ = [
    "ContextItemProjector",
    "ContextProjectionError",
]
