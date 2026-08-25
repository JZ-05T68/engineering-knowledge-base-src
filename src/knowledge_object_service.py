"""Knowledge object service (v0.5.2 Phase 2B).

Domain service for the schema v10 ``knowledge_objects`` entity: creation with
orthogonal fields (kind / authorship / epistemic_basis / confirmation /
lifecycle), content updates with full before/after revision history, user
confirmation, lifecycle transitions (archive / unarchive / supersede /
reactivate / repoint), source linking and typed relations.

Every user-visible mutation writes one append-only revision row in the same
SQLite transaction as the business change. Revision rows have no update or
delete API. ``revision_number`` is a per-object event sequence; the object's
``current_revision`` always equals the sequence number of the latest
content-bearing revision (created or content_updated), and
``confirmed_revision`` binds a confirmation to that content revision.

The service never touches original PDFs, page images, notes or evidence rows:
source links are read-only references whose target existence is re-checked on
every read. The fingerprint state machine is out of scope here and lands in
Phase 2C; source existence is still computed for display.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from src.database import Database, DatabaseError
from src.models import (
    KnowledgeAuthorship,
    KnowledgeConfirmationStatus,
    KnowledgeEpistemicBasis,
    KnowledgeLifecycle,
    KnowledgeObject,
    KnowledgeObjectKind,
    KnowledgeObjectSource,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeObjectView,
    KnowledgeRelation,
    KnowledgeRelationType,
    KnowledgeRevisionEventType,
    KnowledgeSourceAggregate,
    KnowledgeSourceStatus,
    NoteImportance,
    aggregate_source_state,
)
from src.source_fingerprint import (
    FINGERPRINT_VERSION,
    compute_source_fingerprint,
)

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
        lifecycle: KnowledgeLifecycle | str | None = None,
        confirmation_status: KnowledgeConfirmationStatus | str | None = None,
        epistemic_basis: KnowledgeEpistemicBasis | str | None = None,
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
            lifecycle=lifecycle,
            confirmation_status=confirmation_status,
            epistemic_basis=epistemic_basis,
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
        lifecycle: KnowledgeLifecycle | str | None = None,
        confirmation_status: KnowledgeConfirmationStatus | str | None = None,
        epistemic_basis: KnowledgeEpistemicBasis | str | None = None,
        query: str = "",
    ) -> int:
        """Count knowledge objects with exactly the same filters as ``list``."""

        return self._database.count_knowledge_objects(
            kind=kind,
            importance=importance,
            lifecycle=lifecycle,
            confirmation_status=confirmation_status,
            epistemic_basis=epistemic_basis,
            query=query,
        )

    def revisions(self, knowledge_object_id: int) -> list:
        """Return every revision of one object in revision order."""

        self._require_knowledge_object(knowledge_object_id)
        return self._database.list_knowledge_revisions(knowledge_object_id)

    # ------------------------------------------------------------- mutation
    def create(
        self,
        *,
        kind: KnowledgeObjectKind | str,
        title: str,
        content: str,
        importance: NoteImportance | str = NoteImportance.NORMAL,
        epistemic_basis: KnowledgeEpistemicBasis | str,
        source_links: Sequence[
            tuple[KnowledgeObjectSourceType | str, int, str]
        ] = (),
    ) -> KnowledgeObjectView:
        """Create one knowledge object and optionally link its sources.

        Authorship is fixed to ``user`` in v0.5.2; lifecycle starts as
        ``active`` and confirmation as ``unconfirmed``. ``epistemic_basis`` is
        mandatory and must be a definite legal basis: ``unknown_legacy`` is
        reserved for v9 migration backfill and is rejected here. The object
        row, the ``created`` revision and every source link are written in one
        transaction.
        """

        normalized_basis = KnowledgeEpistemicBasis(epistemic_basis)
        if normalized_basis is KnowledgeEpistemicBasis.UNKNOWN_LEGACY:
            raise KnowledgeObjectValidationError(
                "形成依据不能选择「未知（旧数据）」，请选择明确的依据。"
            )
        normalized_links = self._validate_source_links(source_links)
        with self._database.knowledge_transaction() as connection:
            knowledge_object = self._database.create_knowledge_object(
                kind=kind,
                title=title,
                content=content,
                importance=importance,
                authorship=KnowledgeAuthorship.USER,
                epistemic_basis=normalized_basis,
                lifecycle=KnowledgeLifecycle.ACTIVE,
                confirmation_status=KnowledgeConfirmationStatus.UNCONFIRMED,
                connection=connection,
            )
            self._insert_revision(
                connection,
                knowledge_object,
                KnowledgeRevisionEventType.CREATED,
                revision_number=knowledge_object.current_revision,
                after_title=knowledge_object.title,
                after_content=knowledge_object.content,
                after_lifecycle=knowledge_object.lifecycle.value,
                after_confirmation=knowledge_object.confirmation_status.value,
                detail=f"创建知识对象「{knowledge_object.title}」。",
            )
            for source_type, source_id, source_note in normalized_links:
                fingerprint = self._capture_fingerprint(
                    connection, source_type, source_id
                )
                source = self._database.add_knowledge_object_source(
                    knowledge_object_id=knowledge_object.id,
                    source_type=source_type,
                    source_id=source_id,
                    source_note=source_note,
                    source_fingerprint=fingerprint,
                    fingerprint_version=FINGERPRINT_VERSION,
                    connection=connection,
                )
                self._insert_revision(
                    connection,
                    knowledge_object,
                    KnowledgeRevisionEventType.SOURCE_LINKED,
                    revision_number=self._next_revision_number(
                        connection, knowledge_object.id
                    ),
                    source_ref=f"{source.source_type.value}:{source.source_id}",
                    detail=f"关联{source.source_type.label} {source.source_id}。",
                )
        return self.get_view(knowledge_object.id)

    def update_content(
        self,
        knowledge_object_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        importance: NoteImportance | str | None = None,
    ) -> KnowledgeObjectView:
        """Update title/content/importance and record full before/after history.

        ``current_revision`` advances to the next revision event sequence
        number; a previous confirmation is kept as-is and naturally becomes
        stale when ``confirmed_revision`` is older than ``current_revision``.
        """

        current = self._require_knowledge_object(knowledge_object_id)
        if title is None and content is None and importance is None:
            return self.get_view(knowledge_object_id)
        normalized_title = title.strip() if title is not None else None
        normalized_content = content.strip() if content is not None else None
        changed = (
            (normalized_title is not None and normalized_title != current.title)
            or (normalized_content is not None and normalized_content != current.content)
            or (importance is not None and NoteImportance(importance) is not current.importance)
        )
        if not changed:
            return self.get_view(knowledge_object_id)
        with self._database.knowledge_transaction() as connection:
            new_revision = self._next_revision_number(connection, knowledge_object_id)
            updated = self._database.update_knowledge_object_content(
                knowledge_object_id,
                new_revision=new_revision,
                title=title,
                content=content,
                importance=importance,
                connection=connection,
            )
            importance_detail = (
                f"重要程度调整为「{updated.importance.label}」。"
                if importance is not None and updated.importance is not current.importance
                else ""
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.CONTENT_UPDATED,
                revision_number=new_revision,
                before_title=current.title,
                after_title=updated.title,
                before_content=current.content,
                after_content=updated.content,
                detail=f"更新知识对象「{updated.title}」。{importance_detail}",
            )
        return self.get_view(knowledge_object_id)

    def update_epistemic_basis(
        self,
        knowledge_object_id: int,
        *,
        epistemic_basis: KnowledgeEpistemicBasis | str,
    ) -> KnowledgeObjectView:
        """Revise the epistemic basis to a definite legal value.

        ``unknown_legacy`` is reserved for v9 migration backfill and can never
        be re-selected here. A basis change is a content-bearing change: it
        advances ``current_revision`` and therefore makes a prior confirmation
        stale instead of silently reinterpreting what the user confirmed.
        """

        normalized_basis = KnowledgeEpistemicBasis(epistemic_basis)
        if normalized_basis is KnowledgeEpistemicBasis.UNKNOWN_LEGACY:
            raise KnowledgeObjectValidationError(
                "形成依据不能改为「未知（旧数据）」，请选择明确的依据。"
            )
        current = self._require_knowledge_object(knowledge_object_id)
        if normalized_basis is current.epistemic_basis:
            return self.get_view(knowledge_object_id)
        with self._database.knowledge_transaction() as connection:
            new_revision = self._next_revision_number(connection, knowledge_object_id)
            updated = self._database.update_knowledge_object_epistemic_basis(
                knowledge_object_id,
                epistemic_basis=normalized_basis,
                new_revision=new_revision,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.CONTENT_UPDATED,
                revision_number=new_revision,
                before_title=current.title,
                after_title=updated.title,
                before_content=current.content,
                after_content=updated.content,
                before_lifecycle=current.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                before_confirmation=current.confirmation_status.value,
                after_confirmation=updated.confirmation_status.value,
                detail=(
                    f"形成依据由「{current.epistemic_basis.label}」"
                    f"修订为「{normalized_basis.label}」。"
                ),
            )
        return self.get_view(knowledge_object_id)

    def confirm(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Confirm the current content revision; idempotent when already current."""

        current = self._require_knowledge_object(knowledge_object_id)
        if current.confirmation_is_current:
            return self.get_view(knowledge_object_id)
        timestamp = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_confirmation(
                knowledge_object_id,
                confirmation_status=KnowledgeConfirmationStatus.CONFIRMED,
                confirmed_at=timestamp,
                confirmed_revision=current.current_revision,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.CONFIRMATION_CHANGED,
                revision_number=self._next_revision_number(
                    connection, knowledge_object_id
                ),
                before_confirmation=current.confirmation_status.value,
                after_confirmation=updated.confirmation_status.value,
                detail=f"确认知识对象「{updated.title}」第 {updated.confirmed_revision} 版。",
            )
        return self.get_view(knowledge_object_id)

    def unconfirm(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Remove the user confirmation; idempotent when already unconfirmed."""

        current = self._require_knowledge_object(knowledge_object_id)
        if current.confirmation_status is KnowledgeConfirmationStatus.UNCONFIRMED:
            return self.get_view(knowledge_object_id)
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_confirmation(
                knowledge_object_id,
                confirmation_status=KnowledgeConfirmationStatus.UNCONFIRMED,
                confirmed_at=None,
                confirmed_revision=None,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.CONFIRMATION_CHANGED,
                revision_number=self._next_revision_number(
                    connection, knowledge_object_id
                ),
                before_confirmation=current.confirmation_status.value,
                after_confirmation=updated.confirmation_status.value,
                detail=f"取消确认知识对象「{updated.title}」。",
            )
        return self.get_view(knowledge_object_id)

    # ------------------------------------------------------------- lifecycle
    def archive(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Archive an active object; archived objects carry no successor."""

        current = self._require_knowledge_object(knowledge_object_id)
        if current.lifecycle is KnowledgeLifecycle.ARCHIVED:
            return self.get_view(knowledge_object_id)
        if current.lifecycle is not KnowledgeLifecycle.ACTIVE:
            raise KnowledgeObjectValidationError(
                "只有“现行”状态的知识对象可以直接归档；已替代对象请先重新启用。"
            )
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_lifecycle(
                knowledge_object_id,
                lifecycle=KnowledgeLifecycle.ARCHIVED,
                superseded_by_ko_id=None,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.LIFECYCLE_CHANGED,
                revision_number=self._next_revision_number(
                    connection, knowledge_object_id
                ),
                before_lifecycle=current.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                detail=f"归档知识对象「{updated.title}」。",
            )
        return self.get_view(knowledge_object_id)

    def unarchive(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Return an archived object to active."""

        current = self._require_knowledge_object(knowledge_object_id)
        if current.lifecycle is KnowledgeLifecycle.ACTIVE:
            return self.get_view(knowledge_object_id)
        if current.lifecycle is not KnowledgeLifecycle.ARCHIVED:
            raise KnowledgeObjectValidationError("只有“已归档”对象可以重新启用。")
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_lifecycle(
                knowledge_object_id,
                lifecycle=KnowledgeLifecycle.ACTIVE,
                superseded_by_ko_id=None,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.LIFECYCLE_CHANGED,
                revision_number=self._next_revision_number(
                    connection, knowledge_object_id
                ),
                before_lifecycle=current.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                detail=f"重新启用知识对象「{updated.title}」。",
            )
        return self.get_view(knowledge_object_id)

    def supersede(self, old_id: int, new_id: int) -> KnowledgeObjectView:
        """Mark ``old_id`` as superseded by the active object ``new_id``.

        Rejects self-supersession, missing objects, non-active successors and
        transitive cycles. The old object keeps its sources, relations and
        confirmation history untouched.
        """

        old = self._require_knowledge_object(old_id)
        new = self._require_knowledge_object(new_id)
        if old_id == new_id:
            raise KnowledgeObjectValidationError("知识对象不能替代自身。")
        if new.lifecycle is not KnowledgeLifecycle.ACTIVE:
            raise KnowledgeObjectValidationError("替代后继必须是“现行”状态的知识对象。")
        if self._supersession_chain_reaches(new_id, old_id):
            raise KnowledgeObjectValidationError("替代关系会形成循环，已拒绝。")
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_lifecycle(
                old_id,
                lifecycle=KnowledgeLifecycle.SUPERSEDED,
                superseded_by_ko_id=new_id,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.SUPERSESSION_CHANGED,
                revision_number=self._next_revision_number(connection, old_id),
                before_lifecycle=old.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                superseded_by_before=old.superseded_by_ko_id,
                superseded_by_after=updated.superseded_by_ko_id,
                detail=f"「{updated.title}」被知识对象 {new_id}「{new.title}」替代。",
            )
        return self.get_view(old_id)

    def reactivate(self, knowledge_object_id: int) -> KnowledgeObjectView:
        """Return a superseded object to active and clear its successor."""

        current = self._require_knowledge_object(knowledge_object_id)
        if current.lifecycle is KnowledgeLifecycle.ACTIVE:
            return self.get_view(knowledge_object_id)
        if current.lifecycle is not KnowledgeLifecycle.SUPERSEDED:
            raise KnowledgeObjectValidationError("只有“已替代”对象可以重新启用。")
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_lifecycle(
                knowledge_object_id,
                lifecycle=KnowledgeLifecycle.ACTIVE,
                superseded_by_ko_id=None,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.SUPERSESSION_CHANGED,
                revision_number=self._next_revision_number(
                    connection, knowledge_object_id
                ),
                before_lifecycle=current.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                superseded_by_before=current.superseded_by_ko_id,
                superseded_by_after=None,
                detail=f"重新启用「{updated.title}」并解除替代关系。",
            )
        return self.get_view(knowledge_object_id)

    def repoint_supersession(self, old_id: int, new_id: int) -> KnowledgeObjectView:
        """Point a superseded object at a different active successor."""

        old = self._require_knowledge_object(old_id)
        new = self._require_knowledge_object(new_id)
        if old.lifecycle is not KnowledgeLifecycle.SUPERSEDED:
            raise KnowledgeObjectValidationError("只有“已替代”对象可以重新指定后继。")
        if old_id == new_id:
            raise KnowledgeObjectValidationError("知识对象不能替代自身。")
        if new.lifecycle is not KnowledgeLifecycle.ACTIVE:
            raise KnowledgeObjectValidationError("替代后继必须是“现行”状态的知识对象。")
        if self._supersession_chain_reaches(new_id, old_id):
            raise KnowledgeObjectValidationError("替代关系会形成循环，已拒绝。")
        with self._database.knowledge_transaction() as connection:
            updated = self._database.update_knowledge_object_lifecycle(
                old_id,
                lifecycle=KnowledgeLifecycle.SUPERSEDED,
                superseded_by_ko_id=new_id,
                connection=connection,
            )
            self._insert_revision(
                connection,
                updated,
                KnowledgeRevisionEventType.SUPERSESSION_CHANGED,
                revision_number=self._next_revision_number(connection, old_id),
                before_lifecycle=old.lifecycle.value,
                after_lifecycle=updated.lifecycle.value,
                superseded_by_before=old.superseded_by_ko_id,
                superseded_by_after=new_id,
                detail=f"「{updated.title}」的替代后继改为知识对象 {new_id}「{new.title}」。",
            )
        return self.get_view(old_id)

    def delete(self, knowledge_object_id: int) -> None:
        """Delete one knowledge object; sources and relations cascade away.

        Never touches original PDFs, page images, notes or evidence rows.
        Memory entries survive with ``knowledge_object_id`` set to NULL and
        revision rows are never modified. A successor object still referenced
        by superseded predecessors is refused before any write.
        """

        self._require_knowledge_object(knowledge_object_id)
        inbound = self._database.count_inbound_supersessions(knowledge_object_id)
        if inbound > 0:
            raise KnowledgeObjectValidationError(
                f"该知识对象仍被 {inbound} 个已替代对象引用为后继，"
                "请先重新指定后继、重新启用旧对象或归档旧对象。"
            )
        self._database.delete_knowledge_object(knowledge_object_id)

    # --------------------------------------------------------------- sources
    def source_views(self, knowledge_object_id: int) -> tuple[KnowledgeObjectSourceView, ...]:
        """Return every source link with a freshly recomputed fingerprint status.

        The read path is strictly read-only (ADR-03): the canonical fingerprint
        is recomputed in memory and compared with the stored snapshot; nothing
        is written back.
        """

        self._require_knowledge_object(knowledge_object_id)
        sources = self._database.list_knowledge_object_sources(knowledge_object_id)
        with self._database._connection() as connection:  # noqa: SLF001
            views = tuple(
                KnowledgeObjectSourceView(
                    source=source,
                    status=self._source_status(connection, source),
                )
                for source in sources
            )
        return views

    def source_health(self, knowledge_object_id: int) -> KnowledgeSourceAggregate:
        """Compute the object-level aggregate source state (ADR-03 truth table)."""

        views = self.source_views(knowledge_object_id)
        counts = {
            KnowledgeSourceStatus.VALID: 0,
            KnowledgeSourceStatus.CHANGED: 0,
            KnowledgeSourceStatus.MISSING: 0,
            KnowledgeSourceStatus.UNKNOWN: 0,
        }
        evidence_unconfirmed = 0
        for view in views:
            counts[view.status] += 1
            if (
                view.source.source_type is KnowledgeObjectSourceType.EVIDENCE
                and not self._evidence_source_confirmed(view.source.source_id)
            ):
                evidence_unconfirmed += 1
        return KnowledgeSourceAggregate(
            state=aggregate_source_state(
                counts[KnowledgeSourceStatus.VALID],
                counts[KnowledgeSourceStatus.CHANGED],
                counts[KnowledgeSourceStatus.MISSING],
                counts[KnowledgeSourceStatus.UNKNOWN],
            ),
            valid_count=counts[KnowledgeSourceStatus.VALID],
            changed_count=counts[KnowledgeSourceStatus.CHANGED],
            missing_count=counts[KnowledgeSourceStatus.MISSING],
            unknown_count=counts[KnowledgeSourceStatus.UNKNOWN],
            evidence_unconfirmed_count=evidence_unconfirmed,
        )

    def recapture_source_fingerprint(self, source_id: int) -> KnowledgeObjectSourceView:
        """Recapture the canonical fingerprint of one source link.

        Same-fingerprint recapture is a no-op (no write, no revision). A change
        overwrites the stored snapshot and refreshes ``captured_at``; the
        canonical recipe version stays ``FINGERPRINT_VERSION``. The target must
        exist and be computable; otherwise a clear error is raised.
        """

        source = self._database.get_knowledge_object_source(source_id)
        if source is None:
            raise KnowledgeSourceLinkError(f"知识对象来源不存在：{source_id}")
        with self._database.knowledge_transaction() as connection:
            fingerprint = self._capture_fingerprint(
                connection, source.source_type, source.source_id
            )
            if fingerprint == source.source_fingerprint and (
                source.fingerprint_version == FINGERPRINT_VERSION
            ):
                return self._source_view_from(connection, source)
            updated = self._database.update_knowledge_object_source_fingerprint(
                source_id,
                source_fingerprint=fingerprint,
                fingerprint_version=FINGERPRINT_VERSION,
                connection=connection,
            )
            return self._source_view_from(connection, updated)

    def link_source(
        self,
        knowledge_object_id: int,
        *,
        source_type: KnowledgeObjectSourceType | str,
        source_id: int,
        source_note: str = "",
    ) -> KnowledgeObjectSourceView:
        """Link one source entity to a knowledge object after existence check.

        The canonical fingerprint is captured in the same transaction as the
        link row and its ``source_linked`` revision, so a failed capture or a
        failed revision write rolls everything back together.
        """

        knowledge_object = self._require_knowledge_object(knowledge_object_id)
        normalized_type = KnowledgeObjectSourceType(source_type)
        if not self._source_target_exists(normalized_type, source_id):
            raise KnowledgeSourceLinkError(
                f"来源不存在：{normalized_type.label} {source_id}"
            )
        try:
            with self._database.knowledge_transaction() as connection:
                fingerprint = self._capture_fingerprint(
                    connection, normalized_type, source_id
                )
                source = self._database.add_knowledge_object_source(
                    knowledge_object_id=knowledge_object_id,
                    source_type=normalized_type,
                    source_id=source_id,
                    source_note=source_note,
                    source_fingerprint=fingerprint,
                    fingerprint_version=FINGERPRINT_VERSION,
                    connection=connection,
                )
                self._insert_revision(
                    connection,
                    knowledge_object,
                    KnowledgeRevisionEventType.SOURCE_LINKED,
                    revision_number=self._next_revision_number(
                        connection, knowledge_object_id
                    ),
                    source_ref=f"{source.source_type.value}:{source.source_id}",
                    detail=f"关联{source.source_type.label} {source.source_id}。",
                )
        except DatabaseError as exc:
            raise KnowledgeSourceLinkError(str(exc)) from exc
        return KnowledgeObjectSourceView(
            source=source, status=KnowledgeSourceStatus.VALID
        )

    def unlink_source(self, source_id: int) -> None:
        """Remove one source link; the source material itself is never touched."""

        source = self._database.get_knowledge_object_source(source_id)
        if source is None:
            raise KnowledgeSourceLinkError(f"知识对象来源不存在：{source_id}")
        knowledge_object = self._database.get_knowledge_object(
            source.knowledge_object_id
        )
        try:
            with self._database.knowledge_transaction() as connection:
                self._database.remove_knowledge_object_source(
                    source_id, connection=connection
                )
                if knowledge_object is not None:
                    self._insert_revision(
                        connection,
                        knowledge_object,
                        KnowledgeRevisionEventType.SOURCE_UNLINKED,
                        revision_number=self._next_revision_number(
                            connection, knowledge_object.id
                        ),
                        source_ref=f"{source.source_type.value}:{source.source_id}",
                        detail=f"解除{source.source_type.label} {source.source_id} 来源。",
                    )
        except DatabaseError as exc:
            raise KnowledgeSourceLinkError(str(exc)) from exc

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

        self._require_knowledge_object(source_ko_id)
        self._require_knowledge_object(target_ko_id)
        try:
            return self._database.add_knowledge_relation(
                source_ko_id=source_ko_id,
                target_ko_id=target_ko_id,
                relation_type=relation_type,
                description=description,
            )
        except (DatabaseError, ValueError) as exc:
            raise KnowledgeObjectValidationError(str(exc)) from exc

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

    def _next_revision_number(
        self, connection: sqlite3.Connection, knowledge_object_id: int
    ) -> int:
        return self._database.next_knowledge_revision_number(
            knowledge_object_id, connection=connection
        )

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        knowledge_object: KnowledgeObject,
        event_type: KnowledgeRevisionEventType,
        *,
        revision_number: int,
        before_title: str | None = None,
        after_title: str | None = None,
        before_content: str | None = None,
        after_content: str | None = None,
        before_lifecycle: str | None = None,
        after_lifecycle: str | None = None,
        before_confirmation: str | None = None,
        after_confirmation: str | None = None,
        superseded_by_before: int | None = None,
        superseded_by_after: int | None = None,
        source_ref: str | None = None,
        detail: str = "",
    ) -> None:
        """Append one immutable revision row inside the current transaction."""

        self._database.insert_knowledge_revision(
            knowledge_object_id=knowledge_object.id,
            object_local_id_snapshot=knowledge_object.id,
            object_stable_id_snapshot=self._database.knowledge_object_stable_id(
                knowledge_object.id
            ),
            object_title_snapshot=knowledge_object.title,
            object_kind_snapshot=knowledge_object.kind.value,
            revision_number=revision_number,
            event_type=event_type,
            before_title=before_title,
            after_title=after_title,
            before_content=before_content,
            after_content=after_content,
            before_lifecycle=before_lifecycle,
            after_lifecycle=after_lifecycle,
            before_confirmation=before_confirmation,
            after_confirmation=after_confirmation,
            superseded_by_before=superseded_by_before,
            superseded_by_after=superseded_by_after,
            source_ref=source_ref,
            detail=detail,
            connection=connection,
        )

    def _supersession_chain_reaches(self, start_id: int, target_id: int) -> bool:
        """Return whether following successor pointers from ``start_id`` reaches ``target_id``."""

        seen: set[int] = set()
        current_id: int | None = start_id
        while current_id is not None and current_id not in seen:
            if current_id == target_id:
                return True
            seen.add(current_id)
            current = self._database.get_knowledge_object(current_id)
            if current is None:
                return False
            current_id = current.superseded_by_ko_id
        return False

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

    def _capture_fingerprint(
        self,
        connection: sqlite3.Connection,
        source_type: KnowledgeObjectSourceType,
        source_id: int,
    ) -> str:
        fingerprint = compute_source_fingerprint(connection, source_type, source_id)
        if fingerprint is None:
            if source_type is KnowledgeObjectSourceType.PAGE:
                raise KnowledgeSourceLinkError(
                    f"页面 {source_id} 没有可用文本层，无法建立页面来源；"
                    "请改用 evidence(image_region) 或 note。"
                )
            raise KnowledgeSourceLinkError(
                f"来源不可用或不存在：{source_type.label} {source_id}"
            )
        return fingerprint

    def _source_status(
        self,
        connection: sqlite3.Connection,
        source: KnowledgeObjectSource,
    ) -> KnowledgeSourceStatus:
        if not self._source_target_exists(source.source_type, source.source_id):
            return KnowledgeSourceStatus.MISSING
        if source.source_fingerprint is None:
            return KnowledgeSourceStatus.UNKNOWN
        current = compute_source_fingerprint(
            connection, source.source_type, source.source_id
        )
        if current is None:
            return KnowledgeSourceStatus.MISSING
        if current == source.source_fingerprint:
            return KnowledgeSourceStatus.VALID
        return KnowledgeSourceStatus.CHANGED

    def _source_view_from(
        self,
        connection: sqlite3.Connection,
        source: KnowledgeObjectSource,
    ) -> KnowledgeObjectSourceView:
        return KnowledgeObjectSourceView(
            source=source,
            status=self._source_status(connection, source),
        )

    def _evidence_source_confirmed(self, source_id: int) -> bool:
        with self._database._connection() as connection:
            row = connection.execute(
                "SELECT confirmation_status FROM evidence_items WHERE id = ?",
                (source_id,),
            ).fetchone()
        return row is not None and row["confirmation_status"] == "confirmed"

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
