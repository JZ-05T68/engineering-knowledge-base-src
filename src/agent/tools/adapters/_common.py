"""Shared helpers for the v0.6.0 Phase 1B read-only Tool Adapters.

The helpers intentionally stay tiny: argument boundary validation, stable-id
parsing, and ToolResult construction. They contain no retrieval or knowledge
business logic and never touch the network, a model provider, or a UI.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from src.agent.tools.contracts import (
    ToolError,
    ToolErrorCode,
    ToolMetadata,
    ToolReference,
    ToolResult,
    ToolResultStatus,
)

LOGGER = logging.getLogger(__name__)


class AdapterInputError(ValueError):
    """Raised when Tool arguments fail the Tool-boundary validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def reject_unknown_arguments(
    arguments: Mapping[str, object], allowed: frozenset[str]
) -> None:
    """Fail closed on arguments the Adapter did not declare."""
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise AdapterInputError(f"不支持的参数：{', '.join(unknown)}")


def require_text(
    arguments: Mapping[str, object],
    key: str,
    *,
    max_length: int = 1000,
) -> str:
    """Return a required, non-empty, whitespace-stripped string argument."""
    value = arguments.get(key)
    if value is None:
        raise AdapterInputError(f"缺少必填参数：{key}")
    if not isinstance(value, str):
        raise AdapterInputError(f"参数 {key} 必须是字符串")
    value = value.strip()
    if not value:
        raise AdapterInputError(f"参数 {key} 不能为空")
    if len(value) > max_length:
        raise AdapterInputError(f"参数 {key} 不能超过 {max_length} 字符")
    return value


def optional_int(
    arguments: Mapping[str, object],
    key: str,
    *,
    default: int | None,
    min_value: int = 1,
    max_value: int = 100,
) -> int | None:
    """Return an optional bounded integer argument, or ``default``."""
    if key not in arguments or arguments[key] is None:
        return default
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterInputError(f"参数 {key} 必须是整数")
    if not min_value <= value <= max_value:
        raise AdapterInputError(
            f"参数 {key} 必须是 {min_value}～{max_value} 的整数"
        )
    return value


def parse_stable_id(stable_id: str) -> tuple[str, str, int]:
    """Parse ``<kb_uuid>:<type>:<local_id>`` into its three parts."""
    if not isinstance(stable_id, str):
        raise AdapterInputError("stable_id 必须是字符串")
    parts = stable_id.strip().split(":", 2)
    if len(parts) != 3:
        raise AdapterInputError(
            "stable_id 格式必须为 <kb_uuid>:<type>:<local_id>"
        )
    kb_uuid, object_type, raw_local_id = parts
    if not kb_uuid or not object_type or not raw_local_id:
        raise AdapterInputError(
            "stable_id 格式必须为 <kb_uuid>:<type>:<local_id>"
        )
    try:
        local_id = int(raw_local_id)
    except ValueError:
        raise AdapterInputError("stable_id 的 local_id 必须是正整数") from None
    if local_id < 1:
        raise AdapterInputError("stable_id 的 local_id 必须是正整数")
    return kb_uuid, object_type, local_id


def success_result(
    tool_name: str,
    data: object,
    *,
    references: tuple[ToolReference, ...] = (),
    warnings: tuple[str, ...] = (),
    duration_ms: int | None = None,
) -> ToolResult:
    """Build a SUCCESS ToolResult with minimal metadata."""
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=tuple(references),
        warnings=tuple(warnings),
        metadata=ToolMetadata(tool_name=tool_name, duration_ms=duration_ms),
    )


def empty_result(
    tool_name: str,
    data: object,
    *,
    references: tuple[ToolReference, ...] = (),
    warnings: tuple[str, ...] = (),
    duration_ms: int | None = None,
) -> ToolResult:
    """Build an EMPTY ToolResult: legal execution with zero hits."""
    return ToolResult(
        status=ToolResultStatus.EMPTY,
        data=data,
        references=tuple(references),
        warnings=tuple(warnings),
        metadata=ToolMetadata(tool_name=tool_name, duration_ms=duration_ms),
    )


def partial_result(
    tool_name: str,
    data: object,
    *,
    warnings: tuple[str, ...],
    references: tuple[ToolReference, ...] = (),
    duration_ms: int | None = None,
) -> ToolResult:
    """Build a PARTIAL ToolResult with explicit degradation warnings."""
    return ToolResult(
        status=ToolResultStatus.PARTIAL,
        data=data,
        references=tuple(references),
        warnings=tuple(warnings),
        metadata=ToolMetadata(tool_name=tool_name, duration_ms=duration_ms),
    )


def failed_result(
    tool_name: str,
    code: ToolErrorCode,
    message: str,
    *,
    retryable: bool = False,
    detail: str | None = None,
    metadata: Mapping[str, object] | None = None,
    duration_ms: int | None = None,
) -> ToolResult:
    """Build a FAILED ToolResult carrying a structured ToolError."""
    return ToolResult(
        status=ToolResultStatus.FAILED,
        data=None,
        error=ToolError(
            code=code,
            message=message,
            retryable=retryable,
            detail=detail,
            metadata=dict(metadata or {}),
        ),
        metadata=ToolMetadata(tool_name=tool_name, duration_ms=duration_ms),
    )


def internal_failure_result(
    tool_name: str,
    exc: Exception,
    *,
    safe_message: str = "工具执行失败",
) -> ToolResult:
    """Map an unexpected exception to a safe INTERNAL_FAILURE result."""
    LOGGER.exception("Tool %s 执行失败", tool_name)
    return failed_result(
        tool_name,
        ToolErrorCode.INTERNAL_FAILURE,
        safe_message,
        detail=type(exc).__name__,
    )
