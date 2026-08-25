"""Schema-audit tests: v12 ai_calls accurately expresses every AI call kind."""

from __future__ import annotations

import json
from pathlib import Path

from src.ai.experience_model_service import ExperienceModelService
from src.ai.provider import (
    AiCallRecord,
    AuditedAIProvider,
    CompletionResult,
    EmbeddingResult,
)
from src.ai.rag_answer_service import MockCompletionProvider, RagAnswerService
from src.database import Database
from src.knowledge_context_packager import KnowledgeContextPackager
from src.models import (
    ContextAnchorType,
    ContextFingerprintState,
    ContextItem,
    ContextItemType,
    ContextSourceAnchor,
)

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _item(local_id: int = 1) -> ContextItem:
    return ContextItem(
        type=ContextItemType.KNOWLEDGE_OBJECT,
        local_id=local_id,
        stable_id=f"{KB_UUID}:knowledge_object:{local_id}",
        title=f"知识对象 {local_id}",
        content="正文内容",
        kind="fact",
        kind_label="事实",
        status="active",
        status_label="现行",
        importance=None,
        updated_at=None,
        revision_ref="第 1 版",
        source_anchors=(
            ContextSourceAnchor(
                anchor_type=ContextAnchorType.PAGE.value,
                anchor_id=1,
                anchor_label="页面 1",
                fingerprint_state=ContextFingerprintState.VALID.value,
            ),
        ),
        relation_refs=(),
    )


def _package(*items: ContextItem):
    return KnowledgeContextPackager(kb_uuid=KB_UUID).build(list(items))


class _DbLedger:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, call: AiCallRecord) -> None:
        self._database.insert_ai_call(call)


class _TextProvider:
    def complete(self, prompt, *, model=None, max_completion_tokens=None):
        return CompletionResult(text="结论见【来源 #1】。", model="qwen3.7-plus")


class _JsonProvider:
    def complete(self, prompt, *, model=None, max_completion_tokens=None):
        return CompletionResult(
            text=json.dumps(
                {"title": "经验", "citations": [f"{KB_UUID}:knowledge_object:1"]},
                ensure_ascii=False,
            ),
            model="qwen3.7-plus",
        )


class _EmbeddingProvider:
    def embed(self, texts, *, model=None, dimensions=None):
        return EmbeddingResult(
            embeddings=tuple((0.1,) * 8 for _ in texts),
            model="qwen3.7-text-embedding",
        )


def _audited(database: Database, inner: object) -> AuditedAIProvider:
    return AuditedAIProvider(
        inner,
        default_model="qwen3.7-plus",
        default_embedding_model="qwen3.7-text-embedding",
        source_feature="application",
        ledger=_DbLedger(database),
    )


def test_rag_answer_real_call_is_completion_with_rag_feature(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    package = _package(_item(1))
    RagAnswerService(_audited(database, _TextProvider())).answer("问题", package)

    rows = database.list_ai_calls()
    assert len(rows) == 1
    assert rows[0].capability == "completion"
    assert rows[0].source_feature == "rag_answer"
    assert rows[0].status == "success"


def test_experience_model_real_call_is_completion_with_experience_feature(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    package = _package(_item(1))
    ExperienceModelService(_audited(database, _JsonProvider())).generate(
        "任务", package
    )

    rows = database.list_ai_calls()
    assert len(rows) == 1
    assert rows[0].capability == "completion"
    assert rows[0].source_feature == "experience_model"
    assert rows[0].status == "success"


def test_embedding_call_classification_is_unchanged(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    provider = _audited(database, _EmbeddingProvider())
    provider.embed(["文本"], source_feature="page_index")

    rows = database.list_ai_calls()
    assert len(rows) == 1
    assert rows[0].capability == "embedding"
    assert rows[0].source_feature == "page_index"


def test_target_refs_preserve_order_and_deduplication(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    package = _package(_item(1), _item(2))
    RagAnswerService(_audited(database, _TextProvider())).answer("问题", package)

    row = database.list_ai_calls()[0]
    assert row.target_refs == (
        f"{KB_UUID}:knowledge_object:1",
        f"{KB_UUID}:knowledge_object:2",
    )


def test_mock_provider_never_writes_the_real_ledger(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    package = _package(_item(1))
    RagAnswerService(MockCompletionProvider()).answer("问题", package)
    ExperienceModelService(MockCompletionProvider()).generate("任务", package)

    assert database.list_ai_calls() == []


def test_success_and_error_status_are_distinguishable(tmp_path: Path) -> None:
    from src.ai.provider import AIExecutionError

    class _Failing:
        def complete(self, prompt, *, model=None, max_completion_tokens=None):
            raise AIExecutionError("执行失败")

    database = Database(tmp_path / "knowledge.db")
    RagAnswerService(_audited(database, _TextProvider())).answer(
        "问题", _package(_item(1))
    )
    try:
        RagAnswerService(_audited(database, _Failing())).answer(
            "问题", _package(_item(1))
        )
    except AIExecutionError:
        pass

    rows = database.list_ai_calls()
    assert sorted(row.status for row in rows) == ["error", "success"]


def test_ledger_stores_no_prompt_context_answer_or_key(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    RagAnswerService(_audited(database, _TextProvider())).answer(
        "问题", _package(_item(1))
    )

    row = database.list_ai_calls()[0]
    assert row.prompt_sha256 != ""
    assert row.prompt_sha256 != "问题"
    for value in (
        row.prompt_sha256,
        row.target_refs,
        row.error_class,
        row.model,
        row.source_feature,
    ):
        assert "sk-" not in str(value)
        assert "api_key" not in str(value).lower()
