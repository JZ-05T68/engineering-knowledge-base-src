"""Tests for the structured experience model (v0.5.3 Phase 4)."""

from __future__ import annotations

import json

import pytest

from src.ai.experience_model_service import (
    ExperienceModelError,
    ExperienceModelService,
)
from src.ai.experience_prompt_builder import ExperiencePromptBuilder
from src.ai.provider import AIExecutionError, AIUnavailableError, CompletionResult
from src.ai.rag_answer_service import MockCompletionProvider
from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackager,
)
from src.models import (
    ContextAnchorType,
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    ContextSourceAnchor,
)

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _sourced_item(local_id: int = 1) -> ContextItem:
    return ContextItem(
        type=ContextItemType.KNOWLEDGE_OBJECT,
        local_id=local_id,
        stable_id=f"{KB_UUID}:knowledge_object:{local_id}",
        title="编码器接线经验",
        content="A/B 相接反会导致 PID 震荡。",
        kind="experience",
        kind_label="经验",
        status="active",
        status_label="现行",
        importance="primary",
        updated_at=None,
        revision_ref="第 1 版",
        source_anchors=(
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.PAGE.value,
                anchor_id=7,
                anchor_label="页面 7",
                fingerprint_state=ContextFingerprintState.VALID.value,
            ),
        ),
        relation_refs=(),
    )


def _unsourced_item(local_id: int = 2) -> ContextItem:
    return ContextItem(
        type=ContextItemType.KNOWLEDGE_OBJECT,
        local_id=local_id,
        stable_id=f"{KB_UUID}:knowledge_object:{local_id}",
        title="无来源对象",
        content="没有可回源来源。",
        kind="fact",
        kind_label="事实",
        status="active",
        status_label="现行",
        importance=None,
        updated_at=None,
        revision_ref="第 1 版",
        source_anchors=(),
        relation_refs=(),
    )


def _package(*items: ContextItem):
    return KnowledgeContextPackager(kb_uuid=KB_UUID).build(list(items))


class _JsonProvider:
    def __init__(self, payload: str | Exception | None = None) -> None:
        self._payload = payload
        self.prompts: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        self.prompts.append(prompt)
        if isinstance(self._payload, Exception):
            raise self._payload
        text = self._payload if self._payload is not None else json.dumps(
            {
                "title": "编码器接线错误",
                "problem": "PID 震荡",
                "context": "正交编码器",
                "action": "核对 A/B 相",
                "result": "恢复稳定",
                "root_cause": "A/B 相接反",
                "lesson": "先核对相序",
                "applicability": "增量编码器",
                "limitations": "仅适用正交编码器",
                "citations": [f"{KB_UUID}:knowledge_object:1"],
            },
            ensure_ascii=False,
        )
        return CompletionResult(text=text, model="fake-1")


def _mock_service() -> ExperienceModelService:
    return ExperienceModelService(MockCompletionProvider())


# --- ExperiencePromptBuilder --------------------------------------------------


def test_builder_includes_package_and_never_leaks_unselected_context() -> None:
    package = _package(_sourced_item(1))
    prompt = ExperiencePromptBuilder().build("总结编码器经验", package)

    assert "总结编码器经验" in prompt
    assert package.to_markdown() in prompt
    assert f"{KB_UUID}:knowledge_object:1" in prompt
    assert "stable_id" in prompt
    assert "api_key" not in prompt.lower()
    assert "data/database" not in prompt
    assert f"{KB_UUID}:knowledge_object:999" not in prompt


def test_builder_rejects_empty_task_and_empty_context() -> None:
    with pytest.raises(KnowledgeContextError, match="空任务"):
        ExperiencePromptBuilder().build("   ", _package(_sourced_item()))
    with pytest.raises(KnowledgeContextError, match="空上下文"):
        ExperiencePromptBuilder().build("任务", _package())


def test_builder_rejects_all_unsourced_context() -> None:
    with pytest.raises(KnowledgeContextError, match="无来源上下文"):
        ExperiencePromptBuilder().build("任务", _package(_unsourced_item()))


def test_builder_keeps_partial_unsourced_with_warning_text() -> None:
    package = _package(_sourced_item(), _unsourced_item())

    prompt = ExperiencePromptBuilder().build("任务", package)

    assert "无来源" in prompt


# --- ExperienceModelService ---------------------------------------------------


def test_normal_structured_output_is_parsed() -> None:
    package = _package(_sourced_item())

    output = ExperienceModelService(_JsonProvider()).generate("任务", package)

    candidate = output.candidate
    assert candidate.title == "编码器接线错误"
    assert candidate.problem == "PID 震荡"
    assert candidate.root_cause == "A/B 相接反"
    assert candidate.lesson == "先核对相序"
    assert candidate.limitations == "仅适用正交编码器"
    assert candidate.citations == (f"{KB_UUID}:knowledge_object:1",)
    assert output.provider == "qwen"
    assert output.is_mock is False
    assert output.audit_call_id is None


def test_missing_provider_is_rejected() -> None:
    with pytest.raises(AIUnavailableError):
        ExperienceModelService(None).generate("任务", _package(_sourced_item()))


def test_provider_exception_propagates() -> None:
    provider = _JsonProvider(AIExecutionError("boom"))

    with pytest.raises(AIExecutionError):
        ExperienceModelService(provider).generate("任务", _package(_sourced_item()))


def test_invalid_json_is_rejected() -> None:
    provider = _JsonProvider("这不是 JSON")

    with pytest.raises(ExperienceModelError, match="不是可解析的 JSON"):
        ExperienceModelService(provider).generate("任务", _package(_sourced_item()))


def test_missing_title_is_rejected() -> None:
    provider = _JsonProvider(json.dumps({"problem": "没有标题"}))

    with pytest.raises(ExperienceModelError, match="title"):
        ExperienceModelService(provider).generate("任务", _package(_sourced_item()))


def test_valid_citations_are_kept() -> None:
    payload = json.dumps(
        {"title": "经验", "citations": [f"{KB_UUID}:knowledge_object:1"]},
        ensure_ascii=False,
    )
    output = ExperienceModelService(_JsonProvider(payload)).generate(
        "任务", _package(_sourced_item())
    )

    assert output.candidate.citations == (f"{KB_UUID}:knowledge_object:1",)


def test_unknown_citation_is_rejected() -> None:
    payload = json.dumps(
        {"title": "经验", "citations": [f"{KB_UUID}:knowledge_object:999"]},
        ensure_ascii=False,
    )

    with pytest.raises(ExperienceModelError, match="未知或非法"):
        ExperienceModelService(_JsonProvider(payload)).generate(
            "任务", _package(_sourced_item())
        )


def test_all_illegal_citations_are_rejected() -> None:
    payload = json.dumps(
        {"title": "经验", "citations": ["不是 stable_id", f"{KB_UUID}:page:404"]},
        ensure_ascii=False,
    )

    with pytest.raises(ExperienceModelError, match="未知或非法"):
        ExperienceModelService(_JsonProvider(payload)).generate(
            "任务", _package(_sourced_item())
        )


def test_empty_citations_are_rejected() -> None:
    payload = json.dumps({"title": "经验", "citations": []}, ensure_ascii=False)

    with pytest.raises(ExperienceModelError, match="citation"):
        ExperienceModelService(_JsonProvider(payload)).generate(
            "任务", _package(_sourced_item())
        )


def test_duplicate_citations_are_deduplicated_in_order() -> None:
    payload = json.dumps(
        {
            "title": "经验",
            "citations": [
                f"{KB_UUID}:knowledge_object:2",
                f"{KB_UUID}:knowledge_object:1",
                f"{KB_UUID}:knowledge_object:2",
            ],
        },
        ensure_ascii=False,
    )
    output = ExperienceModelService(_JsonProvider(payload)).generate(
        "任务", _package(_sourced_item(1), _sourced_item(2))
    )

    assert output.candidate.citations == (
        f"{KB_UUID}:knowledge_object:2",
        f"{KB_UUID}:knowledge_object:1",
    )


def test_mock_output_is_explicitly_labelled_and_deterministic() -> None:
    package = _package(_sourced_item())

    first = _mock_service().generate("任务", package)
    second = _mock_service().generate("任务", package)

    assert first.is_mock is True
    assert first.provider == "mock"
    assert any("离线演示生成" in warning for warning in first.warnings)
    assert first.candidate.citations == (f"{KB_UUID}:knowledge_object:1",)
    assert first.candidate == second.candidate


def test_real_call_records_experience_audit_metadata() -> None:
    from src.ai.provider import AuditedAIProvider

    class _Ledger:
        def __init__(self) -> None:
            self.records = []

        def record(self, call) -> None:
            self.records.append(call)

    ledger = _Ledger()
    provider = AuditedAIProvider(
        _JsonProvider(),
        default_model="qwen3.7-plus",
        default_embedding_model="qwen3.7-text-embedding",
        source_feature="application",
        ledger=ledger,
    )
    output = ExperienceModelService(provider).generate(
        "任务", _package(_sourced_item())
    )

    assert output.provider == "qwen"
    assert ledger.records
    record = ledger.records[0]
    assert record.source_feature == "experience_model"
    assert record.target_refs == (f"{KB_UUID}:knowledge_object:1",)


def test_service_never_writes_the_database(tmp_path) -> None:

    from src.database import Database

    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="手册",
        filename="manual.pdf",
        source_path=tmp_path / "manual.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page-1.png",
        extracted_text="编码器 A/B 相",
    )
    knowledge_object = database.create_knowledge_object(
        kind="experience", title="编码器经验", content="A/B 相接反。"
    )
    from src.knowledge_object_service import KnowledgeObjectService

    KnowledgeObjectService(database).link_source(
        knowledge_object.id, source_type="page", source_id=page.id
    )
    from src.knowledge_context import ContextItemProjector

    item = ContextItemProjector(database).project_knowledge_object(
        knowledge_object.id
    )
    package = KnowledgeContextPackager(
        kb_uuid=database.get_knowledge_base_uuid()
    ).build([item])
    before = database.database_path.read_bytes()

    _mock_service().generate("任务", package)

    assert database.database_path.read_bytes() == before
