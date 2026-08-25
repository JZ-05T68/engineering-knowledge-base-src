"""Service tests for the v0.5.2 Phase 2B knowledge-object domain rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database
from src.knowledge_object_service import (
    KnowledgeObjectNotFoundError,
    KnowledgeObjectService,
    KnowledgeObjectValidationError,
    KnowledgeSourceLinkError,
)
from src.models import (
    KnowledgeConfirmationStatus,
    KnowledgeEpistemicBasis,
    KnowledgeLifecycle,
    KnowledgeObjectKind,
    KnowledgeRelationType,
    KnowledgeRevisionEventType,
    KnowledgeSourceStatus,
    NoteImportance,
)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


@pytest.fixture()
def service(database: Database) -> KnowledgeObjectService:
    return KnowledgeObjectService(database)


def _create(service: KnowledgeObjectService, **kwargs: object) -> object:
    """Create via the service with a definite default epistemic basis."""

    kwargs.setdefault(
        "epistemic_basis", KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE
    )
    return service.create(**kwargs)


def _seed_document_and_page(database: Database) -> tuple[int, int]:
    document = database.create_document(
        title="测试手册",
        filename="manual.pdf",
        source_path="data/raw/manual.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path="data/pages/1/page_0001.png",
        extracted_text="页面文本",
    )
    return document.id, page.id


def test_create_object_with_orthogonal_fields_and_created_revision(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service,
        kind=KnowledgeObjectKind.EXPERIENCE,
        title="PID 参数经验",
        content="采样频率必须匹配。",
        importance=NoteImportance.PRIMARY,
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
    )

    knowledge_object = view.knowledge_object
    assert knowledge_object.authorship.value == "user"
    assert knowledge_object.epistemic_basis is KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE
    assert knowledge_object.lifecycle is KnowledgeLifecycle.ACTIVE
    assert knowledge_object.confirmation_status is KnowledgeConfirmationStatus.UNCONFIRMED
    assert knowledge_object.current_revision == 1
    revisions = database.list_knowledge_revisions(knowledge_object.id)
    assert [item.event_type for item in revisions] == [KnowledgeRevisionEventType.CREATED]
    assert revisions[0].after_content == "采样频率必须匹配。"


def test_create_always_writes_user_authorship(service: KnowledgeObjectService) -> None:
    view = _create(service, kind="concept", title="概念", content="内容")
    assert view.knowledge_object.authorship.value == "user"


def test_update_content_records_before_after_and_stale_confirmation(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="concept", title="概念", content="旧内容")
    service.confirm(view.knowledge_object.id)

    updated = service.update_content(view.knowledge_object.id, content="新内容")

    assert updated.knowledge_object.content == "新内容"
    assert updated.knowledge_object.current_revision == 3  # created=1, confirm=2, update=3
    assert updated.knowledge_object.confirmation_status is KnowledgeConfirmationStatus.CONFIRMED
    assert updated.knowledge_object.confirmed_revision == 1
    assert updated.knowledge_object.confirmation_is_stale
    revisions = database.list_knowledge_revisions(view.knowledge_object.id)
    content_revision = [
        item for item in revisions if item.event_type is KnowledgeRevisionEventType.CONTENT_UPDATED
    ][0]
    assert content_revision.before_content == "旧内容"
    assert content_revision.after_content == "新内容"


def test_confirm_and_unconfirm_are_idempotent(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="experience", title="经验", content="内容")
    service.confirm(view.knowledge_object.id)
    confirmed = service.confirm(view.knowledge_object.id)
    assert confirmed.knowledge_object.confirmed_revision == 1
    confirm_events = [
        item
        for item in database.list_knowledge_revisions(view.knowledge_object.id)
        if item.event_type is KnowledgeRevisionEventType.CONFIRMATION_CHANGED
    ]
    assert len(confirm_events) == 1  # 重复确认不增长 revision

    service.unconfirm(view.knowledge_object.id)
    unconfirmed = service.unconfirm(view.knowledge_object.id)
    assert (
        unconfirmed.knowledge_object.confirmation_status
        is KnowledgeConfirmationStatus.UNCONFIRMED
    )
    assert unconfirmed.knowledge_object.confirmed_at is None
    assert unconfirmed.knowledge_object.confirmed_revision is None


def test_reconfirm_after_content_change(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="fact", title="事实", content="内容")
    service.confirm(view.knowledge_object.id)
    service.update_content(view.knowledge_object.id, content="新内容")
    updated = service.confirm(view.knowledge_object.id)

    assert updated.knowledge_object.confirmation_is_current
    assert updated.knowledge_object.confirmed_revision == updated.knowledge_object.current_revision


def test_archive_and_unarchive(service: KnowledgeObjectService) -> None:
    view = _create(service, kind="concept", title="概念", content="内容")
    archived = service.archive(view.knowledge_object.id)
    assert archived.knowledge_object.lifecycle is KnowledgeLifecycle.ARCHIVED
    assert archived.knowledge_object.superseded_by_ko_id is None

    active = service.unarchive(view.knowledge_object.id)
    assert active.knowledge_object.lifecycle is KnowledgeLifecycle.ACTIVE


def test_supersede_reactivate_and_repoint(service: KnowledgeObjectService) -> None:
    old = _create(service, kind="fact", title="旧事实", content="旧内容")
    new = _create(service, kind="fact", title="新事实", content="新内容")

    superseded = service.supersede(old.knowledge_object.id, new.knowledge_object.id)
    assert superseded.knowledge_object.lifecycle is KnowledgeLifecycle.SUPERSEDED
    assert superseded.knowledge_object.superseded_by_ko_id == new.knowledge_object.id

    third = _create(service, kind="fact", title="第三事实", content="第三内容")
    repointed = service.repoint_supersession(old.knowledge_object.id, third.knowledge_object.id)
    assert repointed.knowledge_object.superseded_by_ko_id == third.knowledge_object.id

    reactivated = service.reactivate(old.knowledge_object.id)
    assert reactivated.knowledge_object.lifecycle is KnowledgeLifecycle.ACTIVE
    assert reactivated.knowledge_object.superseded_by_ko_id is None


def test_supersede_rejects_self_and_cycles(service: KnowledgeObjectService) -> None:
    first = _create(service, kind="fact", title="A", content="内容")
    second = _create(service, kind="fact", title="B", content="内容")
    third = _create(service, kind="fact", title="C", content="内容")

    with pytest.raises(KnowledgeObjectValidationError, match="自身"):
        service.supersede(first.knowledge_object.id, first.knowledge_object.id)
    service.supersede(first.knowledge_object.id, second.knowledge_object.id)
    service.supersede(second.knowledge_object.id, third.knowledge_object.id)
    # A → B → C：A 已非 active，不能成为 C 的后继（active 规则先于循环检查拦截）。
    with pytest.raises(KnowledgeObjectValidationError, match="现行"):
        service.supersede(third.knowledge_object.id, first.knowledge_object.id)


def test_supersede_requires_active_successor(service: KnowledgeObjectService) -> None:
    old = _create(service, kind="fact", title="旧", content="内容")
    successor = _create(service, kind="fact", title="后继", content="内容")
    service.archive(successor.knowledge_object.id)
    with pytest.raises(KnowledgeObjectValidationError, match="现行"):
        service.supersede(old.knowledge_object.id, successor.knowledge_object.id)


def test_archiving_superseded_object_is_rejected(service: KnowledgeObjectService) -> None:
    old = _create(service, kind="fact", title="旧", content="内容")
    new = _create(service, kind="fact", title="新", content="内容")
    service.supersede(old.knowledge_object.id, new.knowledge_object.id)
    with pytest.raises(KnowledgeObjectValidationError, match="重新启用"):
        service.archive(old.knowledge_object.id)


def test_delete_successor_is_refused_until_chain_resolved(
    service: KnowledgeObjectService,
) -> None:
    old = _create(service, kind="fact", title="旧", content="内容")
    new = _create(service, kind="fact", title="新", content="内容")
    service.supersede(old.knowledge_object.id, new.knowledge_object.id)

    with pytest.raises(KnowledgeObjectValidationError, match="后继"):
        service.delete(new.knowledge_object.id)

    service.reactivate(old.knowledge_object.id)
    service.delete(new.knowledge_object.id)
    with pytest.raises(KnowledgeObjectNotFoundError):
        service.get_view(new.knowledge_object.id)


def test_delete_keeps_memory_and_revisions(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="problem", title="问题", content="内容")
    from src.knowledge_memory_service import KnowledgeMemoryService

    KnowledgeMemoryService(database).create_entry(
        kind="experience", title="记忆", knowledge_object_id=view.knowledge_object.id
    )
    service.delete(view.knowledge_object.id)

    memory = database.list_knowledge_memory_entries()[0]
    assert memory.knowledge_object_id is None
    revisions = [
        item for item in database.list_knowledge_revisions(view.knowledge_object.id)
    ]
    assert len(revisions) == 1


def test_link_source_and_missing_status(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="concept", title="概念", content="内容")
    _, page_id = _seed_document_and_page(database)

    linked = service.link_source(
        view.knowledge_object.id, source_type="page", source_id=page_id
    )
    assert linked.status is KnowledgeSourceStatus.VALID
    with pytest.raises(KnowledgeSourceLinkError, match="不存在"):
        service.link_source(
            view.knowledge_object.id, source_type="page", source_id=9999
        )

    with database._connection() as connection:  # noqa: SLF001 - 测试内清理
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))
    refreshed = service.source_views(view.knowledge_object.id)
    assert refreshed[0].status is KnowledgeSourceStatus.MISSING


def test_source_events_write_revisions(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = _create(service, kind="concept", title="概念", content="内容")
    _, page_id = _seed_document_and_page(database)
    service.link_source(view.knowledge_object.id, source_type="page", source_id=page_id)
    source = database.list_knowledge_object_sources(view.knowledge_object.id)[0]
    service.unlink_source(source.id)

    revisions = database.list_knowledge_revisions(view.knowledge_object.id)
    event_types = [item.event_type for item in revisions]
    assert KnowledgeRevisionEventType.SOURCE_LINKED in event_types
    assert KnowledgeRevisionEventType.SOURCE_UNLINKED in event_types


def test_create_with_invalid_source_link_rejected(
    service: KnowledgeObjectService,
) -> None:
    with pytest.raises(KnowledgeObjectValidationError, match="三元组"):
        _create(service,
            kind="concept", title="概念", content="内容", source_links=[(1, 2)]
        )
    with pytest.raises(KnowledgeObjectValidationError, match="ID 必须大于 0"):
        _create(service,
            kind="concept",
            title="概念",
            content="内容",
            source_links=[("page", 0, "")],
        )


def test_relation_validation(service: KnowledgeObjectService) -> None:
    first = _create(service, kind="concept", title="A", content="内容")
    second = _create(service, kind="concept", title="B", content="内容")

    relation = service.add_relation(
        first.knowledge_object.id,
        second.knowledge_object.id,
        relation_type=KnowledgeRelationType.SUPPORTS,
    )
    assert relation.relation_type is KnowledgeRelationType.SUPPORTS
    with pytest.raises(KnowledgeObjectValidationError, match="自身"):
        service.add_relation(
            first.knowledge_object.id,
            first.knowledge_object.id,
            relation_type="relates_to",
        )
    with pytest.raises(KnowledgeObjectValidationError, match="已经存在"):
        service.add_relation(
            first.knowledge_object.id,
            second.knowledge_object.id,
            relation_type="supports",
        )
    service.remove_relation(relation.id)
    assert service.relations(first.knowledge_object.id) == []


def test_revision_write_failure_rolls_back_business_change(
    service: KnowledgeObjectService, database: Database, monkeypatch
) -> None:
    view = _create(service, kind="concept", title="概念", content="内容")

    def _fail(*args, **kwargs):
        raise RuntimeError("revision 写入失败")

    monkeypatch.setattr(service, "_insert_revision", _fail)
    with pytest.raises(RuntimeError):
        service.update_content(view.knowledge_object.id, content="不应落库")
    assert service.get(view.knowledge_object.id).content == "内容"
    assert service.get(view.knowledge_object.id).current_revision == 1
