"""Tests for the v0.5.2 knowledge prompt builder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.knowledge_prompt_builder import KnowledgePromptBuilder, KnowledgePromptError
from src.models import (
    KnowledgeObject,
    KnowledgeObjectKind,
    KnowledgeObjectSource,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeObjectStatus,
    KnowledgeObjectView,
    KnowledgeSourceStatus,
    NoteImportance,
)

TS = datetime(2026, 8, 1, tzinfo=UTC)


def _object(
    *,
    ko_id: int = 1,
    title: str = "PID 参数经验",
    content: str = "采样频率必须与 PID 参数匹配。",
    status: KnowledgeObjectStatus = KnowledgeObjectStatus.REVIEWED,
) -> KnowledgeObject:
    return KnowledgeObject(
        id=ko_id,
        kind=KnowledgeObjectKind.EXPERIENCE,
        title=title,
        content=content,
        importance=NoteImportance.PRIMARY,
        status=status,
        created_at=TS,
        updated_at=TS,
        reviewed_at=TS if status is KnowledgeObjectStatus.REVIEWED else None,
    )


def _source(
    *,
    source_id: int = 1,
    source_type: KnowledgeObjectSourceType = KnowledgeObjectSourceType.PAGE,
    source_note: str = "关键页",
) -> KnowledgeObjectSource:
    return KnowledgeObjectSource(
        id=1,
        knowledge_object_id=1,
        source_type=source_type,
        source_id=source_id,
        source_note=source_note,
        created_at=TS,
    )


def _view(
    *,
    sources: tuple[KnowledgeObjectSourceView, ...] = (),
    status: KnowledgeObjectStatus = KnowledgeObjectStatus.REVIEWED,
) -> KnowledgeObjectView:
    return KnowledgeObjectView(
        knowledge_object=_object(status=status),
        sources=sources,
        outgoing_relations=(),
        incoming_relations=(),
    )


def test_build_contains_grounding_rules_question_and_object(
) -> None:
    source = _source()
    view = KnowledgeObjectView(
        knowledge_object=_object(),
        sources=(KnowledgeObjectSourceView(source, KnowledgeSourceStatus.VALID),),
        outgoing_relations=(),
        incoming_relations=(),
    )

    prompt = KnowledgePromptBuilder().build(
        "如何避免底盘抖动？",
        [view],
        source_texts={("page", 1): "页面文本：采样频率影响抖动。"},
        generated_at=TS,
    )

    assert "# 任务" in prompt
    assert "如何避免底盘抖动？" in prompt
    assert "只能根据" in prompt
    assert "[知识对象 1]" in prompt
    assert "PID 参数经验" in prompt
    assert "经验" in prompt
    assert "来源：" in prompt
    assert "页面 1（关键页）" in prompt
    assert "页面文本：采样频率影响抖动。" in prompt


def test_build_empty_views_rejected() -> None:
    with pytest.raises(KnowledgePromptError, match="没有可生成"):
        KnowledgePromptBuilder().build("问题", [])


def test_build_default_question_when_blank() -> None:
    prompt = KnowledgePromptBuilder().build("  ", [_view()])
    assert "请概括知识片段中的相关信息。" in prompt


def test_draft_object_carries_warning() -> None:
    prompt = KnowledgePromptBuilder().build(
        "问题", [_view(status=KnowledgeObjectStatus.DRAFT)]
    )
    assert "草稿" in prompt
    assert "不应视为已确认事实" in prompt


def test_missing_source_text_notice() -> None:
    prompt = KnowledgePromptBuilder().build("问题", [_view()])
    assert "没有可用的有效来源" in prompt


def test_source_text_truncation() -> None:
    long_text = "字" * 25_000
    source = _source()
    view = KnowledgeObjectView(
        knowledge_object=_object(),
        sources=(KnowledgeObjectSourceView(source, KnowledgeSourceStatus.VALID),),
        outgoing_relations=(),
        incoming_relations=(),
    )
    prompt = KnowledgePromptBuilder().build(
        "问题", [view], source_texts={("page", 1): long_text}
    )
    assert "仅保留前 20000 个字符" in prompt
