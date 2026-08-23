"""Knowledge object service (v0.5.2).

Domain service for the schema v9 ``knowledge_objects`` entity: creation,
updating, listing, source linking, typed relations, and the automatic
append-only ``knowledge_change`` memory log. The service owns business rules
that the database layer deliberately does not duplicate:

- a knowledge object may only become ``reviewed`` when at least one of its
  source links is still valid;
- every user-visible mutation writes one ``knowledge_change`` memory entry,
  so knowledge never changes silently.

The service never touches original PDFs, page images, notes or evidence rows:
source links are read-only references whose target existence is re-checked on
every read.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from src.database import Database, DatabaseError
from src.models import (
    KnowledgeMemoryEntryKind,
    KnowledgeObject,
    KnowledgeObjectKind,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeObjectStatus,
    KnowledgeObjectView,
    KnowledgeRelation,
    KnowledgeRelationType,
    KnowledgeSourceStatus,
    NoteImportance,
)

LOGGER = logging.getLogger(__name__)

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100


class KnowledgeObjectError(DatabaseError):
    """Base class for knowledge-object service failures."""


class KnowledgeObjectNotFoundError(KnowledgeObjectError):
    """The requested knowledge object does not exist."""


class KnowledgeObjectValidationError(KnowledgeObjectError):
    """Caller-supplied knowledge-object data failed business validation."""


class KnowledgeSourceLinkError(KnowledgeObjectError):
    """A source link is invalid, duplicate, or points to a missing target."""


class KnowledgeObjectService:
    """Domain service for durable, source-linked knowledge objects."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------- queries
    def get(self, knowledge_object_id: int) -> KnowledgeObject | None:
        """Return one knowledge object by primary key, or ``None``."""

        return self._database.get_knowledge_object(knowledge_object_id)

    def get_view(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Return one object with its source links and both relation directions."""

        knowledge_object = self._require_knowledge_object(knowledge_object_id)
        sources = self.source_views(knowledge_object_id)
        relations = self._database.list_knowledge_relations(knowledge_object_id)
        outgoing = tuple(
            relation
            for relation in relations
            if relation.source_ko_id == knowledge_object_id
        )
        incoming = tuple(
            relation
            for relation in relations
            if relation.target_ko_id == knowledge_object_id
        )
        return KnowledgeObjectView(
            knowledge_object=knowledge_object,
            sources=sources,
            outgoing_relations=outgoing,
            incoming_relations=incoming,
        )

    def list(
        self,
        *,
        kind: KnowledgeObjectKind | str | None = None,
        importance: NoteImportance | str | None = None,
        status: KnowledgeObjectStatus | str | None = None,
        query: str = "",
        sort_by: str = "updated_desc",
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> list[KnowledgeObject]:
        """List knowledge objects with stable filters, keyword search and sorting."""

        self._validate_pagination(limit, offset)
        return self._database.list_knowledge_objects(
            kind=kind,
            importance=importance,
            status=status,
            query=query,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        *,
        kind: KnowledgeObjectKind | str | None = None,
        importance: NoteImportance | str | None = None,
        status: KnowledgeObjectStatus | str | None = None,
        query: str = "",
    ) -> int:
        """Count knowledge objects with exactly the same filters as ``list``."""

        return self._database.count_knowledge_objects(
            kind=kind, importance=importance, status=status, query=query
        )

    # ------------------------------------------------------------- mutation
    def create(
        self,
        *,
        kind: KnowledgeObjectKind | str,
        title: str,
        content: str,
        importance: NoteImportance | str = NoteImportance.NORMAL,
        status: KnowledgeObjectStatus | str = KnowledgeObjectStatus.DRAFT,
        source_links: Sequence[
            tuple[KnowledgeObjectSourceType | str, int, str]
        ] = (),
    ) -> KnowledgeObjectView:
        """Create one knowledge object and optionally link its sources.

        ``source_links`` items are ``(source_type, source_id, source_note)``.
        A reviewed object must have at least one valid source link at creation
        time; a draft object may be created without any source.
        """

        normalized_status = KnowledgeObjectStatus(status)
        normalized_links = self._validate_source_links(source_links)
        if normalized_status is KnowledgeObjectStatus.REVIEWED:
            valid_targets = [
                link for link in normalized_links if self._source_target_exists(*link[:2])
            ]
            if not valid_targets:
                raise KnowledgeObjectValidationError(
                    "知识对象设为“已复核”前必须至少关联一个有效来源。"
                )
        knowledge_object = self._database.create_knowledge_object(
            kind=kind,
            title=title,
            content=content,
            importance=importance,
            status=status,
        )
        for source_type, source_id, source_note in normalized_links:
            self._database.add_knowledge_object_source(
                knowledge_object_id=knowledge_object.id,
                source_type=source_type,
                source_id=source_id,
                source_note=source_note,
            )
        self._log_change(
            knowledge_object,
            "创建",
            f"创建知识对象「{knowledge_object.title}」（{knowledge_object.kind.label}）。",
        )
        return self.get_view(knowledge_object.id)

    def update(
        self,
        knowledge_object_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        importance: NoteImportance | str | None = None,
        status: KnowledgeObjectStatus | str | None = None,
    ) -> KnowledgeObjectView:
        """Update one knowledge object, enforcing the reviewed-source rule."""

        current = self._require_knowledge_object(knowledge_object_id)
        normalized_status = (
            KnowledgeObjectStatus(status) if status is not None else current.status
        )
        if normalized_status is KnowledgeObjectStatus.REVIEWED:
            has_valid_source = any(
                view.status is KnowledgeSourceStatus.VALID
                for view in self.source_views(knowledge_object_id)
            )
            if not has_valid_source:
                raise KnowledgeObjectValidationError(
                    "知识对象设为“已复核”前必须至少关联一个有效来源。"
                )
        updated = self._database.update_knowledge_object(
            knowledge_object_id,
            title=title,
            content=content,
            importance=importance,
            status=status,
        )
        changed = (
            (title is not None and updated.title != current.title)
            or (content is not None and updated.content != current.content)
            or (importance is not None and updated.importance is not current.importance)
            or (normalized_status is not current.status)
        )
        if changed:
            self._log_change(
                updated, "更新", f"更新知识对象「{updated.title}」。"
            )
        return self.get_view(knowledge_object_id)

    def delete(self, knowledge_object_id: int) -> None:
        """Delete one knowledge object; sources and relations cascade away.

        Never touches original PDFs, page images, notes or evidence rows.
        Memory entries survive with ``knowledge_object_id`` set to NULL.
        """

        self._require_knowledge_object(knowledge_object_id)
        self._database.delete_knowledge_object(knowledge_object_id)

    # --------------------------------------------------------------- sources
    def source_views(self, knowledge_object_id: int) -> tuple[KnowledgeObjectSourceView, ...]:
        """Return every source link with a freshly recomputed existence status."""

        self._require_knowledge_object(knowledge_object_id)
        sources = self._database.list_knowledge_object_sources(knowledge_object_id)
        return tuple(
            KnowledgeObjectSourceView(
                source=source,
                status=(
                    KnowledgeSourceStatus.VALID
                    if self._source_target_exists(source.source_type, source.source_id)
                    else KnowledgeSourceStatus.MISSING
                ),
            )
            for source in sources
        )

    def link_source(
        self,
        knowledge_object_id: int,
        *,
        source_type: KnowledgeObjectSourceType | str,
        source_id: int,
        source_note: str = "",
    ) -> KnowledgeObjectSourceView:
        """Link one source entity to a knowledge object after existence check."""

        knowledge_object = self._require_knowledge_object(knowledge_object_id)
        normalized_type = KnowledgeObjectSourceType(source_type)
        if not self._source_target_exists(normalized_type, source_id):
            raise KnowledgeSourceLinkError(
                f"来源不存在：{normalized_type.label} {source_id}"
            )
        try:
            source = self._database.add_knowledge_object_source(
                knowledge_object_id=knowledge_object_id,
                source_type=normalized_type,
                source_id=source_id,
                source_note=source_note,
            )
        except DatabaseError as exc:
            raise KnowledgeSourceLinkError(str(exc)) from exc
        self._log_change(
            knowledge_object,
            "关联来源",
            f"为知识对象「{knowledge_object.title}」关联{normalized_type.label} "
            f"{source_id}。",
        )
        return KnowledgeObjectSourceView(
            source=source, status=KnowledgeSourceStatus.VALID
        )

    def unlink_source(self, source_id: int) -> None:
        """Remove one source link; the source material itself is never touched."""

        source = self._database.get_knowledge_object_source(source_id)
        if source is None:
            raise KnowledgeSourceLinkError(f"知识对象来源不存在：{source_id}")
        self._database.remove_knowledge_object_source(source_id)
        knowledge_object = self._database.get_knowledge_object(
            source.knowledge_object_id
        )
        if knowledge_object is not None:
            self._log_change(
                knowledge_object,
                "解除来源",
                f"解除知识对象「{knowledge_object.title}」的"
                f"{source.source_type.label} {source.source_id} 来源。",
            )

    # ------------------------------------------------------------- relations
    def add_relation(
        self,
        source_ko_id: int,
        target_ko_id: int,
        *,
        relation_type: KnowledgeRelationType | str,
        description: str = "",
    ) -> KnowledgeRelation:
        """Create one typed directed relation between two knowledge objects."""

        source = self._require_knowledge_object(source_ko_id)
        target = self._require_knowledge_object(target_ko_id)
        try:
            relation = self._database.add_knowledge_relation(
                source_ko_id=source_ko_id,
                target_ko_id=target_ko_id,
                relation_type=relation_type,
                description=description,
            )
        except (DatabaseError, ValueError) as exc:
            raise KnowledgeObjectValidationError(str(exc)) from exc
        normalized_type = KnowledgeRelationType(relation_type)
        self._log_change(
            source,
            "建立关系",
            f"「{source.title}」—{normalized_type.label}→「{target.title}」。",
        )
        return relation

    def remove_relation(self, relation_id: int) -> None:
        """Delete one relation row; both knowledge objects remain untouched."""

        relation = self._database.get_knowledge_relation(relation_id)
        if relation is None:
            raise KnowledgeObjectNotFoundError(f"知识关系不存在：{relation_id}")
        self._database.remove_knowledge_relation(relation_id)

    def relations(self, knowledge_object_id: int) -> list[KnowledgeRelation]:
        """List every relation touching one object (both directions)."""

        self._require_knowledge_object(knowledge_object_id)
        return self._database.list_knowledge_relations(knowledge_object_id)

    # -------------------------------------------------------------- internal
    def _require_knowledge_object(self, knowledge_object_id: int) -> KnowledgeObject:
        knowledge_object = self._database.get_knowledge_object(knowledge_object_id)
        if knowledge_object is None:
            raise KnowledgeObjectNotFoundError(f"知识对象不存在：{knowledge_object_id}")
        return knowledge_object

    def _validate_source_links(
        self,
        source_links: Sequence[tuple[KnowledgeObjectSourceType | str, int, str]],
    ) -> list[tuple[KnowledgeObjectSourceType, int, str]]:
        normalized: list[tuple[KnowledgeObjectSourceType, int, str]] = []
        seen: set[tuple[KnowledgeObjectSourceType, int]] = set()
        for raw_link in source_links:
            if not isinstance(raw_link, (tuple, list)) or len(raw_link) != 3:
                raise KnowledgeObjectValidationError(
                    "来源链接必须是 (来源类型, 来源 ID, 说明) 三元组。"
                )
            source_type = KnowledgeObjectSourceType(raw_link[0])
            source_id = int(raw_link[1])
            source_note = str(raw_link[2])
            if source_id <= 0:
                raise KnowledgeObjectValidationError("来源 ID 必须大于 0。")
            if len(source_note) > 500:
                raise KnowledgeObjectValidationError("来源说明不能超过 500 个字符。")
            key = (source_type, source_id)
            if key in seen:
                raise KnowledgeObjectValidationError(
                    f"来源重复：{source_type.label} {source_id}"
                )
            seen.add(key)
            normalized.append((source_type, source_id, source_note.strip()))
        return normalized

    def _source_target_exists(
        self, source_type: KnowledgeObjectSourceType, source_id: int
    ) -> bool:
        table = {
            KnowledgeObjectSourceType.DOCUMENT: "documents",
            KnowledgeObjectSourceType.PAGE: "pages",
            KnowledgeObjectSourceType.NOTE: "notes",
            KnowledgeObjectSourceType.EVIDENCE: "evidence_items",
        }[source_type]
        with self._database._connection() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (source_id,)
            ).fetchone()
        return row is not None

    def _log_change(
        self, knowledge_object: KnowledgeObject, action: str, detail: str
    ) -> None:
        try:
            self._database.create_knowledge_memory_entry(
                kind=KnowledgeMemoryEntryKind.KNOWLEDGE_CHANGE,
                title=f"知识{action}：{knowledge_object.title}",
                content=detail,
                knowledge_object_id=knowledge_object.id,
            )
        except Exception:
            # 自动日志失败不得阻断主操作，但必须可见。
            LOGGER.exception(
                "写入知识变更日志失败：ko_id=%s action=%s",
                knowledge_object.id,
                action,
            )

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= LIST_LIMIT_MAX
        ):
            raise KnowledgeObjectValidationError(
                f"limit 必须是 1～{LIST_LIMIT_MAX} 的整数"
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise KnowledgeObjectValidationError("offset 必须是非负整数")


__all__ = [
    "KnowledgeObjectError",
    "KnowledgeObjectNotFoundError",
    "KnowledgeObjectService",
    "KnowledgeObjectValidationError",
    "KnowledgeSourceLinkError",
]
