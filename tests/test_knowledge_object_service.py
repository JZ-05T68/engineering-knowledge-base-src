"""Service tests for the v0.5.2 knowledge-object domain rules."""

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
    KnowledgeMemoryEntryKind,
    KnowledgeObjectKind,
    KnowledgeObjectSourceType,
    KnowledgeObjectStatus,
    KnowledgeRelationType,
    KnowledgeSourceStatus,
    NoteImportance,
)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


@pytest.fixture()
def service(database: Database) -> KnowledgeObjectService:
    return KnowledgeObjectService(database)


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


def test_create_draft_without_sources_and_view(service: KnowledgeObjectService) -> None:
    view = service.create(
        kind=KnowledgeObjectKind.EXPERIENCE,
        title="PID 参数经验",
        content="采样频率必须匹配。",
        importance=NoteImportance.PRIMARY,
    )

    assert view.knowledge_object.status is KnowledgeObjectStatus.DRAFT
    assert view.sources == ()
    assert view.outgoing_relations == ()
    assert view.incoming_relations == ()


def test_create_reviewed_without_source_is_rejected(service: KnowledgeObjectService) -> None:
    with pytest.raises(KnowledgeObjectValidationError, match="有效来源"):
        service.create(
            kind="concept", title="概念", content="内容", status="reviewed"
        )


def test_create_reviewed_with_valid_source_ok(
    service: KnowledgeObjectService, database: Database
) -> None:
    _, page_id = _seed_document_and_page(database)

    view = service.create(
        kind="fact",
        title="事实",
        content="内容",
        status="reviewed",
        source_links=[(KnowledgeObjectSourceType.PAGE, page_id, "关键页")],
    )

    assert view.knowledge_object.status is KnowledgeObjectStatus.REVIEWED
    assert view.knowledge_object.reviewed_at is not None
    assert len(view.sources) == 1
    assert view.sources[0].status is KnowledgeSourceStatus.VALID


def test_create_with_invalid_source_link_rejected(
    service: KnowledgeObjectService, database: Database
) -> None:
    with pytest.raises(KnowledgeObjectValidationError, match="三元组"):
        service.create(
            kind="concept", title="概念", content="内容", source_links=[(1, 2)]
        )
    with pytest.raises(KnowledgeObjectValidationError, match="ID 必须大于 0"):
        service.create(
            kind="concept",
            title="概念",
            content="内容",
            source_links=[("page", 0, "")],
        )


def test_link_source_and_missing_status(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = service.create(kind="concept", title="概念", content="内容")
    _, page_id = _seed_document_and_page(database)

    linked = service.link_source(
        view.knowledge_object.id, source_type="page", source_id=page_id
    )
    assert linked.status is KnowledgeSourceStatus.VALID

    with pytest.raises(KnowledgeSourceLinkError, match="不存在"):
        service.link_source(
            view.knowledge_object.id, source_type="page", source_id=9999
        )

    # 直接删除页面后，来源状态应实时变为 MISSING。
    with database._connection() as connection:  # noqa: SLF001 - 测试内清理
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))

    refreshed = service.source_views(view.knowledge_object.id)
    assert refreshed[0].status is KnowledgeSourceStatus.MISSING


def test_review_requires_valid_source(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = service.create(kind="concept", title="概念", content="内容")
    with pytest.raises(KnowledgeObjectValidationError, match="有效来源"):
        service.update(view.knowledge_object.id, status="reviewed")

    _, page_id = _seed_document_and_page(database)
    service.link_source(view.knowledge_object.id, source_type="page", source_id=page_id)
    updated = service.update(view.knowledge_object.id, status="reviewed")
    assert updated.knowledge_object.status is KnowledgeObjectStatus.REVIEWED


def test_update_leaving_reviewed_clears_reviewed_at(
    service: KnowledgeObjectService, database: Database
) -> None:
    _, page_id = _seed_document_and_page(database)
    view = service.create(
        kind="fact",
        title="事实",
        content="内容",
        status="reviewed",
        source_links=[("page", page_id, "")],
    )
    assert view.knowledge_object.reviewed_at is not None

    archived = service.update(view.knowledge_object.id, status="archived")
    assert archived.knowledge_object.reviewed_at is None


def test_delete_cascades_links_and_keeps_memory(
    service: KnowledgeObjectService, database: Database
) -> None:
    first = service.create(kind="problem", title="问题", content="内容")
    second = service.create(kind="experience", title="经验", content="内容")
    service.add_relation(
        first.knowledge_object.id,
        second.knowledge_object.id,
        relation_type="derived_from",
    )

    service.delete(first.knowledge_object.id)

    with pytest.raises(KnowledgeObjectNotFoundError):
        service.get_view(first.knowledge_object.id)
    assert database.list_knowledge_relations(first.knowledge_object.id) == []
    assert database.count_knowledge_memory_entries() >= 1


def test_relation_validation(
    service: KnowledgeObjectService, database: Database
) -> None:
    first = service.create(kind="concept", title="A", content="内容")
    second = service.create(kind="concept", title="B", content="内容")

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
    with pytest.raises(KnowledgeObjectNotFoundError):
        service.add_relation(first.knowledge_object.id, 9999, relation_type="relates_to")

    service.remove_relation(relation.id)
    assert service.relations(first.knowledge_object.id) == []


def test_auto_knowledge_change_log_appended(
    service: KnowledgeObjectService, database: Database
) -> None:
    view = service.create(kind="concept", title="概念", content="内容")
    service.update(view.knowledge_object.id, content="新内容")

    changes = [
        entry
        for entry in database.list_knowledge_memory_entries(
            kind=KnowledgeMemoryEntryKind.KNOWLEDGE_CHANGE
        )
        if entry.knowledge_object_id == view.knowledge_object.id
    ]
    assert len(changes) == 2
    assert any("创建" in entry.title for entry in changes)
    assert any("更新" in entry.title for entry in changes)
