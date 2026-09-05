"""Qwen (Aliyun Bailian / DashScope) adapter for the AI provider boundary.

This is the only module allowed to contain vendor-specific details: the
endpoint layout, request payloads, response parsing, and error mapping of
the Qwen API. Business services never import this module; they depend on
the protocols in ``src.ai.provider``.

Wire transport:

- The wire transport is an injected callable. ``urllib_transport`` is the
  minimal standard-library HTTP implementation; it is never wired in
  automatically. The default remains the unconfigured transport, which
  raises ``AIUnavailableError`` on any call, so manual mode, a missing API
  key, application startup, import, and provider construction can never
  emit network traffic. Only an explicit ``complete``/``embed`` call on a
  provider that was deliberately built with a real transport can send a
  request.
- Construction performs no I/O: it does not read the environment, touch
  the disk, or open a connection.

Retry policy (cost and loop guardrails):

- At most ``max_extra_attempts`` (default 2) extra attempts per call, a
  flat bounded loop — never recursive, never an agent loop, and callers
  can override it (a paid smoke call forces 0).
- Only transient transport failures are retried: network-level failures
  (no HTTP status), HTTP 429, and HTTP 5xx.
- Client errors (other 4xx), malformed responses, and semantic
  dissatisfaction with an answer are never retried.

The chat-completions and embeddings payloads follow the DashScope
OpenAI-compatible mode. Thinking is a vendor-specific paid behavior:
``qwen3.7-plus`` enables it by default, so every request explicitly sends
``enable_thinking: false`` unless the adapter was deliberately constructed
with thinking enabled. The rerank channel uses a different vendor
contract whose official specification is verified in a later phase;
``rerank`` therefore raises ``AIUnavailableError`` for now instead of
guessing a wire protocol.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from src.ai.provider import (
    AIExecutionError,
    AIUnavailableError,
    CompletionResult,
    CompletionUsage,
    EmbeddingResult,
    EmbeddingUsage,
    RerankResult,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_EXTRA_ATTEMPTS",
    "QwenProvider",
    "QwenTransportError",
    "Transport",
    "urllib_transport",
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


def urllib_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Minimal standard-library HTTP transport (explicit opt-in only).

    Sends one JSON POST via ``urllib`` and returns the decoded response
    body. HTTP error statuses become ``QwenTransportError`` with the
    status code; network and timeout failures become ``QwenTransportError``
    without a status code; a non-JSON or non-object body is an
    ``AIExecutionError``. Credentials never appear in raised messages —
    only status codes and transport reasons are reported.
    """

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise QwenTransportError(
            f"HTTP 错误响应（状态 {exc.code}）", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QwenTransportError(f"网络传输失败：{type(exc).__name__}") from exc
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AIExecutionError("AI 响应解析失败：响应不是合法 JSON。") from exc
    if not isinstance(decoded, Mapping):
        raise AIExecutionError("AI 响应解析失败：响应不是 JSON 对象。")
    return decoded


def _unconfigured_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Default transport: guarantee this phase never emits real requests."""

    raise AIUnavailableError(
        "AI 传输层未配置：当前环境不发起真实 AI API 请求。"
    )


def _is_transient(error: QwenTransportError) -> bool:
    """Return whether a transport failure is eligible for bounded retry."""

    return (
        error.status_code is None
        or error.status_code == 429
        or error.status_code >= 500
    )


def _error_class_for(error: QwenTransportError) -> str:
    """Map one transport failure to the vendor-neutral ledger error class."""

    if error.status_code is None:
        return "transport"
    if error.status_code == 429:
        return "http_429"
    if error.status_code >= 500:
        return "http_5xx"
    return "http_4xx"


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
        vision_model: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_extra_attempts: int = MAX_EXTRA_ATTEMPTS,
        enable_thinking: bool = False,
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
        self._vision_model = vision_model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_extra_attempts = max_extra_attempts
        self._enable_thinking = enable_thinking
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        """Return whether an API credential is present."""

        return bool(self._api_key)

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        """Return the completion for ``prompt`` via the chat endpoint.

        Every request explicitly carries ``enable_thinking`` (default off —
        thinking is a paid behavior that higher layers must opt into) and
        ``stream: false``; ``max_completion_tokens`` is included when given.
        No tools, web search, or agent fields are ever added.
        """

        self._require_credential()
        chosen_model = model or self._llm_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
            "enable_thinking": self._enable_thinking,
            "stream": False,
        }
        if max_completion_tokens is not None:
            if max_completion_tokens <= 0:
                raise ValueError("max_completion_tokens 必须为正数")
            payload["max_completion_tokens"] = max_completion_tokens
        response, retry_count = self._post("/chat/completions", payload)
        return self._parse_completion(response, chosen_model, retry_count=retry_count)

    def complete_vision(
        self,
        prompt: str,
        image_png_base64: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        """Return one completion over a prompt plus a PNG image (v0.7.2).

        The image is sent inline as a base64 data URL to a vision-capable
        model. Budget/audit responsibility stays with the caller's wrapper;
        this method only shapes the vendor payload.
        """

        self._require_credential()
        if not image_png_base64.strip():
            raise ValueError("视觉调用必须提供图片内容")
        chosen_model = model or self._vision_model
        if not chosen_model:
            raise AIUnavailableError(
                "未配置视觉模型，无法读取页面图片。"
            )
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_png_base64}"
                            },
                        },
                    ],
                }
            ],
            "enable_thinking": self._enable_thinking,
            "stream": False,
        }
        if max_completion_tokens is not None:
            if max_completion_tokens <= 0:
                raise ValueError("max_completion_tokens 必须为正数")
            payload["max_completion_tokens"] = max_completion_tokens
        response, retry_count = self._post("/chat/completions", payload)
        return self._parse_completion(response, chosen_model, retry_count=retry_count)

    def embed(
        self,
        texts: Sequence[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        """Return one embedding per input text, in input order.

        The payload stays minimal (model, input, float encoding, optional
        dimensions) with no thinking-related or vendor-extra fields. The
        response is validated fail-closed: vector count must match the
        input count, indexes must be an exact permutation of the input
        positions, no vector may be empty, non-numeric values are
        rejected, and a requested ``dimensions`` value must match every
        returned vector.
        """

        self._require_credential()
        if not texts:
            raise ValueError("embedding 输入不能为空")
        if dimensions is not None and dimensions <= 0:
            raise ValueError("dimensions 必须为正数")
        chosen_model = model or self._embedding_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        response, retry_count = self._post("/embeddings", payload)
        return self._parse_embeddings(
            response,
            chosen_model,
            expected_count=len(texts),
            dimensions=dimensions,
            retry_count=retry_count,
        )

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

    def _post(
        self, path: str, payload: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], int]:
        """Send one request with the bounded, flat retry policy.

        Returns ``(response_body, extra_attempts_used)`` so the adapter can
        report the consumed retry budget to the vendor-neutral audit layer.
        """

        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        attempts = 1 + self._max_extra_attempts
        for attempt in range(attempts):
            try:
                return self._transport(url, headers, payload, self._timeout_seconds), attempt
            except QwenTransportError as exc:
                if not _is_transient(exc) or attempt == attempts - 1:
                    raise AIExecutionError(
                        f"AI 请求执行失败（HTTP {exc.status_code}）：{exc}",
                        error_class=_error_class_for(exc),
                        retry_count=attempt,
                    ) from exc
        raise AIExecutionError(
            "AI 请求执行失败：重试次数已用尽。",
            error_class="transport",
            retry_count=self._max_extra_attempts,
        )  # pragma: no cover

    @staticmethod
    def _parse_completion(
        response: Mapping[str, Any],
        requested_model: str,
        *,
        retry_count: int = 0,
    ) -> CompletionResult:
        """Parse one OpenAI-compatible chat completion response."""

        try:
            choices = response["choices"]
            message = choices[0]["message"]
            text = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIExecutionError(
                "AI 响应解析失败：缺少 choices/message/content 结构。",
                error_class="parse",
            ) from exc
        if not isinstance(text, str):
            raise AIExecutionError(
                "AI 响应解析失败：content 不是文本。", error_class="parse"
            )
        return CompletionResult(
            text=text,
            model=str(response.get("model") or requested_model),
            usage=QwenProvider._parse_usage(response.get("usage")),
            finish_reason=QwenProvider._parse_finish_reason(choices[0]),
            retry_count=retry_count,
        )

    @staticmethod
    def _parse_finish_reason(choice: Any) -> str | None:
        """Read the optional finish reason; absent or non-text is not an error."""

        if isinstance(choice, Mapping):
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                return reason
        return None

    @staticmethod
    def _parse_embeddings(
        response: Mapping[str, Any],
        requested_model: str,
        *,
        expected_count: int,
        dimensions: int | None,
        retry_count: int = 0,
    ) -> EmbeddingResult:
        """Parse and fail-closed validate one embeddings response."""

        try:
            data = response["data"]
            items = sorted(data, key=lambda item: item["index"])
        except (KeyError, TypeError) as exc:
            raise AIExecutionError(
                "AI 响应解析失败：缺少 data/embedding 结构。",
                error_class="parse",
            ) from exc
        if len(items) != expected_count:
            raise AIExecutionError(
                f"AI 响应校验失败：返回向量数 {len(items)} 与输入数 {expected_count} 不一致。",
                error_class="parse",
            )
        if [item["index"] for item in items] != list(range(expected_count)):
            raise AIExecutionError(
                "AI 响应校验失败：embedding index 与输入顺序不对应。",
                error_class="parse",
            )
        vectors: list[tuple[float, ...]] = []
        for item in items:
            raw_vector = item.get("embedding")
            if raw_vector is None:
                raise AIExecutionError(
                    "AI 响应解析失败：缺少 data/embedding 结构。",
                    error_class="parse",
                )
            if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, str):
                raise AIExecutionError(
                    "AI 响应校验失败：embedding 不是数值数组。",
                    error_class="parse",
                )
            if len(raw_vector) == 0:
                raise AIExecutionError(
                    "AI 响应校验失败：存在空 embedding 向量。",
                    error_class="parse",
                )
            if dimensions is not None and len(raw_vector) != dimensions:
                raise AIExecutionError(
                    f"AI 响应校验失败：向量维度 {len(raw_vector)} 与请求值 {dimensions} 不一致。",
                    error_class="parse",
                )
            try:
                vectors.append(tuple(float(value) for value in raw_vector))
            except (TypeError, ValueError) as exc:
                raise AIExecutionError(
                    "AI 响应校验失败：embedding 含非数值元素。",
                    error_class="parse",
                ) from exc
        return EmbeddingResult(
            embeddings=tuple(vectors),
            model=str(response.get("model") or requested_model),
            usage=QwenProvider._parse_embedding_usage(response.get("usage")),
            retry_count=retry_count,
        )

    @staticmethod
    def _parse_embedding_usage(raw_usage: Any) -> EmbeddingUsage | None:
        """Parse optional embedding token usage; absent usage is not an error."""

        if not isinstance(raw_usage, Mapping):
            return None
        try:
            return EmbeddingUsage(
                prompt_tokens=int(raw_usage["prompt_tokens"]),
                total_tokens=int(raw_usage["total_tokens"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIExecutionError(
                "AI 响应解析失败：usage 结构不完整。", error_class="parse"
            ) from exc

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
            raise AIExecutionError(
                "AI 响应解析失败：usage 结构不完整。", error_class="parse"
            ) from exc
