"""Database data-access tests for the schema v10 knowledge foundation tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database, DatabaseError, RecordNotFoundError
from src.models import (
    KnowledgeAuthorship,
    KnowledgeConfirmationStatus,
    KnowledgeEpistemicBasis,
    KnowledgeLifecycle,
    KnowledgeMemoryEntryKind,
    KnowledgeMemoryStatus,
    KnowledgeObjectKind,
    KnowledgeObjectSourceType,
    KnowledgeRelationType,
    KnowledgeRevisionEventType,
    NoteImportance,
)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


def test_knowledge_object_create_and_get_orthogonal_defaults(database: Database) -> None:
    created = database.create_knowledge_object(
        kind=KnowledgeObjectKind.EXPERIENCE,
        title="PID 参数与采样频率",
        content="采样频率不匹配会导致底盘抖动。",
        importance=NoteImportance.PRIMARY,
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
    )

    loaded = database.get_knowledge_object(created.id)

    assert loaded is not None
    assert loaded.kind is KnowledgeObjectKind.EXPERIENCE
    assert loaded.authorship is KnowledgeAuthorship.USER
    assert loaded.epistemic_basis is KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE
    assert loaded.lifecycle is KnowledgeLifecycle.ACTIVE
    assert loaded.confirmation_status is KnowledgeConfirmationStatus.UNCONFIRMED
    assert loaded.confirmed_at is None
    assert loaded.confirmed_revision is None
    assert loaded.current_revision == 1
    assert loaded.superseded_by_ko_id is None


def test_create_knowledge_object_rejects_ai_authorship(database: Database) -> None:
    with pytest.raises(ValueError, match="AI 署名"):
        database.create_knowledge_object(
            kind="concept", title="概念", content="内容", authorship=KnowledgeAuthorship.AI
        )
    with pytest.raises(ValueError, match="未确认"):
        database.create_knowledge_object(
            kind="concept",
            title="概念",
            content="内容",
            confirmation_status=KnowledgeConfirmationStatus.CONFIRMED,
        )


def test_knowledge_object_validation_rejects_bad_values(database: Database) -> None:
    with pytest.raises(ValueError, match="标题不能为空"):
        database.create_knowledge_object(kind="concept", title="  ", content="内容")
    with pytest.raises(ValueError, match="内容不能为空"):
        database.create_knowledge_object(kind="concept", title="标题", content=" ")
    with pytest.raises(ValueError):
        database.create_knowledge_object(kind="diary", title="标题", content="内容")
    with pytest.raises(ValueError):
        database.create_knowledge_object(
            kind="concept", title="标题", content="内容", epistemic_basis="telepathy"
        )
    with pytest.raises(ValueError):
        database.create_knowledge_object(
            kind="concept", title="标题", content="内容", lifecycle="published"
        )


def test_knowledge_object_content_update_advances_revision(database: Database) -> None:
    created = database.create_knowledge_object(kind="fact", title="旧标题", content="旧内容")

    updated = database.update_knowledge_object_content(
        created.id, new_revision=2, title="新标题", content="新内容"
    )

    assert updated.title == "新标题"
    assert updated.content == "新内容"
    assert updated.current_revision == 2
    with pytest.raises(RecordNotFoundError):
        database.update_knowledge_object_content(999, new_revision=2, content="x")


def test_knowledge_object_confirmation_lifecycle_updates(database: Database) -> None:
    created = database.create_knowledge_object(kind="fact", title="事实", content="内容")

    confirmed = database.update_knowledge_object_confirmation(
        created.id,
        confirmation_status="confirmed",
        confirmed_at="2026-08-02T00:00:00+00:00",
        confirmed_revision=1,
    )
    assert confirmed.confirmation_status is KnowledgeConfirmationStatus.CONFIRMED
    assert confirmed.confirmed_revision == 1

    unconfirmed = database.update_knowledge_object_confirmation(
        created.id,
        confirmation_status="unconfirmed",
        confirmed_at=None,
        confirmed_revision=None,
    )
    assert unconfirmed.confirmation_status is KnowledgeConfirmationStatus.UNCONFIRMED
    assert unconfirmed.confirmed_at is None

    with pytest.raises(ValueError):
        database.update_knowledge_object_confirmation(
            created.id, confirmation_status="confirmed", confirmed_at=None, confirmed_revision=None
        )


def test_knowledge_object_lifecycle_update_with_pointer(database: Database) -> None:
    first = database.create_knowledge_object(kind="fact", title="A", content="内容")
    second = database.create_knowledge_object(kind="fact", title="B", content="内容")

    superseded = database.update_knowledge_object_lifecycle(
        first.id, lifecycle="superseded", superseded_by_ko_id=second.id
    )
    assert superseded.lifecycle is KnowledgeLifecycle.SUPERSEDED
    assert superseded.superseded_by_ko_id == second.id
    assert database.count_inbound_supersessions(second.id) == 1

    reactivated = database.update_knowledge_object_lifecycle(
        first.id, lifecycle="active", superseded_by_ko_id=None
    )
    assert reactivated.lifecycle is KnowledgeLifecycle.ACTIVE
    assert reactivated.superseded_by_ko_id is None


def test_knowledge_object_delete_restrict_guard(database: Database) -> None:
    first = database.create_knowledge_object(kind="fact", title="A", content="内容")
    second = database.create_knowledge_object(kind="fact", title="B", content="内容")
    database.update_knowledge_object_lifecycle(
        first.id, lifecycle="superseded", superseded_by_ko_id=second.id
    )

    with pytest.raises(DatabaseError, match="替代后继"):
        database.delete_knowledge_object(second.id)

    database.update_knowledge_object_lifecycle(
        first.id, lifecycle="active", superseded_by_ko_id=None
    )
    database.delete_knowledge_object(second.id)
    assert database.get_knowledge_object(second.id) is None


def test_knowledge_object_list_filters_sort_and_pagination(database: Database) -> None:
    first = database.create_knowledge_object(
        kind="experience",
        title="阿尔法",
        content="关于泵站维护的经验",
        importance="primary",
        epistemic_basis="personal_experience",
    )
    second = database.create_knowledge_object(
        kind="concept", title="贝塔", content="关于电机的概念"
    )
    third = database.create_knowledge_object(
        kind="problem",
        title="伽马",
        content="关于噪声的问题",
        importance="secondary",
        epistemic_basis="problem_definition",
    )
    database.update_knowledge_object_confirmation(
        third.id, confirmation_status="confirmed",
        confirmed_at="2026-08-02T00:00:00+00:00", confirmed_revision=1,
    )

    assert [item.id for item in database.list_knowledge_objects()] == [
        third.id, second.id, first.id,
    ]
    assert [item.id for item in database.list_knowledge_objects(kind="experience")] == [first.id]
    assert [item.id for item in database.list_knowledge_objects(importance="normal")] == [second.id]
    assert [item.id for item in database.list_knowledge_objects(
        confirmation_status="confirmed")] == [third.id]
    assert [item.id for item in database.list_knowledge_objects(
        epistemic_basis="personal_experience")] == [first.id]
    assert [item.id for item in database.list_knowledge_objects(query="泵站")] == [first.id]
    assert [item.id for item in database.list_knowledge_objects(limit=2, offset=1)] == [
        second.id, first.id,
    ]
    assert database.count_knowledge_objects() == 3
    assert database.count_knowledge_objects(query="电机") == 1


def test_knowledge_object_source_link_lifecycle(database: Database) -> None:
    ko = database.create_knowledge_object(kind="fact", title="事实", content="内容")

    source = database.add_knowledge_object_source(
        knowledge_object_id=ko.id,
        source_type=KnowledgeObjectSourceType.PAGE,
        source_id=7,
        source_note=" 关键段落 ",
    )

    loaded = database.get_knowledge_object_source(source.id)
    assert loaded is not None
    assert loaded.source_note == "关键段落"
    assert [item.id for item in database.list_knowledge_object_sources(ko.id)] == [source.id]
    with pytest.raises(DatabaseError, match="已经关联"):
        database.add_knowledge_object_source(
            knowledge_object_id=ko.id, source_type="page", source_id=7
        )
    database.remove_knowledge_object_source(source.id)
    assert database.list_knowledge_object_sources(ko.id) == []


def test_knowledge_relation_lifecycle_and_validation(database: Database) -> None:
    first = database.create_knowledge_object(kind="problem", title="问题", content="内容")
    second = database.create_knowledge_object(kind="experience", title="经验", content="内容")

    relation = database.add_knowledge_relation(
        source_ko_id=first.id,
        target_ko_id=second.id,
        relation_type=KnowledgeRelationType.DERIVED_FROM,
        description=" 从问题提炼 ",
    )

    assert relation.description == "从问题提炼"
    assert [item.id for item in database.list_knowledge_relations(first.id)] == [relation.id]
    with pytest.raises(ValueError, match="自身"):
        database.add_knowledge_relation(
            source_ko_id=first.id, target_ko_id=first.id, relation_type="relates_to"
        )
    with pytest.raises(DatabaseError, match="已经存在"):
        database.add_knowledge_relation(
            source_ko_id=first.id, target_ko_id=second.id, relation_type="derived_from"
        )
    database.remove_knowledge_relation(relation.id)
    assert database.list_knowledge_relations(first.id) == []


def test_knowledge_memory_entry_lifecycle_and_status(database: Database) -> None:
    ko = database.create_knowledge_object(kind="problem", title="抖动", content="内容")

    entry = database.create_knowledge_memory_entry(
        kind=KnowledgeMemoryEntryKind.PROBLEM_SOLVING,
        title="STM32 电机控制异常",
        content="修改 PWM、调整 PID 均无效。",
        root_cause="编码器中断配置错误。",
        lesson="高速控制系统优先检查时序问题。",
        knowledge_object_id=ko.id,
    )

    loaded = database.get_knowledge_memory_entry(entry.id)
    assert loaded is not None
    assert loaded.status is KnowledgeMemoryStatus.ACTIVE

    archived = database.update_knowledge_memory_status(entry.id, status="archived")
    assert archived.status is KnowledgeMemoryStatus.ARCHIVED
    assert [item.id for item in database.list_knowledge_memory_entries(status="archived")] == [
        entry.id
    ]
    assert database.count_knowledge_memory_entries(status="active") == 0

    database.delete_knowledge_memory_entry(entry.id)
    assert database.get_knowledge_memory_entry(entry.id) is None


def test_knowledge_memory_entry_validation(database: Database) -> None:
    with pytest.raises(ValueError, match="标题不能为空"):
        database.create_knowledge_memory_entry(kind="experience", title="  ")
    with pytest.raises(ValueError):
        database.create_knowledge_memory_entry(kind="knowledge_change", title="标题")
    with pytest.raises(ValueError):
        database.create_knowledge_memory_entry(kind="experience", title="标题", page_id=0)


def test_knowledge_revisions_are_insert_only(database: Database) -> None:
    ko = database.create_knowledge_object(kind="concept", title="概念", content="内容")
    stable_id = database.knowledge_object_stable_id(ko.id)

    first = database.insert_knowledge_revision(
        knowledge_object_id=ko.id,
        object_local_id_snapshot=ko.id,
        object_stable_id_snapshot=stable_id,
        object_title_snapshot=ko.title,
        object_kind_snapshot=ko.kind.value,
        revision_number=1,
        event_type=KnowledgeRevisionEventType.CREATED,
        after_content=ko.content,
        detail="创建",
    )
    assert first.id > 0
    assert database.next_knowledge_revision_number(ko.id) == 2

    revisions = database.list_knowledge_revisions(ko.id)
    assert [item.revision_number for item in revisions] == [1]
    assert revisions[0].object_stable_id_snapshot == stable_id
    # No update/delete API exists on the database layer.
    assert not hasattr(database, "update_knowledge_revision")
    assert not hasattr(database, "delete_knowledge_revision")


def test_kb_uuid_stable_and_stable_id_format(database: Database) -> None:
    kb_uuid = database.get_knowledge_base_uuid()
    assert len(kb_uuid) == 36
    ko = database.create_knowledge_object(kind="fact", title="事实", content="内容")
    assert database.knowledge_object_stable_id(ko.id) == f"{kb_uuid}:knowledge_object:{ko.id}"
