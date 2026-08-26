"""Tool Contract and Phase 1 read-only Tool Adapters (v0.6.0).

This package freezes the vendor-neutral, agent-runtime-neutral and UI-neutral
tool contract (definitions, inputs, contexts, results, errors, references,
side-effect classification, registry) and the first four read-only adapters.

It deliberately contains no Agent, no executor, no retry loop, no AI provider
dependency, and no Streamlit dependency. ``rag_answer`` is not a Tool
(ADR-006 decision 7); it belongs to the Final Answer Stage.
"""

from src.agent.tools.bootstrap import (
    build_phase1_handlers,
    build_phase1_registry,
    phase1_tool_definitions,
)
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
    "build_phase1_handlers",
    "build_phase1_registry",
    "phase1_tool_definitions",
]
