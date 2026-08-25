"""Tests for the vendor-neutral AI call ledger wrapper (v0.5.3 Phase 2-A)."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.ai.provider import (
    AiCallRecord,
    AIExecutionError,
    AIUnavailableError,
    AuditedAIProvider,
    CompletionResult,
    CompletionUsage,
    EmbeddingResult,
    EmbeddingUsage,
)


class _RecordingLedger:
    def __init__(self) -> None:
        self.records: list[AiCallRecord] = []

    def record(self, call: AiCallRecord) -> None:
        self.records.append(call)


class _RaisingLedger:
    def record(self, call: AiCallRecord) -> None:
        raise RuntimeError("ledger down")


class _DenyBudgetGuard:
    def ensure_allowed(self, capability: str) -> None:
        raise AIUnavailableError("AI 调用被预算限制拒绝")


class _FakeProvider:
    def __init__(
        self,
        completion: CompletionResult | Exception | None = None,
        embedding: EmbeddingResult | Exception | None = None,
    ) -> None:
        self._completion: CompletionResult | Exception = (
            completion
            if completion is not None
            else CompletionResult(
                text="回答",
                model="qwen3.7-plus",
                usage=CompletionUsage(10, 5, 15),
                finish_reason="stop",
            )
        )
        self._embedding: EmbeddingResult | Exception = (
            embedding
            if embedding is not None
            else EmbeddingResult(
                embeddings=((0.1, 0.2),),
                model="qwen3.7-text-embedding",
                usage=EmbeddingUsage(7, 7),
            )
        )
        self.completion_calls: list[tuple[str, str | None, int | None]] = []
        self.embedding_calls: list[tuple[list[str], str | None, int | None]] = []

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        self.completion_calls.append((prompt, model, max_completion_tokens))
        if isinstance(self._completion, Exception):
            raise self._completion
        return self._completion

    def embed(
        self,
        texts,
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        self.embedding_calls.append((list(texts), model, dimensions))
        if isinstance(self._embedding, Exception):
            raise self._embedding
        return self._embedding


def _wrapper(provider: _FakeProvider, ledger=None, budget_guard=None) -> AuditedAIProvider:
    return AuditedAIProvider(
        provider,
        default_model="qwen3.7-plus",
        default_embedding_model="qwen3.7-text-embedding",
        source_feature="unit_test",
        target_refs=("kb:page:1",),
        ledger=ledger,
        budget_guard=budget_guard,
    )


def test_complete_success_records_full_audit_row() -> None:
    ledger = _RecordingLedger()
    provider = _FakeProvider()
    wrapper = _wrapper(provider, ledger=ledger)

    result = wrapper.complete("什么是 PID 控制？")

    assert result.text == "回答"
    assert len(ledger.records) == 1
    record = ledger.records[0]
    assert record.call_uuid
    assert record.capability == "completion"
    assert record.model == "qwen3.7-plus"
    assert record.prompt_sha256 == hashlib.sha256(
        "什么是 PID 控制？".encode()
    ).hexdigest()
    assert record.input_chars == len("什么是 PID 控制？")
    assert record.status == "success"
    assert record.error_class is None
    assert record.retry_count == 0
    assert record.latency_ms is not None
    assert record.prompt_tokens == 10
    assert record.completion_tokens == 5
    assert record.total_tokens == 15
    assert record.finish_reason == "stop"
    assert record.source_feature == "unit_test"
    assert record.target_refs == ("kb:page:1",)
    assert record.created_at


def test_complete_records_requested_model_override() -> None:
    ledger = _RecordingLedger()
    wrapper = _wrapper(_FakeProvider(), ledger=ledger)

    wrapper.complete("hi", model="qwen3.8-max")

    assert ledger.records[0].model == "qwen3.8-max"


def test_complete_error_records_error_class_and_retry_count() -> None:
    ledger = _RecordingLedger()
    provider = _FakeProvider(
        completion=AIExecutionError(
            "rate limited", error_class="http_429", retry_count=2
        )
    )
    wrapper = _wrapper(provider, ledger=ledger)

    with pytest.raises(AIExecutionError) as captured:
        wrapper.complete("hi")

    assert captured.value.error_class == "http_429"
    assert captured.value.retry_count == 2
    record = ledger.records[0]
    assert record.status == "error"
    assert record.error_class == "http_429"
    assert record.retry_count == 2
    assert record.latency_ms is not None


def test_complete_unavailable_records_error_class() -> None:
    ledger = _RecordingLedger()
    provider = _FakeProvider(completion=AIUnavailableError("未配置 API Key"))
    wrapper = _wrapper(provider, ledger=ledger)

    with pytest.raises(AIUnavailableError):
        wrapper.complete("hi")

    assert ledger.records[0].status == "error"
    assert ledger.records[0].error_class == "unavailable"


def test_embed_success_records_embedding_usage() -> None:
    ledger = _RecordingLedger()
    wrapper = _wrapper(_FakeProvider(), ledger=ledger)

    result = wrapper.embed(["第一", "第二"])

    assert result.embeddings == ((0.1, 0.2),)
    record = ledger.records[0]
    assert record.capability == "embedding"
    assert record.model == "qwen3.7-text-embedding"
    assert record.prompt_sha256 == hashlib.sha256(
        json.dumps(["第一", "第二"], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert record.input_chars == len("第一") + len("第二")
    assert record.status == "success"
    assert record.prompt_tokens == 7
    assert record.total_tokens == 7
    assert record.completion_tokens is None


def test_budget_rejection_records_rejected_and_never_calls_provider() -> None:
    ledger = _RecordingLedger()
    provider = _FakeProvider()
    wrapper = _wrapper(provider, ledger=ledger, budget_guard=_DenyBudgetGuard())

    with pytest.raises(AIUnavailableError, match="预算"):
        wrapper.complete("hi")

    assert provider.completion_calls == []
    record = ledger.records[0]
    assert record.status == "rejected"
    assert record.error_class == "budget"
    assert record.prompt_sha256 == hashlib.sha256(b"hi").hexdigest()


def test_budget_rejection_applies_to_embedding_too() -> None:
    ledger = _RecordingLedger()
    provider = _FakeProvider()
    wrapper = _wrapper(provider, ledger=ledger, budget_guard=_DenyBudgetGuard())

    with pytest.raises(AIUnavailableError, match="预算"):
        wrapper.embed(["文本"])

    assert provider.embedding_calls == []
    assert ledger.records[0].status == "rejected"


def test_ledger_failure_never_breaks_the_ai_result() -> None:
    wrapper = _wrapper(_FakeProvider(), ledger=_RaisingLedger())

    result = wrapper.complete("hi")

    assert result.text == "回答"


def test_null_ledger_and_guard_defaults_allow_calls() -> None:
    wrapper = _wrapper(_FakeProvider())

    assert wrapper.complete("hi").text == "回答"
    assert wrapper.embed(["文本"]).embeddings == ((0.1, 0.2),)


def test_wrapper_rejects_empty_construction_arguments() -> None:
    provider = _FakeProvider()
    with pytest.raises(ValueError, match="default_model"):
        AuditedAIProvider(
            provider,
            default_model="",
            default_embedding_model="qwen3.7-text-embedding",
            source_feature="unit_test",
        )
    with pytest.raises(ValueError, match="source_feature"):
        AuditedAIProvider(
            provider,
            default_model="qwen3.7-plus",
            default_embedding_model="qwen3.7-text-embedding",
            source_feature=" ",
        )
