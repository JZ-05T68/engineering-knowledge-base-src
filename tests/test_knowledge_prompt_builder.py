"""Tests for the v0.5.2 knowledge prompt builder (Phase 2B)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.knowledge_prompt_builder import KnowledgePromptBuilder, KnowledgePromptError
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
    KnowledgeSourceStatus,
    NoteImportance,
)

TS = datetime(2026, 8, 1, tzinfo=UTC)


def _object(
    *,
    ko_id: int = 1,
    title: str = "PID 参数经验",
    content: str = "采样频率必须与 PID 参数匹配。",
    confirmation_status: KnowledgeConfirmationStatus = KnowledgeConfirmationStatus.CONFIRMED,
    confirmed_revision: int = 1,
    current_revision: int = 1,
    lifecycle: KnowledgeLifecycle = KnowledgeLifecycle.ACTIVE,
) -> KnowledgeObject:
    return KnowledgeObject(
        id=ko_id,
        kind=KnowledgeObjectKind.EXPERIENCE,
        authorship=KnowledgeAuthorship.USER,
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
        title=title,
        content=content,
        importance=NoteImportance.PRIMARY,
        lifecycle=lifecycle,
        superseded_by_ko_id=None,
        confirmation_status=confirmation_status,
        confirmed_at=TS if confirmation_status is KnowledgeConfirmationStatus.CONFIRMED else None,
        confirmed_revision=(
            confirmed_revision
            if confirmation_status is KnowledgeConfirmationStatus.CONFIRMED
            else None
        ),
        current_revision=current_revision,
        created_at=TS,
        updated_at=TS,
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
    confirmation_status: KnowledgeConfirmationStatus = KnowledgeConfirmationStatus.CONFIRMED,
    current_revision: int = 1,
    lifecycle: KnowledgeLifecycle = KnowledgeLifecycle.ACTIVE,
) -> KnowledgeObjectView:
    return KnowledgeObjectView(
        knowledge_object=_object(
            confirmation_status=confirmation_status,
            current_revision=current_revision,
            lifecycle=lifecycle,
        ),
        sources=sources,
        outgoing_relations=(),
        incoming_relations=(),
    )


def test_build_contains_grounding_rules_question_and_object() -> None:
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
    assert "确认状态：已确认" in prompt
    assert "形成依据：个人经历" in prompt


def test_build_empty_views_rejected() -> None:
    with pytest.raises(KnowledgePromptError, match="没有可生成"):
        KnowledgePromptBuilder().build("问题", [])


def test_build_default_question_when_blank() -> None:
    prompt = KnowledgePromptBuilder().build("  ", [_view()])
    assert "请概括知识片段中的相关信息。" in prompt


def test_unconfirmed_object_carries_warning() -> None:
    prompt = KnowledgePromptBuilder().build(
        "问题", [_view(confirmation_status=KnowledgeConfirmationStatus.UNCONFIRMED)]
    )
    assert "尚未经用户确认" in prompt
    assert "不应视为已确认事实" in prompt


def test_stale_confirmation_carries_warning() -> None:
    prompt = KnowledgePromptBuilder().build(
        "问题",
        [_view(confirmation_status=KnowledgeConfirmationStatus.CONFIRMED, current_revision=3)],
    )
    assert "确认之后又被修改" in prompt


def test_non_active_object_carries_history_notice() -> None:
    prompt = KnowledgePromptBuilder().build(
        "问题", [_view(lifecycle=KnowledgeLifecycle.ARCHIVED)]
    )
    assert "仅作历史参考" in prompt


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
