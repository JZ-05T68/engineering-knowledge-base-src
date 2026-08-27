"""Final Answer / Agent response contracts (v0.6.0 Phase 2C).

This module defines the minimal structured response envelope produced by the
Final Answer Stage. It deliberately does not replace ``AgentExecutionResult``:
the execution kernel result stays the source of decision/tool facts, and
``AgentResponse`` is the user-facing structured outcome after citation
validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.agent.execution.contracts import AgentRuntimeTrace
from src.ai.provider import CompletionUsage

__all__ = [
    "AgentResponse",
    "AgentResponseError",
    "AgentResponseErrorCode",
    "AgentResponseStatus",
]


class AgentResponseStatus(StrEnum):
    """Final structured response status."""

    COMPLETED = "completed"
    FAILED = "failed"


class AgentResponseErrorCode(StrEnum):
    """Closed set of Final Answer Stage failure codes."""

    TOOL_FAILED = "tool_failed"
    FINAL_ANSWER_FAILED = "final_answer_failed"
    CITATION_INVALID = "citation_invalid"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True, slots=True)
class AgentResponseError:
    """Safe, structured Final Answer Stage failure."""

    code: AgentResponseErrorCode
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Structured final response after citation validation.

    ``grounded`` is ``True`` only when the answer was generated from Tool
    evidence and passed the existing citation validator. ``citations`` are
    validated stable-id references from the model answer; ``context_stable_ids``
    are the allowed evidence identities provided to the model. Raw model output
    is never stored here.
    """

    status: AgentResponseStatus
    answer: str
    grounded: bool
    citations: tuple[str, ...] = ()
    context_stable_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: AgentResponseError | None = None
    trace: AgentRuntimeTrace | None = None
    token_usage: CompletionUsage | None = None
    model: str | None = None
