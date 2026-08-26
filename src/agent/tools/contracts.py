"""Tool Contract types for the v0.6.0 Agent Foundation (Phase 1A).

This module freezes the vendor-neutral contract that future Tool Adapters and
the single-step read-only Agent will consume. It intentionally contains no
execution engine, no retry loop, no model provider, no database connection,
and no Streamlit dependency.

ADR-006 decision 7: ``rag_answer`` is NOT a Tool; it is the Final Answer
Stage. No ``rag_answer`` definition is defined or registered here.

Tool name format (frozen):
    snake_case ASCII stable identifier matching
    ``^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$``,
    at most 100 characters, case-sensitive, non-empty. Display titles and
    Chinese descriptions must never be used as tool identity.

Immutability rules:
    ``ToolDefinition.input_schema``, ``ToolInput.arguments`` and
    ``ToolError.metadata`` are shallow-copied into read-only mapping proxies.
    Nested mutable containers are intentionally not deep-frozen here: the
    envelope guarantees top-level isolation, while nested values are owned by
    the Tool Adapter's per-tool schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from src.models import ContextFingerprintState

MAX_TOOL_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 2000
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class ToolSideEffect(StrEnum):
    """Frozen side-effect classification of a Tool.

    Phase 1 execution policy allows only :attr:`READ_ONLY`. The other values
    may be registered in the registry for future extension / tests, but the
    Phase 1 policy rejects them at resolve/execution validation time.
    """

    READ_ONLY = "read_only"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_DESTRUCTIVE = "write_destructive"


class ToolResultStatus(StrEnum):
    """Structured execution outcome of one Tool call.

    ``EMPTY`` is a legal successful execution with zero hits, not a failure.
    ``PARTIAL`` is a usable result with an explicit degradation warning.
    ``FAILED`` means no consumable result was produced and carries a
    :class:`ToolError`.
    """

    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolErrorCode(StrEnum):
    """Closed set of Tool-layer error codes (frozen in ADR-006 / entry §8.2).

    ``PROVIDER_UNAVAILABLE`` covers the "AI unavailable / manual mode"
    semantics required by Phase 1A; ``TOOL_UNAVAILABLE`` covers unregistered
    or policy-rejected tools.
    """

    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    EMPTY_RESULT = "empty_result"
    STALE_SOURCE = "stale_source"
    MISSING_SOURCE = "missing_source"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERNAL_FAILURE = "internal_failure"
    CITATION_INVALID = "citation_invalid"


def _validate_tool_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Tool name 必须是字符串")
    name = name.strip()
    if not name:
        raise ValueError("Tool name 不能为空")
    if len(name) > MAX_TOOL_NAME_LENGTH:
        raise ValueError(f"Tool name 不能超过 {MAX_TOOL_NAME_LENGTH} 字符")
    if not _TOOL_NAME_RE.fullmatch(name):
        raise ValueError(
            "Tool name 必须是 snake_case ASCII 标识符，"
            "格式为 ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
        )
    return name


def _validate_description(description: str) -> str:
    if not isinstance(description, str):
        raise TypeError("Tool description 必须是字符串")
    description = description.strip()
    if not description:
        raise ValueError("Tool description 不能为空")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Tool description 不能超过 {MAX_DESCRIPTION_LENGTH} 字符"
        )
    return description


def _freeze_mapping(value: Mapping[str, object], field_name: str) -> MappingProxyType[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是 mapping")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Static, immutable contract of one Tool.

    Carries only identity / metadata. It must never contain execution state,
    model instances, database sessions, or UI state.
    """

    name: str
    description: str
    side_effect: ToolSideEffect
    input_schema: Mapping[str, object] = field(default_factory=dict)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_tool_name(self.name))
        object.__setattr__(self, "description", _validate_description(self.description))
        object.__setattr__(self, "side_effect", ToolSideEffect(self.side_effect))
        object.__setattr__(
            self, "input_schema", _freeze_mapping(self.input_schema, "input_schema")
        )
        if self.timeout_seconds is not None:
            if isinstance(self.timeout_seconds, bool) or not isinstance(
                self.timeout_seconds, (int, float)
            ):
                raise ValueError("timeout_seconds 必须是正数")
            if self.timeout_seconds <= 0:
                raise ValueError("timeout_seconds 必须是正数")
            object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view for audit/export."""
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect.value,
            "input_schema": dict(self.input_schema),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ToolInput:
    """Generic immutable envelope for one Tool invocation.

    The envelope only guarantees a named, isolated mapping boundary. Per-tool
    argument validation and unknown-field handling belong to the Tool Adapter
    (Phase 1B), not to this generic contract.
    """

    tool_name: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_name", _validate_tool_name(self.tool_name))
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments, "arguments"))

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view."""
        return {"tool_name": self.tool_name, "arguments": dict(self.arguments)}


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Minimal generic runtime metadata for one Tool execution.

    Deliberately contains no Streamlit session, user profile, prompt, database
    connection, or model provider. It is not tied to a persisted agent_run
    (schema v13 is deferred).
    """

    run_id: str | None = None
    request_id: str | None = None
    deadline_epoch_ms: int | None = None

    def __post_init__(self) -> None:
        if self.deadline_epoch_ms is not None:
            if isinstance(self.deadline_epoch_ms, bool) or not isinstance(
                self.deadline_epoch_ms, int
            ):
                raise ValueError("deadline_epoch_ms 必须是整数毫秒")
            if self.deadline_epoch_ms <= 0:
                raise ValueError("deadline_epoch_ms 必须是正整数")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view."""
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "deadline_epoch_ms": self.deadline_epoch_ms,
        }


@dataclass(frozen=True, slots=True)
class ToolReference:
    """One structured citation-lineage reference returned by a Tool.

    Reuses the existing ``stable_id`` namespace (``build_stable_id``) and the
    existing ``ContextFingerprintState`` values; it does not invent a new
    citation model. Future lineage:
    ``ToolResult.references -> ContextItem.source_anchors ->
    KnowledgeContextPackage.citations -> final answer citation validation``.
    """

    stable_id: str
    anchor_label: str = ""
    fingerprint_state: str = ContextFingerprintState.NOT_APPLICABLE.value

    def __post_init__(self) -> None:
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ValueError("ToolReference.stable_id 不能为空")
        object.__setattr__(self, "stable_id", self.stable_id.strip())
        if not isinstance(self.anchor_label, str):
            raise TypeError("anchor_label 必须是字符串")
        if not isinstance(self.fingerprint_state, str):
            raise TypeError("fingerprint_state 必须是字符串")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view."""
        return {
            "stable_id": self.stable_id,
            "anchor_label": self.anchor_label,
            "fingerprint_state": self.fingerprint_state,
        }


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Small audit-oriented metadata block carried by a ToolResult."""

    tool_name: str
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_name", _validate_tool_name(self.tool_name))
        if self.duration_ms is not None:
            if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
                raise ValueError("duration_ms 必须是整数")
            if self.duration_ms < 0:
                raise ValueError("duration_ms 不能为负数")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view."""
        return {"tool_name": self.tool_name, "duration_ms": self.duration_ms}


@dataclass(frozen=True, slots=True)
class ToolError:
    """Structured Tool-layer failure.

    ``message`` is the safe public message: it must never contain stack
    traces, absolute paths, API keys, database paths, or private content.
    ``detail`` is the internal diagnostic and must not be forwarded to an
    Agent final response by default.

    ``retryable`` reports transport-level retryability metadata only. It does
    NOT mean the Agent may retry: ADR-006 freezes Agent autonomous retry = 0.
    """

    code: ToolErrorCode
    message: str
    retryable: bool = False
    detail: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", ToolErrorCode(self.code))
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("ToolError.message 不能为空")
        object.__setattr__(self, "message", self.message.strip())
        if self.detail is not None:
            if not isinstance(self.detail, str):
                raise TypeError("ToolError.detail 必须是字符串")
            if not self.detail.strip():
                object.__setattr__(self, "detail", None)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))

    def to_dict(self, include_detail: bool = False) -> dict[str, object]:
        """Return a plain-structure view.

        By default ``detail`` is omitted so the output is safe for public /
        Agent-final-response surfaces. Pass ``include_detail=True`` only for
        internal logs or audit sinks.
        """
        payload: dict[str, object] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "metadata": dict(self.metadata),
        }
        if include_detail and self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Structured result of one Tool execution.

    Status invariants:

    - ``FAILED`` must carry ``error`` and must not be used for EMPTY;
    - non-``FAILED`` must not carry ``error``;
    - ``PARTIAL`` must carry at least one warning.
    """

    status: ToolResultStatus
    data: object = None
    references: tuple[ToolReference, ...] = ()
    warnings: tuple[str, ...] = ()
    error: ToolError | None = None
    metadata: ToolMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ToolResultStatus(self.status))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if self.status is ToolResultStatus.FAILED and self.error is None:
            raise ValueError("FAILED ToolResult 必须携带 error")
        if self.status is not ToolResultStatus.FAILED and self.error is not None:
            raise ValueError("非 FAILED ToolResult 不允许携带 error")
        if self.status is ToolResultStatus.PARTIAL and not self.warnings:
            raise ValueError("PARTIAL ToolResult 必须至少携带一条 warning")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic plain-structure view."""
        return {
            "status": self.status.value,
            "data": self.data,
            "references": [reference.to_dict() for reference in self.references],
            "warnings": list(self.warnings),
            "error": self.error.to_dict() if self.error is not None else None,
            "metadata": self.metadata.to_dict() if self.metadata is not None else None,
        }


@runtime_checkable
class ToolHandler(Protocol):
    """Minimal executable Tool boundary used only for contract tests.

    Phase 1A does not implement a Tool Executor; this protocol exists so tests
    can prove that a callable matching ``ToolInput + ToolContext ->
    ToolResult`` is expressible without an execution engine.
    """

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        """Execute one Tool and always return a structured ToolResult."""
        ...


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_TOOL_NAME_LENGTH",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolInput",
    "ToolMetadata",
    "ToolReference",
    "ToolResult",
    "ToolResultStatus",
    "ToolSideEffect",
]
