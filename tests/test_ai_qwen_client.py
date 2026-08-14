"""Tests for the Qwen adapter skeleton. All transports are fake; no network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.ai.provider import AIExecutionError, AIUnavailableError
from src.ai.qwen_client import (
    DEFAULT_BASE_URL,
    QwenProvider,
    QwenTransportError,
)

SECRET = "sk-unit-test-only-never-real"


class FakeTransport:
    """Recording transport stub: queued responses or queued failures."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any], float]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append((url, headers, payload, timeout_seconds))
        outcome = self._outcomes.pop(0) if self._outcomes else {"choices": []}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion_body(text: str = "回答", model: str = "qwen3.7-plus") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _provider(transport: FakeTransport, **overrides: Any) -> QwenProvider:
    options: dict[str, Any] = {
        "api_key": SECRET,
        "llm_model": "qwen3.7-plus",
        "llm_model_hard": "qwen3.8-max",
        "embedding_model": "qwen3.7-text-embedding",
        "rerank_model": "qwen3-rerank",
        "timeout_seconds": 12.5,
        "transport": transport,
    }
    options.update(overrides)
    return QwenProvider(**options)


def test_complete_builds_openai_compatible_request() -> None:
    transport = FakeTransport([_completion_body()])
    provider = _provider(transport)

    result = provider.complete("什么是 PID 控制？")

    assert len(transport.calls) == 1
    url, headers, payload, timeout = transport.calls[0]
    assert url == f"{DEFAULT_BASE_URL}/chat/completions"
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["Content-Type"] == "application/json"
    assert payload == {
        "model": "qwen3.7-plus",
        "messages": [{"role": "user", "content": "什么是 PID 控制？"}],
        "enable_thinking": False,
        "stream": False,
    }
    assert timeout == 12.5
    assert result.text == "回答"


def test_complete_parses_model_echo_and_usage() -> None:
    transport = FakeTransport([_completion_body(text="你好", model="qwen3.7-plus-2026")])
    result = _provider(transport).complete("hi")

    assert result.text == "你好"
    assert result.model == "qwen3.7-plus-2026"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 15


def test_complete_uses_requested_model_override_and_falls_back_model_echo() -> None:
    body = _completion_body()
    del body["model"]
    transport = FakeTransport([body])

    result = _provider(transport).complete("难", model="qwen3.8-max")

    assert transport.calls[0][2]["model"] == "qwen3.8-max"
    assert result.model == "qwen3.8-max"


def test_complete_tolerates_missing_usage() -> None:
    body = _completion_body()
    del body["usage"]
    result = _provider(FakeTransport([body])).complete("hi")

    assert result.usage is None


def test_malformed_completion_response_fails_without_retry() -> None:
    transport = FakeTransport([{"unexpected": True}])

    with pytest.raises(AIExecutionError, match="响应解析失败"):
        _provider(transport).complete("hi")

    assert len(transport.calls) == 1


def test_malformed_usage_fails_without_retry() -> None:
    body = _completion_body()
    body["usage"] = {"prompt_tokens": 1}
    transport = FakeTransport([body])

    with pytest.raises(AIExecutionError, match="usage"):
        _provider(transport).complete("hi")

    assert len(transport.calls) == 1


def test_429_is_retried_once_then_succeeds() -> None:
    transport = FakeTransport(
        [QwenTransportError("rate limited", status_code=429), _completion_body()]
    )

    result = _provider(transport).complete("hi")

    assert result.text == "回答"
    assert len(transport.calls) == 2


def test_persistent_429_exhausts_bounded_retry() -> None:
    transport = FakeTransport(
        [QwenTransportError("rate limited", status_code=429)] * 10
    )

    with pytest.raises(AIExecutionError, match="429"):
        _provider(transport).complete("hi")

    # 1 initial attempt + 2 extra attempts, never more.
    assert len(transport.calls) == 3


def test_5xx_and_network_failures_are_retried() -> None:
    transport = FakeTransport(
        [
            QwenTransportError("server error", status_code=500),
            QwenTransportError("connection reset", status_code=None),
            _completion_body(),
        ]
    )

    result = _provider(transport).complete("hi")

    assert result.text == "回答"
    assert len(transport.calls) == 3


def test_client_error_400_is_never_retried() -> None:
    transport = FakeTransport([QwenTransportError("bad request", status_code=400)] * 5)

    with pytest.raises(AIExecutionError, match="400"):
        _provider(transport).complete("hi")

    assert len(transport.calls) == 1


def test_zero_extra_attempts_means_single_call() -> None:
    transport = FakeTransport([QwenTransportError("boom", status_code=503)] * 5)

    with pytest.raises(AIExecutionError):
        _provider(transport, max_extra_attempts=0).complete("hi")

    assert len(transport.calls) == 1


def test_missing_credential_fails_closed_without_any_transport_call() -> None:
    transport = FakeTransport([_completion_body()])
    provider = _provider(transport, api_key="")

    assert not provider.is_configured
    with pytest.raises(AIUnavailableError, match="API Key"):
        provider.complete("hi")
    with pytest.raises(AIUnavailableError, match="API Key"):
        provider.embed(["文本"])

    assert transport.calls == []


def test_default_transport_never_emits_real_requests() -> None:
    provider = QwenProvider(
        api_key=SECRET,
        llm_model="qwen3.7-plus",
        llm_model_hard="qwen3.8-max",
        embedding_model="qwen3.7-text-embedding",
        rerank_model="qwen3-rerank",
    )

    with pytest.raises(AIUnavailableError, match="不发起真实 AI API 请求"):
        provider.complete("hi")


def test_exception_messages_never_carry_the_api_key() -> None:
    transport = FakeTransport([QwenTransportError("rate limited", status_code=429)])

    with pytest.raises(AIExecutionError) as captured:
        _provider(transport).complete("hi")
    assert SECRET not in str(captured.value)

    default_transport_provider = QwenProvider(
        api_key=SECRET,
        llm_model="qwen3.7-plus",
        llm_model_hard="qwen3.8-max",
        embedding_model="qwen3.7-text-embedding",
        rerank_model="qwen3-rerank",
    )
    with pytest.raises(AIUnavailableError) as captured_unavailable:
        default_transport_provider.complete("hi")
    assert SECRET not in str(captured_unavailable.value)


def test_embed_builds_batch_request_and_preserves_input_order() -> None:
    body = {
        "model": "qwen3.7-text-embedding",
        "data": [
            {"index": 1, "embedding": [0.5, 0.6]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ],
    }
    transport = FakeTransport([body])
    provider = _provider(transport)

    result = provider.embed(["第一", "第二"])

    url, _, payload, timeout = transport.calls[0]
    assert url == f"{DEFAULT_BASE_URL}/embeddings"
    assert payload == {"model": "qwen3.7-text-embedding", "input": ["第一", "第二"]}
    assert timeout == 12.5
    assert result.embeddings == ((0.1, 0.2), (0.5, 0.6))
    assert result.model == "qwen3.7-text-embedding"


def test_embed_rejects_empty_input_before_any_call() -> None:
    transport = FakeTransport([])

    with pytest.raises(ValueError, match="不能为空"):
        _provider(transport).embed([])

    assert transport.calls == []


def test_embed_malformed_response_fails_without_retry() -> None:
    transport = FakeTransport([{"data": [{"index": 0}]}])

    with pytest.raises(AIExecutionError, match="响应解析失败"):
        _provider(transport).embed(["文本"])

    assert len(transport.calls) == 1


def test_rerank_is_explicitly_deferred() -> None:
    provider = _provider(FakeTransport([]))

    with pytest.raises(AIUnavailableError, match="rerank"):
        provider.rerank("查询", ["文档一", "文档二"])


def test_thinking_is_explicitly_off_by_default() -> None:
    """qwen3.7-plus enables thinking by default; we must always send false."""

    transport = FakeTransport([_completion_body()])
    _provider(transport).complete("hi")

    assert transport.calls[0][2]["enable_thinking"] is False
    assert transport.calls[0][2]["stream"] is False


def test_thinking_requires_explicit_opt_in() -> None:
    transport = FakeTransport([_completion_body()])
    _provider(transport, enable_thinking=True).complete("hi")

    assert transport.calls[0][2]["enable_thinking"] is True


def test_max_completion_tokens_included_only_when_given() -> None:
    transport = FakeTransport([_completion_body(), _completion_body()])
    provider = _provider(transport)

    provider.complete("hi", max_completion_tokens=64)
    provider.complete("hi")

    assert transport.calls[0][2]["max_completion_tokens"] == 64
    assert "max_completion_tokens" not in transport.calls[1][2]


def test_invalid_max_completion_tokens_fails_before_any_call() -> None:
    transport = FakeTransport([])

    with pytest.raises(ValueError, match="max_completion_tokens"):
        _provider(transport).complete("hi", max_completion_tokens=0)

    assert transport.calls == []


def test_finish_reason_is_captured_when_present() -> None:
    body = _completion_body()
    body["choices"][0]["finish_reason"] = "stop"
    result = _provider(FakeTransport([body])).complete("hi")

    assert result.finish_reason == "stop"


def test_finish_reason_absent_is_none_not_error() -> None:
    result = _provider(FakeTransport([_completion_body()])).complete("hi")

    assert result.finish_reason is None


def test_invalid_policy_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_extra_attempts"):
        _provider(FakeTransport([]), max_extra_attempts=-1)
    with pytest.raises(ValueError, match="timeout_seconds"):
        _provider(FakeTransport([]), timeout_seconds=0)
