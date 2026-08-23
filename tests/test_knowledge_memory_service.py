"""Service tests for the v0.5.2 knowledge-memory domain rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
    KnowledgeMemoryValidationError,
)
from src.models import KnowledgeMemoryEntryKind


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


@pytest.fixture()
def service(database: Database) -> KnowledgeMemoryService:
    return KnowledgeMemoryService(database)


def test_create_list_update_delete_entry(service: KnowledgeMemoryService) -> None:
    entry = service.create_entry(
        kind=KnowledgeMemoryEntryKind.PROBLEM_SOLVING,
        title="STM32 电机控制异常",
        content="修改 PWM、调整 PID 均无效。",
        root_cause="编码器中断配置错误。",
        lesson="高速控制系统优先检查时序问题。",
    )

    assert service.get(entry.id) is not None
    assert [item.id for item in service.list()] == [entry.id]
    assert service.count() == 1
    assert service.count(kind="experience") == 0

    updated = service.update_entry(entry.id, lesson="优先检查中断与时序。")
    assert updated.lesson == "优先检查中断与时序。"

    service.delete_entry(entry.id)
    assert service.get(entry.id) is None
    with pytest.raises(KnowledgeMemoryEntryNotFoundError):
        service.delete_entry(entry.id)


def test_knowledge_change_kind_cannot_be_created_manually(
    service: KnowledgeMemoryService,
) -> None:
    with pytest.raises(KnowledgeMemoryValidationError, match="自动生成"):
        service.create_entry(kind="knowledge_change", title="手动变更")


def test_missing_links_rejected_with_clear_errors(
    service: KnowledgeMemoryService, database: Database
) -> None:
    with pytest.raises(KnowledgeMemoryValidationError, match="知识对象不存在"):
        service.create_entry(kind="experience", title="经验", knowledge_object_id=999)
    with pytest.raises(KnowledgeMemoryValidationError, match="文档不存在"):
        service.create_entry(kind="experience", title="经验", document_id=999)
    with pytest.raises(KnowledgeMemoryValidationError, match="页面不存在"):
        service.create_entry(kind="experience", title="经验", page_id=999)


def test_update_missing_entry_raises(service: KnowledgeMemoryService) -> None:
    with pytest.raises(KnowledgeMemoryEntryNotFoundError):
        service.update_entry(999, title="不存在")
