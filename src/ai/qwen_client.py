"""Qwen (Aliyun Bailian / DashScope) adapter for the AI provider boundary.

This is the only module allowed to contain vendor-specific details: the
endpoint layout, request payloads, response parsing, and error mapping of
the Qwen API. Business services never import this module; they depend on
the protocols in ``src.ai.provider``.

Phase v0.5.0-1 constraint — **no real network request**:

- The wire transport is an injected callable. The default is the
  unconfigured transport, which raises ``AIUnavailableError`` on any call,
  so this adapter structurally cannot emit real HTTP traffic in this
  phase. A real transport is a later-phase change, explicitly wired.
- Construction performs no I/O: it does not read the environment, touch
  the disk, or open a connection.

Retry policy (cost and loop guardrails):

- At most ``max_extra_attempts`` (default 2) extra attempts per call, a
  flat bounded loop — never recursive, never an agent loop.
- Only transient transport failures are retried: network-level failures
  (no HTTP status), HTTP 429, and HTTP 5xx.
- Client errors (other 4xx), malformed responses, and semantic
  dissatisfaction with an answer are never retried.

The chat-completions and embeddings payloads follow the DashScope
OpenAI-compatible mode. The rerank channel uses a different vendor
contract whose official specification is verified in a later phase;
``rerank`` therefore raises ``AIUnavailableError`` for now instead of
guessing a wire protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from src.ai.provider import (
    AIExecutionError,
    AIUnavailableError,
    CompletionResult,
    CompletionUsage,
    EmbeddingResult,
    RerankResult,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_EXTRA_ATTEMPTS",
    "QwenProvider",
    "QwenTransportError",
    "Transport",
]

DEFAULT_BASE_URL: Final[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MAX_EXTRA_ATTEMPTS: Final[int] = 2


class Transport(Protocol):
    """Minimal injected wire boundary: one request, one decoded response.

    Implementations receive the full URL, request headers, the JSON-ready
    payload, and the per-call timeout in seconds, and return the decoded
    JSON response body. They raise ``QwenTransportError`` for transport
    and HTTP-level failures. A real HTTP implementation is deliberately
    not provided in this phase.
    """

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Send one request and return the decoded response body."""
        ...


class QwenTransportError(RuntimeError):
    """Transport-level failure reported by the injected transport callable.

    ``status_code`` is the HTTP status when a response exists; ``None``
    means a network-level failure before any HTTP response arrived.
    Messages must never carry API keys.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _unconfigured_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Default transport: guarantee this phase never emits real requests."""

    raise AIUnavailableError(
        "AI 传输层未配置：v0.5.0 当前阶段不发起真实 AI API 请求。"
    )


def _is_transient(error: QwenTransportError) -> bool:
    """Return whether a transport failure is eligible for bounded retry."""

    return (
        error.status_code is None
        or error.status_code == 429
        or error.status_code >= 500
    )


class QwenProvider:
    """Qwen adapter implementing the completion and embedding contracts.

    The API key is accepted as plain text at this boundary (the caller's
    ``SecretStr`` is unwrapped once in the runtime factory) and is used
    only to build the ``Authorization`` header; it never appears in
    exceptions, logs, or string representations of results.
    """

    def __init__(
        self,
        *,
        api_key: str,
        llm_model: str,
        llm_model_hard: str,
        embedding_model: str,
        rerank_model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_extra_attempts: int = MAX_EXTRA_ATTEMPTS,
        transport: Transport = _unconfigured_transport,
    ) -> None:
        if max_extra_attempts < 0:
            raise ValueError("max_extra_attempts 不能为负数")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self._api_key = api_key
        self._llm_model = llm_model
        self._llm_model_hard = llm_model_hard
        self._embedding_model = embedding_model
        self._rerank_model = rerank_model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_extra_attempts = max_extra_attempts
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        """Return whether an API credential is present."""

        return bool(self._api_key)

    def complete(self, prompt: str, *, model: str | None = None) -> CompletionResult:
        """Return the completion for ``prompt`` via the chat endpoint."""

        self._require_credential()
        chosen_model = model or self._llm_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = self._post("/chat/completions", payload)
        return self._parse_completion(response, chosen_model)

    def embed(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> EmbeddingResult:
        """Return one embedding per input text, in input order."""

        self._require_credential()
        if not texts:
            raise ValueError("embedding 输入不能为空")
        chosen_model = model or self._embedding_model
        payload: dict[str, Any] = {"model": chosen_model, "input": list(texts)}
        response = self._post("/embeddings", payload)
        return self._parse_embeddings(response, chosen_model)

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        model: str | None = None,
        top_n: int | None = None,
    ) -> RerankResult:
        """Rerank is deferred: its vendor contract is verified in a later phase."""

        raise AIUnavailableError(
            "Qwen rerank 通道尚未启用：其官方接口契约将在后续阶段核对后接入。"
        )

    def _require_credential(self) -> None:
        if not self._api_key:
            raise AIUnavailableError(
                "AI 能力不可用：未配置 API Key（当前为手动模式或未设置 EKB_AI_API_KEY）。"
            )

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one request with the bounded, flat retry policy."""

        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        attempts = 1 + self._max_extra_attempts
        for attempt in range(attempts):
            try:
                return self._transport(url, headers, payload, self._timeout_seconds)
            except QwenTransportError as exc:
                if not _is_transient(exc) or attempt == attempts - 1:
                    raise AIExecutionError(
                        f"AI 请求执行失败（HTTP {exc.status_code}）：{exc}"
                    ) from exc
        raise AIExecutionError("AI 请求执行失败：重试次数已用尽。")  # pragma: no cover

    @staticmethod
    def _parse_completion(
        response: Mapping[str, Any], requested_model: str
    ) -> CompletionResult:
        """Parse one OpenAI-compatible chat completion response."""

        try:
            choices = response["choices"]
            message = choices[0]["message"]
            text = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIExecutionError(
                "AI 响应解析失败：缺少 choices/message/content 结构。"
            ) from exc
        if not isinstance(text, str):
            raise AIExecutionError("AI 响应解析失败：content 不是文本。")
        return CompletionResult(
            text=text,
            model=str(response.get("model") or requested_model),
            usage=QwenProvider._parse_usage(response.get("usage")),
        )

    @staticmethod
    def _parse_embeddings(
        response: Mapping[str, Any], requested_model: str
    ) -> EmbeddingResult:
        """Parse one OpenAI-compatible embeddings response, in input order."""

        try:
            data = response["data"]
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            vectors = tuple(
                tuple(float(value) for value in item["embedding"]) for item in ordered
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIExecutionError(
                "AI 响应解析失败：缺少 data/embedding 结构。"
            ) from exc
        return EmbeddingResult(
            embeddings=vectors,
            model=str(response.get("model") or requested_model),
        )

    @staticmethod
    def _parse_usage(raw_usage: Any) -> CompletionUsage | None:
        """Parse optional token usage; absent usage is not an error."""

        if not isinstance(raw_usage, Mapping):
            return None
        try:
            return CompletionUsage(
                prompt_tokens=int(raw_usage["prompt_tokens"]),
                completion_tokens=int(raw_usage["completion_tokens"]),
                total_tokens=int(raw_usage["total_tokens"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIExecutionError("AI 响应解析失败：usage 结构不完整。") from exc
