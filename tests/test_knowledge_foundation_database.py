"""Database data-access tests for the schema v9 knowledge foundation tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database, DatabaseError, RecordNotFoundError
from src.models import (
    KnowledgeMemoryEntryKind,
    KnowledgeObjectKind,
    KnowledgeObjectSourceType,
    KnowledgeObjectStatus,
    KnowledgeRelationType,
    NoteImportance,
)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


def test_knowledge_object_create_and_get(database: Database) -> None:
    created = database.create_knowledge_object(
        kind=KnowledgeObjectKind.EXPERIENCE,
        title="PID 参数与采样频率",
        content="采样频率不匹配会导致底盘抖动。",
        importance=NoteImportance.PRIMARY,
        status=KnowledgeObjectStatus.DRAFT,
    )

    loaded = database.get_knowledge_object(created.id)

    assert loaded is not None
    assert loaded.kind is KnowledgeObjectKind.EXPERIENCE
    assert loaded.title == "PID 参数与采样频率"
    assert loaded.importance is NoteImportance.PRIMARY
    assert loaded.status is KnowledgeObjectStatus.DRAFT
    assert loaded.reviewed_at is None
    assert loaded.created_at == loaded.updated_at


def test_knowledge_object_create_reviewed_stamps_reviewed_at(database: Database) -> None:
    created = database.create_knowledge_object(
        kind="concept", title="概念", content="内容", status="reviewed"
    )

    assert created.status is KnowledgeObjectStatus.REVIEWED
    assert created.reviewed_at is not None


def test_knowledge_object_validation_rejects_bad_values(database: Database) -> None:
    with pytest.raises(ValueError, match="标题不能为空"):
        database.create_knowledge_object(kind="concept", title="  ", content="内容")
    with pytest.raises(ValueError, match="内容不能为空"):
        database.create_knowledge_object(kind="concept", title="标题", content=" ")
    with pytest.raises(ValueError):
        database.create_knowledge_object(kind="diary", title="标题", content="内容")
    with pytest.raises(ValueError):
        database.create_knowledge_object(
            kind="concept", title="标题", content="内容", importance="urgent"
        )
    with pytest.raises(ValueError):
        database.create_knowledge_object(
            kind="concept", title="标题", content="内容", status="published"
        )


def test_knowledge_object_update_fields_and_status(database: Database) -> None:
    created = database.create_knowledge_object(kind="fact", title="旧标题", content="旧内容")

    updated = database.update_knowledge_object(
        created.id, title="新标题", content="新内容", importance="secondary"
    )
    reviewed = database.update_knowledge_object(created.id, status="reviewed")
    un_reviewed = database.update_knowledge_object(created.id, status="archived")

    assert updated.title == "新标题"
    assert updated.content == "新内容"
    assert updated.importance is NoteImportance.SECONDARY
    assert reviewed.status is KnowledgeObjectStatus.REVIEWED
    assert reviewed.reviewed_at is not None
    assert un_reviewed.status is KnowledgeObjectStatus.ARCHIVED
    assert un_reviewed.reviewed_at is None


def test_knowledge_object_update_missing_raises(database: Database) -> None:
    with pytest.raises(RecordNotFoundError):
        database.update_knowledge_object(999, title="不存在")


def test_knowledge_object_list_filters_sort_and_pagination(database: Database) -> None:
    first = database.create_knowledge_object(
        kind="experience", title="阿尔法", content="关于泵站维护的经验", importance="primary"
    )
    second = database.create_knowledge_object(
        kind="concept", title="贝塔", content="关于电机的概念", importance="normal"
    )
    third = database.create_knowledge_object(
        kind="problem",
        title="伽马",
        content="关于噪声的问题",
        importance="secondary",
        status="reviewed",
    )

    assert [item.id for item in database.list_knowledge_objects()] == [
        third.id, second.id, first.id,
    ]
    assert [item.id for item in database.list_knowledge_objects(kind="experience")] == [first.id]
    assert [item.id for item in database.list_knowledge_objects(importance="normal")] == [second.id]
    assert [item.id for item in database.list_knowledge_objects(status="reviewed")] == [third.id]
    assert [item.id for item in database.list_knowledge_objects(query="泵站")] == [first.id]
    assert [item.id for item in database.list_knowledge_objects(query="电机")] == [second.id]
    assert [item.id for item in database.list_knowledge_objects(limit=2, offset=1)] == [
        second.id, first.id,
    ]
    assert database.count_knowledge_objects() == 3
    assert database.count_knowledge_objects(query="电机") == 1


def test_knowledge_object_title_sort_uses_binary_collation(database: Database) -> None:
    first = database.create_knowledge_object(kind="fact", title="alpha", content="内容")
    second = database.create_knowledge_object(kind="fact", title="beta", content="内容")
    third = database.create_knowledge_object(kind="fact", title="gamma", content="内容")

    assert [item.id for item in database.list_knowledge_objects(sort_by="title_asc")] == [
        first.id, second.id, third.id,
    ]


def test_knowledge_object_delete_cascades_links(database: Database) -> None:
    first = database.create_knowledge_object(kind="concept", title="A", content="内容")
    second = database.create_knowledge_object(kind="concept", title="B", content="内容")
    database.add_knowledge_object_source(
        knowledge_object_id=first.id, source_type="page", source_id=1
    )
    database.add_knowledge_relation(
        source_ko_id=first.id, target_ko_id=second.id, relation_type="supports"
    )

    database.delete_knowledge_object(first.id)

    assert database.get_knowledge_object(first.id) is None
    assert database.list_knowledge_object_sources(first.id) == []
    assert database.list_knowledge_relations(first.id) == []
    with pytest.raises(RecordNotFoundError):
        database.delete_knowledge_object(first.id)


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
    assert loaded.source_type is KnowledgeObjectSourceType.PAGE
    assert loaded.source_id == 7
    assert loaded.source_note == "关键段落"
    assert [item.id for item in database.list_knowledge_object_sources(ko.id)] == [source.id]

    with pytest.raises(DatabaseError, match="已经关联"):
        database.add_knowledge_object_source(
            knowledge_object_id=ko.id, source_type="page", source_id=7
        )
    with pytest.raises(RecordNotFoundError, match="知识对象不存在"):
        database.add_knowledge_object_source(
            knowledge_object_id=999, source_type="page", source_id=7
        )

    database.remove_knowledge_object_source(source.id)
    assert database.list_knowledge_object_sources(ko.id) == []
    with pytest.raises(RecordNotFoundError):
        database.remove_knowledge_object_source(source.id)


def test_knowledge_relation_lifecycle_and_validation(database: Database) -> None:
    first = database.create_knowledge_object(kind="problem", title="问题", content="内容")
    second = database.create_knowledge_object(kind="experience", title="经验", content="内容")

    relation = database.add_knowledge_relation(
        source_ko_id=first.id,
        target_ko_id=second.id,
        relation_type=KnowledgeRelationType.DERIVED_FROM,
        description=" 从问题提炼 ",
    )

    assert database.get_knowledge_relation(relation.id) is not None
    assert relation.description == "从问题提炼"
    relations = database.list_knowledge_relations(first.id)
    assert [item.id for item in relations] == [relation.id]
    assert database.list_knowledge_relations(second.id)[0].id == relation.id

    with pytest.raises(ValueError, match="自身"):
        database.add_knowledge_relation(
            source_ko_id=first.id, target_ko_id=first.id, relation_type="relates_to"
        )
    with pytest.raises(DatabaseError, match="已经存在"):
        database.add_knowledge_relation(
            source_ko_id=first.id, target_ko_id=second.id, relation_type="derived_from"
        )
    with pytest.raises(RecordNotFoundError, match="不存在"):
        database.add_knowledge_relation(
            source_ko_id=first.id, target_ko_id=999, relation_type="relates_to"
        )

    database.remove_knowledge_relation(relation.id)
    assert database.list_knowledge_relations(first.id) == []
    with pytest.raises(RecordNotFoundError):
        database.remove_knowledge_relation(relation.id)


def test_knowledge_memory_entry_lifecycle(database: Database) -> None:
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
    assert loaded.kind is KnowledgeMemoryEntryKind.PROBLEM_SOLVING
    assert loaded.knowledge_object_id == ko.id
    assert loaded.root_cause == "编码器中断配置错误。"

    updated = database.update_knowledge_memory_entry(
        entry.id, lesson="高速系统优先检查中断与时序。"
    )
    assert updated.lesson == "高速系统优先检查中断与时序。"

    assert [item.id for item in database.list_knowledge_memory_entries()] == [entry.id]
    assert [item.id for item in database.list_knowledge_memory_entries(kind="experience")] == []
    assert database.count_knowledge_memory_entries() == 1

    database.delete_knowledge_memory_entry(entry.id)
    assert database.get_knowledge_memory_entry(entry.id) is None
    with pytest.raises(RecordNotFoundError):
        database.delete_knowledge_memory_entry(entry.id)


def test_knowledge_memory_entry_links_nullified_on_ko_delete(database: Database) -> None:
    ko = database.create_knowledge_object(kind="decision", title="决策", content="内容")
    entry = database.create_knowledge_memory_entry(
        kind="decision", title="决策记录", knowledge_object_id=ko.id
    )

    database.delete_knowledge_object(ko.id)

    survived = database.get_knowledge_memory_entry(entry.id)
    assert survived is not None
    assert survived.knowledge_object_id is None


def test_knowledge_memory_entry_validation(database: Database) -> None:
    with pytest.raises(ValueError, match="标题不能为空"):
        database.create_knowledge_memory_entry(kind="experience", title="  ")
    with pytest.raises(ValueError):
        database.create_knowledge_memory_entry(kind="diary", title="标题")
    with pytest.raises(ValueError):
        database.create_knowledge_memory_entry(kind="experience", title="标题", page_id=0)
