"""Contract tests for the vendor-neutral AI provider boundary."""

from __future__ import annotations

import dataclasses

import pytest

import src.prompt_builder as prompt_builder
from src.ai.provider import (
    AiCallRecord,
    AIError,
    AIExecutionError,
    AIProductionCompositionError,
    AIProvider,
    AIUnavailableError,
    AuditedAIProvider,
    CompletionProvider,
    CompletionResult,
    CompletionUsage,
    EmbeddingProvider,
    EmbeddingResult,
    RerankHit,
    RerankProvider,
    RerankResult,
    build_production_audited_provider,
    require_ai_provider,
    require_production_audited_provider,
)


class _FakeCompletion:
    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        return CompletionResult(text=f"echo:{prompt}", model=model or "fake-model")


class _FakeEmbedding:
    def embed(self, texts, *, model=None, dimensions=None) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=tuple((1.0,) for _ in texts), model=model or "fake-embedding"
        )


class _FakeRerank:
    def rerank(self, query, documents, *, model=None, top_n=None) -> RerankResult:
        return RerankResult(
            hits=tuple(RerankHit(index=i, relevance_score=1.0) for i in range(len(documents))),
            model=model or "fake-rerank",
        )


class _FakeLegacy:
    def generate(self, prompt: str) -> str:
        return prompt


class _CountingCompletion(_FakeCompletion):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt, *, model=None, max_completion_tokens=None):
        self.calls += 1
        return super().complete(
            prompt, model=model, max_completion_tokens=max_completion_tokens
        )


class _RecordingLedger:
    def __init__(self) -> None:
        self.records: list[AiCallRecord] = []

    def record(self, call: AiCallRecord) -> None:
        self.records.append(call)


class _AllowBudgetGuard:
    def __init__(self) -> None:
        self.capabilities: list[str] = []

    def ensure_allowed(self, capability: str) -> None:
        self.capabilities.append(capability)


def test_exception_hierarchy_separates_unavailable_from_execution() -> None:
    assert issubclass(AIUnavailableError, AIError)
    assert issubclass(AIExecutionError, AIError)
    assert issubclass(AIError, RuntimeError)
    assert not issubclass(AIUnavailableError, AIExecutionError)
    assert not issubclass(AIExecutionError, AIUnavailableError)


def test_capability_protocols_are_runtime_checkable() -> None:
    assert isinstance(_FakeCompletion(), CompletionProvider)
    assert isinstance(_FakeEmbedding(), EmbeddingProvider)
    assert isinstance(_FakeRerank(), RerankProvider)
    assert not isinstance(_FakeEmbedding(), CompletionProvider)
    assert not isinstance(object(), CompletionProvider)


def test_result_dataclasses_are_frozen() -> None:
    usage = CompletionUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8)
    result = CompletionResult(text="答案", model="qwen3.7-plus", usage=usage)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.text = "改写"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        usage.total_tokens = 0  # type: ignore[misc]
    embedding = EmbeddingResult(embeddings=((0.1, 0.2),), model="qwen3.7-text-embedding")
    with pytest.raises(dataclasses.FrozenInstanceError):
        embedding.model = "other"  # type: ignore[misc]
    hit = RerankHit(index=0, relevance_score=0.5)
    rerank = RerankResult(hits=(hit,), model="qwen3-rerank")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rerank.hits = ()  # type: ignore[misc]


def test_completion_result_keeps_model_echo_and_optional_usage() -> None:
    with_usage = _FakeCompletion().complete("问题")
    assert with_usage.text == "echo:问题"
    assert with_usage.model == "fake-model"
    assert with_usage.usage is None

    usage = CompletionUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    result = CompletionResult(text="t", model="m", usage=usage)
    assert result.usage == usage


def test_legacy_ai_provider_moved_to_single_authoritative_definition() -> None:
    """prompt_builder re-exports the contract; there is exactly one definition."""

    assert prompt_builder.AIProvider is AIProvider
    assert "AIProvider" in prompt_builder.__all__
    assert isinstance(_FakeLegacy(), AIProvider)


def test_require_ai_provider_turns_none_into_typed_unavailable() -> None:
    with pytest.raises(AIUnavailableError, match="手动模式"):
        require_ai_provider(None)

    provider = _FakeCompletion()
    assert require_ai_provider(provider) is provider


def test_audited_provider_has_no_public_wrapped_escape_hatch() -> None:
    provider = AuditedAIProvider(
        _FakeCompletion(),
        default_model="fake-model",
        default_embedding_model="fake-embedding",
        source_feature="test",
    )

    for name in ("wrapped", "raw", "inner", "transport", "provider", "underlying"):
        assert not hasattr(provider, name)


def test_production_boundary_rejects_raw_provider_before_transport() -> None:
    raw = _CountingCompletion()

    with pytest.raises(AIProductionCompositionError):
        require_production_audited_provider(raw)

    assert raw.calls == 0


def test_production_boundary_rejects_unapproved_or_incomplete_wrapper() -> None:
    low_level = AuditedAIProvider(
        _FakeCompletion(),
        default_model="fake-model",
        default_embedding_model="fake-embedding",
        source_feature="test",
    )

    with pytest.raises(AIProductionCompositionError):
        require_production_audited_provider(low_level)
    with pytest.raises(AIProductionCompositionError):
        build_production_audited_provider(
            _FakeCompletion(),
            default_model="fake-model",
            default_embedding_model="fake-embedding",
            source_feature="test",
            ledger=None,  # type: ignore[arg-type]
            budget_guard=_AllowBudgetGuard(),
        )


def test_production_builder_preserves_audit_budget_and_transport_order() -> None:
    raw = _CountingCompletion()
    ledger = _RecordingLedger()
    budget = _AllowBudgetGuard()
    provider = build_production_audited_provider(
        raw,
        default_model="fake-model",
        default_embedding_model="fake-embedding",
        source_feature="composition_test",
        ledger=ledger,
        budget_guard=budget,
    )

    assert require_production_audited_provider(provider) is provider
    result = provider.complete("test prompt")

    assert result.text == "echo:test prompt"
    assert raw.calls == 1
    assert budget.capabilities == ["completion"]
    assert len(ledger.records) == 1
    assert ledger.records[0].source_feature == "composition_test"
