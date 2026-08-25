"""Knowledge memory service (v0.5.2 Phase 2B).

Thin domain service around the schema v10 ``knowledge_memory_entries`` table,
which now holds only user-authored personal memory
(problem solving / experience / decision). System change records live in the
separate ``knowledge_object_revisions`` table and are never written here.

Every entry may link to at most one knowledge object, one document and one
page. Links are validated before write so failures carry clear Chinese
messages instead of raw foreign-key errors.
"""

from __future__ import annotations

from src.database import Database, DatabaseError
from src.models import (
    KnowledgeMemoryEntry,
    KnowledgeMemoryEntryKind,
    KnowledgeMemoryStatus,
)

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100


class KnowledgeMemoryError(DatabaseError):
    """Base class for knowledge-memory service failures."""


class KnowledgeMemoryEntryNotFoundError(KnowledgeMemoryError):
    """The requested memory entry does not exist."""


class KnowledgeMemoryValidationError(KnowledgeMemoryError):
    """Caller-supplied memory data failed validation."""


class KnowledgeMemoryService:
    """Domain service for durable personal memory entries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, entry_id: int) -> KnowledgeMemoryEntry | None:
        """Return one memory entry by primary key, or ``None``."""

        return self._database.get_knowledge_memory_entry(entry_id)

    def list(
        self,
        *,
        kind: KnowledgeMemoryEntryKind | str | None = None,
        status: KnowledgeMemoryStatus | str | None = None,
        knowledge_object_id: int | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> list[KnowledgeMemoryEntry]:
        """List memory entries with stable filters, newest update first."""

        self._validate_pagination(limit, offset)
        return self._database.list_knowledge_memory_entries(
            kind=kind,
            status=status,
            knowledge_object_id=knowledge_object_id,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        *,
        kind: KnowledgeMemoryEntryKind | str | None = None,
        status: KnowledgeMemoryStatus | str | None = None,
        knowledge_object_id: int | None = None,
    ) -> int:
        """Count memory entries with exactly the same filters as ``list``."""

        return self._database.count_knowledge_memory_entries(
            kind=kind, status=status, knowledge_object_id=knowledge_object_id
        )

    def create_entry(
        self,
        *,
        kind: KnowledgeMemoryEntryKind | str,
        title: str,
        content: str = "",
        root_cause: str = "",
        lesson: str = "",
        knowledge_object_id: int | None = None,
        document_id: int | None = None,
        page_id: int | None = None,
        status: KnowledgeMemoryStatus | str = KnowledgeMemoryStatus.ACTIVE,
    ) -> KnowledgeMemoryEntry:
        """Create one user-authored memory entry with validated links.

        ``knowledge_change`` is not a valid personal-memory kind and is
        rejected here with a clear message (the database CHECK is the second
        line of defence).
        """

        try:
            normalized_kind = KnowledgeMemoryEntryKind(kind)
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(
                "记忆类型必须是：问题解决、经验或决策。"
            ) from exc
        if normalized_kind is None:  # pragma: no cover - defensive
            raise KnowledgeMemoryValidationError("记忆类型不能为空。")
        self._validate_links(
            knowledge_object_id=knowledge_object_id,
            document_id=document_id,
            page_id=page_id,
        )
        try:
            return self._database.create_knowledge_memory_entry(
                kind=normalized_kind,
                title=title,
                content=content,
                root_cause=root_cause,
                lesson=lesson,
                knowledge_object_id=knowledge_object_id,
                document_id=document_id,
                page_id=page_id,
                status=status,
            )
        except (ValueError, DatabaseError) as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def update_entry(
        self,
        entry_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        root_cause: str | None = None,
        lesson: str | None = None,
    ) -> KnowledgeMemoryEntry:
        """Update one memory entry's text fields in a single transaction."""

        if self._database.get_knowledge_memory_entry(entry_id) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        try:
            return self._database.update_knowledge_memory_entry(
                entry_id,
                title=title,
                content=content,
                root_cause=root_cause,
                lesson=lesson,
            )
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def set_status(
        self,
        entry_id: int,
        *,
        status: KnowledgeMemoryStatus | str,
    ) -> KnowledgeMemoryEntry:
        """Archive or reactivate one memory entry."""

        if self._database.get_knowledge_memory_entry(entry_id) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        try:
            return self._database.update_knowledge_memory_status(
                entry_id, status=status
            )
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def delete_entry(self, entry_id: int) -> None:
        """Delete one memory entry; linked source material is never touched."""

        if self._database.get_knowledge_memory_entry(entry_id) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        self._database.delete_knowledge_memory_entry(entry_id)

    # -------------------------------------------------------------- internal
    def _validate_links(
        self,
        *,
        knowledge_object_id: int | None,
        document_id: int | None,
        page_id: int | None,
    ) -> None:
        if knowledge_object_id is not None and (
            self._database.get_knowledge_object(knowledge_object_id) is None
        ):
            raise KnowledgeMemoryValidationError(
                f"关联的知识对象不存在：{knowledge_object_id}"
            )
        if document_id is not None and self._database.get_document(document_id) is None:
            raise KnowledgeMemoryValidationError(f"关联的文档不存在：{document_id}")
        if page_id is not None and self._database.get_page(page_id) is None:
            raise KnowledgeMemoryValidationError(f"关联的页面不存在：{page_id}")

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= LIST_LIMIT_MAX
        ):
            raise KnowledgeMemoryValidationError(
                f"limit 必须是 1～{LIST_LIMIT_MAX} 的整数"
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise KnowledgeMemoryValidationError("offset 必须是非负整数")


__all__ = [
    "KnowledgeMemoryEntryNotFoundError",
    "KnowledgeMemoryError",
    "KnowledgeMemoryService",
    "KnowledgeMemoryValidationError",
]
