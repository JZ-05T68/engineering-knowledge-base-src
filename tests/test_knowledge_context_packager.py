"""Tests for the fail-closed KnowledgeContextPackager (v0.5.3 Phase 2-B)."""

from __future__ import annotations

import json

import pytest

from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackager,
)
from src.models import (
    ContextAnchorType,
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    ContextRelationRef,
    ContextSourceAnchor,
)

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _item(
    *,
    item_type: ContextItemType = ContextItemType.KNOWLEDGE_OBJECT,
    local_id: int = 1,
    stable_id: str = f"{KB_UUID}:knowledge_object:1",
    title: str = "知识对象一",
    content: str = "正文内容",
    status: str = "active",
    status_label: str = "现行",
    kind: str = "fact",
    kind_label: str = "事实",
    anchors: tuple[ContextSourceAnchor, ...] = (),
    relations: tuple[ContextRelationRef, ...] = (),
) -> ContextItem:
    return ContextItem(
        type=item_type,
        local_id=local_id,
        stable_id=stable_id,
        title=title,
        content=content,
        kind=kind,
        kind_label=kind_label,
        status=status,
        status_label=status_label,
        importance="normal",
        updated_at=None,
        revision_ref="第 1 版",
        source_anchors=anchors,
        relation_refs=relations,
    )


def _anchor(
    anchor_type: str = ContextAnchorType.PAGE.value,
    anchor_id: int = 7,
    fingerprint_state: str = ContextFingerprintState.VALID.value,
) -> ContextSourceAnchor:
    return ContextSourceAnchor(
        anchor_type=anchor_type,
        anchor_id=anchor_id,
        anchor_label="页面 7",
        fingerprint_state=fingerprint_state,
    )


def test_json_generation_contains_manifest_items_citations_excluded() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID, app_version="0.5.2")
    first = _item()
    second = _item(
        local_id=2,
        stable_id=f"{KB_UUID}:knowledge_object:2",
        title="知识对象二",
        item_type=ContextItemType.KNOWLEDGE_OBJECT,
    )
    archived = _item(
        local_id=3,
        stable_id=f"{KB_UUID}:knowledge_object:3",
        title="已归档对象",
        status="archived",
        status_label="已归档",
    )

    package = packager.build([first, second, archived], question="如何接线？")

    payload = json.loads(package.to_json())
    assert payload["manifest"]["item_count"] == 2
    assert payload["manifest"]["excluded_count"] == 1
    assert payload["manifest"]["question"] == "如何接线？"
    assert payload["manifest"]["kb_uuid"] == KB_UUID
    assert payload["items"][0]["stable_id"] == first.stable_id
    assert payload["citations"] == {
        first.stable_id: "#1",
        second.stable_id: "#2",
    }
    assert payload["excluded"] == [
        {
            "stable_id": archived.stable_id,
            "title": "已归档对象",
            "reason": "已归档（默认排除）",
        }
    ]


def test_markdown_generation_contains_required_blocks() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    item = _item(anchors=(_anchor(),))
    package = packager.build([item], question="用户问题")

    markdown = package.to_markdown()

    assert "# 知识上下文包" in markdown
    assert "用户问题：用户问题" in markdown
    assert item.stable_id in markdown
    assert "来源：" in markdown
    assert "现行" in markdown
    assert "正文：" in markdown
    assert "正文内容" in markdown


def test_empty_context_is_rejected() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)

    with pytest.raises(KnowledgeContextError, match="空上下文"):
        packager.build([])


def test_all_excluded_is_rejected() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    archived = _item(status="archived", status_label="已归档")

    with pytest.raises(KnowledgeContextError, match="所有知识项均被排除"):
        packager.build([archived])


def test_archived_and_superseded_default_exclusion_and_opt_in() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    archived = _item(
        local_id=1,
        stable_id=f"{KB_UUID}:knowledge_object:1",
        status="archived",
        status_label="已归档",
    )
    superseded = _item(
        local_id=2,
        stable_id=f"{KB_UUID}:knowledge_object:2",
        status="superseded",
        status_label="已替代",
    )

    with pytest.raises(KnowledgeContextError, match="所有知识项均被排除"):
        packager.build([archived, superseded])

    package = packager.build(
        [archived, superseded], include_archived=True, include_superseded=True
    )
    assert len(package.items) == 2


def test_memory_archived_is_excluded_by_default() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    memory = _item(
        item_type=ContextItemType.KNOWLEDGE_MEMORY,
        stable_id=f"{KB_UUID}:knowledge_memory:9",
        status="archived",
        status_label="已归档",
        kind="experience",
        kind_label="经验",
    )

    with pytest.raises(KnowledgeContextError, match="所有知识项均被排除"):
        packager.build([memory])

    active = _item(
        item_type=ContextItemType.KNOWLEDGE_MEMORY,
        stable_id=f"{KB_UUID}:knowledge_memory:10",
        kind="experience",
        kind_label="经验",
    )
    package = packager.build([memory, active])
    assert [item.stable_id for item in package.items] == [active.stable_id]
    assert package.excluded[0].reason == "已归档（默认排除）"


def test_missing_source_is_marked_not_dropped() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    unsourced = _item(anchors=())

    package = packager.build([unsourced])

    assert len(package.items) == 1
    assert any("无来源" in warning.message for warning in package.warnings)
    assert "无来源" in package.to_markdown()


def test_changed_source_is_marked_with_warning() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    item = _item(
        anchors=(_anchor(fingerprint_state=ContextFingerprintState.CHANGED.value),)
    )

    package = packager.build([item])

    assert any("来源已变化" in warning.message for warning in package.warnings)


def test_content_truncation_and_total_char_limit() -> None:
    packager = KnowledgeContextPackager(
        kb_uuid=KB_UUID, max_chars_per_item=100, max_total_chars=300
    )
    item = _item(content="长" * 500)

    package = packager.build([item])
    assert len(package.items[0].content) <= 100 + len("\n（内容过长，已截断。）")
    assert "已截断" in package.items[0].content

    packager_tight = KnowledgeContextPackager(
        kb_uuid=KB_UUID, max_chars_per_item=100, max_total_chars=101
    )
    with pytest.raises(KnowledgeContextError, match="总字符数"):
        packager_tight.build([item])


def test_max_items_is_enforced() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID, max_items=2)

    with pytest.raises(KnowledgeContextError, match="上限"):
        packager.build([_item(local_id=index) for index in range(3)])


def test_packager_is_pure_and_never_mutates_items() -> None:
    packager = KnowledgeContextPackager(kb_uuid=KB_UUID)
    item = _item()
    snapshot = (item.stable_id, item.title, item.content, item.status)

    package = packager.build([item])

    assert (item.stable_id, item.title, item.content, item.status) == snapshot
    assert package.items[0] == item
    assert package.items[0].content == item.content
