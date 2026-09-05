"""Knowledge memory service (v0.5.2 Phase 2B, v0.7 Phase 1 identity split).

Thin domain service around the schema v13 ``knowledge_memory_entries`` table,
which holds only user-authored personal memory. Since v13 the two record
semantics are distinct kinds and must never be conflated:

- ``kind='raw_qa'`` — a verbatim question + agent answer the user explicitly
  saved. It only ever means "the user chose to keep this Q&A copy"; it is not
  user experience, not a confirmed fact and not an agent conclusion the user
  endorsed.
- ``kind='experience'`` (and ``problem_solving`` / ``decision``) — authored or
  structured personal knowledge. ``creation_origin`` records how the entry
  came into being (``human_saved`` / ``agent_assisted``; legacy rows whose
  origin cannot be verified stay ``None`` and are never guessed).

Raw Q&A saves are exact-duplicate protected by a canonical content
fingerprint, keep a full citation snapshot of every verified page citation,
and are never silently truncated: oversized content is refused with a clear
message instead. Deletes are soft tombstones (``status='deleted'``) with an
explicit permanent purge behind them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.database import Database, DatabaseError
from src.models import (
    PAGE_STABLE_TYPE,
    KnowledgeMemoryEntry,
    KnowledgeMemoryEntryKind,
    KnowledgeMemoryStatus,
    MemoryCitation,
    MemoryCitationSnapshotError,
    build_stable_id,
    serialize_memory_citations,
)

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100
CONTENT_MAX_LENGTH = 20000

_RAW_QA_CANONICAL_PREFIX = "问题："
_RAW_QA_ANSWER_MARKER = "Agent 回答："


class KnowledgeMemoryError(DatabaseError):
    """Base class for knowledge-memory service failures."""


class KnowledgeMemoryEntryNotFoundError(KnowledgeMemoryError):
    """The requested memory entry does not exist."""


class KnowledgeMemoryValidationError(KnowledgeMemoryError):
    """Caller-supplied memory data failed validation."""


class KnowledgeMemoryDuplicateError(KnowledgeMemoryError):
    """An identical active raw Q&A already exists (exact fingerprint match)."""


@dataclass(frozen=True, slots=True)
class RawQaSaveResult:
    """Outcome of one raw Q&A save attempt.

    ``entry`` is the newly created record; it is ``None`` when the exact same
    Q&A is already saved (``duplicate_of`` points at the existing active
    copy). ``skipped_citations`` counts stable references that could not be
    resolved to a page in this knowledge base.
    """

    entry: KnowledgeMemoryEntry | None
    duplicate_of: KnowledgeMemoryEntry | None = None
    skipped_citations: int = 0


class KnowledgeMemoryService:
    """Domain service for durable personal memory entries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(
        self, entry_id: int, *, include_deleted: bool = False
    ) -> KnowledgeMemoryEntry | None:
        """Return one memory entry by primary key, or ``None``.

        Tombstoned entries are invisible by default; only the restore flow
        asks for them explicitly.
        """

        entry = self._database.get_knowledge_memory_entry(entry_id)
        if entry is None:
            return None
        if entry.status is KnowledgeMemoryStatus.DELETED and not include_deleted:
            return None
        return entry

    def list(
        self,
        *,
        kind: KnowledgeMemoryEntryKind | str | None = None,
        status: KnowledgeMemoryStatus | str | None = None,
        knowledge_object_id: int | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> list[KnowledgeMemoryEntry]:
        """List memory entries with stable filters, newest update first.

        Tombstoned entries are excluded unless explicitly requested by
        status; the database layer enforces the same default.
        """

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
        outcome: str = "",
        context_conditions: str = "",
        creation_origin: str | None = None,
        citation_snapshot: str = "",
        content_fingerprint: str | None = None,
        source_entry_id: int | None = None,
        source_title: str | None = None,
        root_cause_confirmed: bool = False,
    ) -> KnowledgeMemoryEntry:
        """Create one authored memory entry with validated links.

        ``kind='raw_qa'`` is rejected here on purpose: raw saved Q&A has its
        own dedicated save path (``create_raw_qa_entry``) that enforces the
        duplicate advisory and the full citation snapshot. Generic creation
        stays for authored kinds.
        """

        try:
            normalized_kind = KnowledgeMemoryEntryKind(kind)
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(
                "记忆类型必须是：问题解决、经验、决策或保存的问答。"
            ) from exc
        if normalized_kind is KnowledgeMemoryEntryKind.RAW_QA:
            raise KnowledgeMemoryValidationError(
                "保存的问答必须通过 create_raw_qa_entry 保存。"
            )
        if normalized_kind is None:  # pragma: no cover - defensive
            raise KnowledgeMemoryValidationError("记忆类型不能为空。")
        self._validate_links(
            knowledge_object_id=knowledge_object_id,
            document_id=document_id,
            page_id=page_id,
        )
        self._validate_origin_for_kind(normalized_kind, creation_origin)
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
                outcome=outcome,
                context_conditions=context_conditions,
                creation_origin=creation_origin,
                citation_snapshot=citation_snapshot,
                content_fingerprint=content_fingerprint,
                source_entry_id=source_entry_id,
                source_title=source_title,
                root_cause_confirmed=root_cause_confirmed,
            )
        except (ValueError, DatabaseError) as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def promote_raw_qa_to_experience(
        self,
        raw_qa_id: int,
        *,
        title: str,
        content: str,
        root_cause: str = "",
        lesson: str = "",
        outcome: str = "",
        context_conditions: str = "",
        root_cause_confirmed: bool = False,
    ) -> KnowledgeMemoryEntry:
        """Turn one user-confirmed raw Q&A into a structured personal experience.

        Boundary semantics (v0.7.0 Experience Capture):

        - the source must be an *active* ``raw_qa`` entry; the raw copy itself
          is never modified — promotion only adds a new structured record;
        - ``citation_snapshot`` is copied verbatim from the source, so the
          experience keeps the frozen citation history of the original save;
        - ``creation_origin='agent_assisted'`` records that an AI
          transformation produced the first draft; it is independent of
          ``root_cause_confirmed``, which is only set through the explicit
          user confirmation gesture and never flips automatically;
        - ``source_entry_id`` / ``source_title`` keep the traceable link back
          to the raw Q&A (Experience → Raw Q&A → citations → original page);
        - no silent truncation: oversized fields are refused with clear
          messages, exactly like the raw-QA save path.
        """

        source = self.get(raw_qa_id, include_deleted=True)
        if source is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{raw_qa_id}")
        if source.kind is not KnowledgeMemoryEntryKind.RAW_QA:
            raise KnowledgeMemoryValidationError(
                "只有保存的问答才能整理成经验。"
            )
        if source.status is not KnowledgeMemoryStatus.ACTIVE:
            raise KnowledgeMemoryValidationError(
                "这条内容已被删除或归档，不能整理成经验。"
            )
        normalized_title = title.strip()
        if not normalized_title:
            raise KnowledgeMemoryValidationError("经验标题不能为空。")
        normalized_content = content.strip()
        if not normalized_content:
            raise KnowledgeMemoryValidationError("经验内容不能为空。")
        try:
            return self._database.create_knowledge_memory_entry(
                kind=KnowledgeMemoryEntryKind.EXPERIENCE,
                title=normalized_title,
                content=normalized_content,
                root_cause=root_cause.strip(),
                lesson=lesson.strip(),
                knowledge_object_id=source.knowledge_object_id,
                document_id=source.document_id,
                page_id=source.page_id,
                status=KnowledgeMemoryStatus.ACTIVE,
                outcome=outcome.strip(),
                context_conditions=context_conditions.strip(),
                creation_origin="agent_assisted",
                citation_snapshot=source.citation_snapshot,
                source_entry_id=source.id,
                source_title=source.title,
                root_cause_confirmed=root_cause_confirmed,
            )
        except (ValueError, DatabaseError) as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def create_raw_qa_entry(
        self,
        *,
        question: str,
        answer: str,
        cited_page_ids: tuple[int, ...] = (),
        title: str | None = None,
    ) -> RawQaSaveResult:
        """Save one verbatim Q&A copy as ``kind='raw_qa'``.

        Semantics: this records only that the user chose to keep this exact
        question and agent answer. It never means the user experienced the
        content, confirmed it, or endorses the agent's conclusions.

        - No silent truncation: content that exceeds the storage limit is
          refused with the exact sizes, never clipped.
        - Exact-duplicate advisory: an identical active raw Q&A (same
          canonical fingerprint) blocks the save; the existing copy stays
          untouched and no second row is created.
        - Full citation snapshot: every resolvable page citation is frozen
          (document identity, title, sha256, page number, stable id), so the
          save keeps working after the source material is deleted.
        """

        normalized_question = question.strip()
        normalized_answer = answer.strip()
        if not normalized_question:
            raise KnowledgeMemoryValidationError("问题不能为空，无法保存。")
        if not normalized_answer:
            raise KnowledgeMemoryValidationError("Agent 回答为空，没有可保存的内容。")
        content = (
            f"问题：{normalized_question}\n\nAgent 回答：\n{normalized_answer}"
        )
        if len(content) > CONTENT_MAX_LENGTH:
            raise KnowledgeMemoryValidationError(
                "这次问答内容过长（约 "
                f"{len(content)} 字，上限 {CONTENT_MAX_LENGTH} 字），"
                "未保存。你仍可以在原始资料中查看完整回答。"
            )
        fingerprint = _raw_qa_fingerprint(content)
        duplicate = self._database.find_active_raw_qa_by_fingerprint(fingerprint)
        if duplicate is not None:
            return RawQaSaveResult(entry=None, duplicate_of=duplicate)

        citations, skipped = self._resolve_citations(cited_page_ids)
        snapshot = _serialize_snapshot_or_empty(citations)
        if citations:
            first = citations[0]
            document_id = first.document_id
            page_id = first.page_id
        else:
            document_id = None
            page_id = None
        if title is not None and title.strip():
            entry_title = title.strip()[:200]
        else:
            entry_title = self._synthesize_raw_qa_title(
                normalized_question, citations
            )
        try:
            entry = self._database.create_knowledge_memory_entry(
                kind=KnowledgeMemoryEntryKind.RAW_QA,
                title=entry_title,
                content=content,
                document_id=document_id,
                page_id=page_id,
                creation_origin="human_saved",
                citation_snapshot=snapshot,
                content_fingerprint=fingerprint,
            )
        except (ValueError, DatabaseError) as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc
        return RawQaSaveResult(entry=entry, skipped_citations=skipped)

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

        if self.get(entry_id) is None:
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
        """Archive, reactivate or tombstone one memory entry."""

        if self.get(entry_id, include_deleted=True) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        try:
            normalized = KnowledgeMemoryStatus(status)
        except ValueError as exc:
            raise KnowledgeMemoryValidationError("记忆状态必须是：现行、已归档或已删除。") from exc
        try:
            return self._database.update_knowledge_memory_status(
                entry_id, status=normalized
            )
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def delete_entry(self, entry_id: int) -> KnowledgeMemoryEntry:
        """Soft-delete one memory entry (v13 tombstone).

        The entry disappears from normal lists, search and agent reads but
        stays restorable. Linked source material is never touched.
        """

        if self.get(entry_id) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        try:
            return self._database.update_knowledge_memory_status(
                entry_id, status=KnowledgeMemoryStatus.DELETED
            )
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def restore_entry(self, entry_id: int) -> KnowledgeMemoryEntry:
        """Restore one tombstoned memory entry back to ``active``."""

        if self.get(entry_id, include_deleted=True) is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        entry = self._database.get_knowledge_memory_entry(entry_id)
        if entry is None or entry.status is not KnowledgeMemoryStatus.DELETED:
            raise KnowledgeMemoryValidationError("只有已删除的内容才能恢复。")
        try:
            return self._database.update_knowledge_memory_status(
                entry_id, status=KnowledgeMemoryStatus.ACTIVE
            )
        except ValueError as exc:
            raise KnowledgeMemoryValidationError(str(exc)) from exc

    def purge_entry(self, entry_id: int) -> None:
        """Permanently delete one memory entry (explicit, user-confirmed).

        Only tombstoned entries can be purged: the permanent step always
        follows an explicit delete, so no single action destroys content.
        Structured experiences that referenced this entry keep their own
        copied citation snapshot.
        """

        entry = self._database.get_knowledge_memory_entry(entry_id)
        if entry is None:
            raise KnowledgeMemoryEntryNotFoundError(f"记忆条目不存在：{entry_id}")
        if entry.status is not KnowledgeMemoryStatus.DELETED:
            raise KnowledgeMemoryValidationError(
                "请先删除这条内容，再执行永久删除。"
            )
        self._database.delete_knowledge_memory_entry(entry_id)

    def list_deleted(self, *, limit: int = LIST_LIMIT_DEFAULT) -> list[KnowledgeMemoryEntry]:
        """Return tombstoned entries for the restore flow, newest first."""

        self._validate_pagination(limit, 0)
        return self._database.list_knowledge_memory_entries(
            status=KnowledgeMemoryStatus.DELETED, limit=limit
        )

    # -------------------------------------------------------------- internal
    def _resolve_citations(
        self, cited_page_ids: tuple[int, ...]
    ) -> tuple[tuple[MemoryCitation, ...], int]:
        """Resolve page ids into frozen citation snapshots.

        Unresolvable ids (page already deleted mid-session, foreign stable
        id) are skipped and counted so the caller can stay honest about what
        was actually captured.
        """

        citations: list[MemoryCitation] = []
        skipped = 0
        seen_pages: set[int] = set()
        for page_id in cited_page_ids:
            if isinstance(page_id, bool) or not isinstance(page_id, int):
                skipped += 1
                continue
            if page_id in seen_pages:
                continue
            seen_pages.add(page_id)
            page = self._database.get_page(page_id)
            if page is None:
                skipped += 1
                continue
            document = self._database.get_document(page.document_id)
            if document is None:
                citations.append(
                    MemoryCitation(
                        document_id=None,
                        document_title="",
                        document_sha256=None,
                        page_id=page.id,
                        page_number=page.page_number,
                        stable_id=build_stable_id(
                            self._database.get_knowledge_base_uuid(),
                            PAGE_STABLE_TYPE,
                            page.id,
                        ),
                    )
                )
                skipped += 1
                continue
            citations.append(
                MemoryCitation(
                    document_id=document.id,
                    document_title=document.title,
                    document_sha256=document.sha256,
                    page_id=page.id,
                    page_number=page.page_number,
                    stable_id=build_stable_id(
                        self._database.get_knowledge_base_uuid(),
                        PAGE_STABLE_TYPE,
                        page.id,
                    ),
                )
            )
        return tuple(citations), skipped

    def _synthesize_raw_qa_title(
        self, question: str, citations: tuple[MemoryCitation, ...]
    ) -> str:
        """Mirror the accepted save-button title synthesis rules."""

        document_titles = {
            citation.document_title
            for citation in citations
            if citation.document_title
        }
        if len(document_titles) == 1:
            return f"关于 {next(iter(document_titles))} 的讨论"[:200]
        if len(document_titles) > 1:
            return f"关于 {len(document_titles)} 份资料的讨论"[:200]
        return (question or "Agent 对话")[:200]

    @staticmethod
    def _validate_origin_for_kind(
        kind: KnowledgeMemoryEntryKind, creation_origin: str | None
    ) -> None:
        if kind is KnowledgeMemoryEntryKind.RAW_QA:  # pragma: no cover - defensive
            raise KnowledgeMemoryValidationError(
                "保存的问答必须通过 create_raw_qa_entry 保存。"
            )
        if creation_origin not in (None, "human_saved", "agent_assisted"):
            raise KnowledgeMemoryValidationError(
                "记忆来源标识必须是 human_saved 或 agent_assisted。"
            )

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


def _raw_qa_fingerprint(content: str) -> str:
    """Return the exact-duplicate fingerprint of one raw Q&A copy.

    Canonical form: CRLF/CR normalized to LF and outer whitespace stripped.
    Inner whitespace is kept verbatim — the fingerprint identifies the exact
    saved copy, not paraphrases.
    """

    canonical = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_snapshot_or_empty(
    citations: tuple[MemoryCitation, ...],
) -> str:
    """Serialize the citation snapshot, mapping constraint errors to validation."""

    try:
        return serialize_memory_citations(citations)
    except MemoryCitationSnapshotError as exc:
        raise KnowledgeMemoryValidationError(str(exc)) from exc


__all__ = [
    "CONTENT_MAX_LENGTH",
    "LIST_LIMIT_DEFAULT",
    "LIST_LIMIT_MAX",
    "KnowledgeMemoryDuplicateError",
    "KnowledgeMemoryEntryNotFoundError",
    "KnowledgeMemoryError",
    "KnowledgeMemoryService",
    "KnowledgeMemoryValidationError",
    "RawQaSaveResult",
]
