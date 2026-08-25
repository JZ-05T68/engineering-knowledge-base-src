"""Tests for the read-only ContextItem projection layer (v0.5.3 Phase 2-B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_context import ContextItemProjector, ContextProjectionError
from src.knowledge_object_service import KnowledgeObjectService
from src.models import (
    EVIDENCE_STABLE_TYPE,
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    KnowledgeRelationType,
    NoteImportance,
    PageStatus,
    build_stable_id,
)

_DOC_SHA256 = "a" * 64


def _document(database: Database, tmp_path: Path):
    source_path = tmp_path / "motor.pdf"
    source_path.write_bytes(b"%PDF-1.4 test")
    return database.create_document(
        title="电机手册",
        filename="motor.pdf",
        source_path=source_path,
        sha256=_DOC_SHA256,
        page_count=1,
    )


def _page(database: Database, document_id: int, tmp_path: Path):
    image_path = tmp_path / "page-1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return database.create_page(
        document_id=document_id,
        page_number=1,
        image_path=image_path,
        extracted_text="提取文本：编码器接线",
        ocr_text="",
        status=PageStatus.REVIEWED,
    )


def test_page_projection(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, tmp_path)
    page = _page(database, document.id, tmp_path)
    projector = ContextItemProjector(database)

    item = projector.project_page(page.id)

    assert item.type.value == "page"
    assert item.local_id == page.id
    assert item.stable_id == build_stable_id(
        database.get_knowledge_base_uuid(), PAGE_STABLE_TYPE, page.id
    )
    assert item.title == "电机手册 · 第 1 页"
    assert item.content == "提取文本：编码器接线"
    assert item.kind is None and item.kind_label is None
    assert item.status == "reviewed"
    assert item.importance is None
    assert item.relation_refs == ()
    assert [(anchor.anchor_type, anchor.anchor_id) for anchor in item.source_anchors] == [
        ("document", document.id),
        ("page", page.id),
    ]


def test_knowledge_object_projection_with_sources_and_relations(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, tmp_path)
    page = _page(database, document.id, tmp_path)
    service = KnowledgeObjectService(database)
    first = database.create_knowledge_object(
        kind="fact",
        title="编码器 A/B 相正交",
        content="正交编码器 A/B 相相差 90 度。",
    )
    second = database.create_knowledge_object(
        kind="experience",
        title="编码器接线经验",
        content="接线错误导致 PID 震荡。",
    )
    service.link_source(first.id, source_type="page", source_id=page.id)
    service.add_relation(
        first.id, second.id, relation_type=KnowledgeRelationType.SUPPORTS
    )
    projector = ContextItemProjector(database)

    item = projector.project_knowledge_object(first.id)

    assert item.type.value == "knowledge_object"
    assert item.stable_id == database.knowledge_object_stable_id(first.id)
    assert item.kind == "fact"
    assert item.status == "active"
    assert item.importance == NoteImportance.NORMAL.value
    assert item.revision_ref is not None and "当前第" in item.revision_ref
    assert [anchor.anchor_type for anchor in item.source_anchors] == ["page"]
    assert item.source_anchors[0].fingerprint_state == "valid"
    assert len(item.relation_refs) == 1
    relation = item.relation_refs[0]
    assert relation.relation_type == "supports"
    assert relation.direction == "outgoing"
    assert relation.target_stable_id == database.knowledge_object_stable_id(second.id)


def test_knowledge_memory_projection(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, tmp_path)
    page = _page(database, document.id, tmp_path)
    entry = database.create_knowledge_memory_entry(
        kind="problem_solving",
        title="STM32 编码器故障",
        content="电机抖动",
        root_cause="A/B 相接反",
        lesson="先核对相序",
        outcome="PID 稳定",
        context_conditions="正交编码器",
        document_id=document.id,
        page_id=page.id,
    )
    projector = ContextItemProjector(database)

    item = projector.project_knowledge_memory(entry.id)

    assert item.type.value == "knowledge_memory"
    assert item.stable_id == build_stable_id(
        database.get_knowledge_base_uuid(), KNOWLEDGE_MEMORY_STABLE_TYPE, entry.id
    )
    assert item.kind == "problem_solving"
    assert item.status == "active"
    assert item.revision_ref == "第 1 版"
    assert "根因：A/B 相接反" in item.content
    assert "教训：先核对相序" in item.content
    assert "结果：PID 稳定" in item.content
    assert "适用条件：正交编码器" in item.content
    assert [(anchor.anchor_type, anchor.anchor_id) for anchor in item.source_anchors] == [
        ("document", document.id),
        ("page", page.id),
    ]


def test_evidence_projection(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, tmp_path)
    page = _page(database, document.id, tmp_path)
    evidence = EvidenceBasketService(database).add_item(
        document_id=document.id,
        page_id=page.id,
        evidence_text="编码器 A/B 相必须正交。",
    )
    projector = ContextItemProjector(database)

    item = projector.project_evidence(evidence.id)

    assert item.type.value == "evidence"
    assert item.stable_id == build_stable_id(
        database.get_knowledge_base_uuid(), EVIDENCE_STABLE_TYPE, evidence.id
    )
    assert item.content == "编码器 A/B 相必须正交。"
    assert item.kind == "text_selection"
    assert item.status == "unconfirmed"
    assert item.status_label == "未确认"
    assert [anchor.anchor_type for anchor in item.source_anchors] == [
        "document",
        "page",
    ]


def test_projection_is_read_only(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, tmp_path)
    page = _page(database, document.id, tmp_path)
    knowledge_object = database.create_knowledge_object(
        kind="fact", title="事实", content="内容"
    )
    memory = database.create_knowledge_memory_entry(
        kind="experience", title="记忆", content="内容"
    )
    evidence = EvidenceBasketService(database).add_item(
        document_id=document.id, page_id=page.id, evidence_text="证据"
    )
    projector = ContextItemProjector(database)
    before = database.database_path.read_bytes()

    projector.project_page(page.id)
    projector.project_knowledge_object(knowledge_object.id)
    projector.project_knowledge_memory(memory.id)
    projector.project_evidence(evidence.id)

    assert database.database_path.read_bytes() == before


def test_missing_targets_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    projector = ContextItemProjector(database)

    with pytest.raises(ContextProjectionError, match="页面不存在"):
        projector.project_page(999)
    with pytest.raises(ContextProjectionError, match="知识对象不存在"):
        projector.project_knowledge_object(999)
    with pytest.raises(ContextProjectionError, match="知识记忆不存在"):
        projector.project_knowledge_memory(999)
    with pytest.raises(ContextProjectionError, match="证据条目不存在"):
        projector.project_evidence(999)
