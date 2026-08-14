"""Vendor-neutral AI provider contracts for the optional AI layer.

This module is the single authoritative definition of the AI call boundary:

- ``CompletionProvider`` / ``EmbeddingProvider`` / ``RerankProvider`` are the
  protocols a concrete vendor adapter must satisfy, one per capability so
  callers depend only on the capability they actually use;
- ``AIError`` / ``AIUnavailableError`` / ``AIExecutionError`` express the two
  distinct failure semantics: "AI cannot be called right now" (manual mode,
  missing credential, capability not yet enabled) versus "a call was
  attempted and failed" (transport, protocol, or parsing failure);
- the frozen result dataclasses carry only the fields callers need, plus the
  model echo required for auditability.

The module intentionally performs no I/O at import time: it reads no
configuration, no API key, no disk, and no network, and imports no vendor
SDK. Vendor-specific adapters (for example the Qwen adapter) live outside
this module and implement these protocols. Business services never import
vendor modules; they depend on these contracts only.

``AIProvider`` is the legacy minimal extension point originally defined in
``src.prompt_builder``. It is kept for backward compatibility and re-exported
there; new code should depend on the capability protocols above.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "AIError",
    "AIExecutionError",
    "AIProvider",
    "AIUnavailableError",
    "CompletionProvider",
    "CompletionResult",
    "CompletionUsage",
    "EmbeddingProvider",
    "EmbeddingResult",
    "RerankHit",
    "RerankProvider",
    "RerankResult",
    "require_ai_provider",
]


class AIError(RuntimeError):
    """Base class of all AI boundary failures.

    Messages must never carry API keys or credentials.
    """


class AIUnavailableError(AIError):
    """AI cannot be called right now.

    Covers manual AI mode, a missing API key, a disabled capability, or an
    adapter without a configured transport. This is deliberately distinct
    from ``AIExecutionError`` so callers can tell "not configured" apart
    from "configured but the call failed".
    """


class AIExecutionError(AIError):
    """An AI call was attempted and failed.

    Covers transport failures, vendor error responses, and malformed
    response payloads after the retry policy is exhausted.
    """


@dataclass(frozen=True, slots=True)
class CompletionUsage:
    """Token accounting reported by one completion call."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """One completion response with its model echo and optional usage."""

    text: str
    model: str
    usage: CompletionUsage | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Embeddings for one batch of input texts, in input order."""

    embeddings: tuple[tuple[float, ...], ...]
    model: str


@dataclass(frozen=True, slots=True)
class RerankHit:
    """One reranked document: its index in the input batch and its score."""

    index: int
    relevance_score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Reranked hits ordered by descending relevance."""

    hits: tuple[RerankHit, ...]
    model: str


@runtime_checkable
class CompletionProvider(Protocol):
    """Minimal contract of a text completion provider."""

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        """Return the completion for ``prompt``.

        ``model`` overrides the provider's default model for this call.
        ``max_completion_tokens`` caps the generated tokens for this call;
        ``None`` leaves the provider default. Implementations raise
        ``AIUnavailableError`` when the provider cannot be called at all,
        and ``AIExecutionError`` when an attempted call fails. Semantic
        dissatisfaction with an answer is never a reason for the client
        layer to retry automatically.
        """
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal contract of a text embedding provider."""

    def embed(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> EmbeddingResult:
        """Return one embedding per input text, in input order."""
        ...


@runtime_checkable
class RerankProvider(Protocol):
    """Minimal contract of a document rerank provider."""

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        model: str | None = None,
        top_n: int | None = None,
    ) -> RerankResult:
        """Return input document indexes ordered by relevance to ``query``."""
        ...


@runtime_checkable
class AIProvider(Protocol):
    """Legacy minimal extension point kept for backward compatibility.

    Originally defined in ``src.prompt_builder`` for the manual AI mode.
    New code should depend on ``CompletionProvider`` instead; this protocol
    remains the authoritative definition of the legacy ``generate``
    signature so existing imports keep working unchanged.
    """

    def generate(self, prompt: str) -> str:
        """Generate a response for ``prompt`` when a provider is enabled."""
        ...


def require_ai_provider(provider: CompletionProvider | None) -> CompletionProvider:
    """Return the given provider, or raise ``AIUnavailableError`` when absent.

    Callers that receive an optional provider use this to convert the
    disabled state into one explicit, typed failure instead of an
    ``AttributeError`` on ``None``. A real provider instance is returned
    unchanged — never wrapped or replaced.
    """

    if provider is None:
        raise AIUnavailableError(
            "AI 能力不可用：当前为手动模式，或尚未配置 API Key。"
        )
    return provider
