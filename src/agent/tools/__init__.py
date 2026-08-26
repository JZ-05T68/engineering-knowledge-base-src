"""Tool Contract Foundation (v0.6.0 Phase 1A).

This package freezes the vendor-neutral, agent-runtime-neutral and UI-neutral
tool contract: definitions, inputs, contexts, results, errors, references,
the side-effect classification, and the read-only registry.

It deliberately contains no Tool Adapter, no Agent, no executor, no retry
loop, no database connection, and no AI provider dependency. ``rag_answer``
is not a Tool (ADR-006 decision 7); it belongs to the Final Answer Stage.
"""

from src.agent.tools.contracts import (
    MAX_DESCRIPTION_LENGTH,
    MAX_TOOL_NAME_LENGTH,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolErrorCode,
    ToolHandler,
    ToolInput,
    ToolMetadata,
    ToolReference,
    ToolResult,
    ToolResultStatus,
    ToolSideEffect,
)
from src.agent.tools.registry import (
    DuplicateToolError,
    Phase1ReadOnlyPolicy,
    ToolExecutionPolicy,
    ToolNotAllowedError,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
)

__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_TOOL_NAME_LENGTH",
    "DuplicateToolError",
    "Phase1ReadOnlyPolicy",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCode",
    "ToolExecutionPolicy",
    "ToolHandler",
    "ToolInput",
    "ToolMetadata",
    "ToolNotAllowedError",
    "ToolReference",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolResultStatus",
    "ToolSideEffect",
    "UnknownToolError",
]
