"""Agent Foundation package (v0.6.0).

Contains the vendor-neutral Tool Contract, the Phase 1 seven-tool read-only
registry, the single-step read-only Agent execution kernel, and the Phase 2B
model-backed structured decision adapter (``src.agent.decision``).

Independence contract:

- no ``streamlit`` / UI imports;
- no provider / vendor AI imports in the execution kernel;
- no database connection imports in the execution kernel itself;
- the decision adapter imports the existing vendor-neutral ``src.ai.provider``
  boundary only, never ``src.ai.qwen_client``;
- no schema v13 tables or migrations.
"""

from src.agent.decision import (
    DEFAULT_DECISION_MAX_OUTPUT_TOKENS,
    DEFAULT_DECISION_SOURCE_FEATURE,
    MAX_DECISION_OUTPUT_CHARS,
    DecisionParseError,
    ModelDecisionProvider,
    build_decision_prompt,
    build_tool_catalog,
    parse_decision,
)
from src.agent.execution import (
    AgentDecision,
    AgentDecisionKind,
    AgentExecutionError,
    AgentExecutionErrorCode,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentRequest,
    AgentRuntimeTrace,
    DecisionProvider,
    SingleStepAgentExecutor,
    build_single_step_executor,
)

__all__ = [
    "AgentDecision",
    "AgentDecisionKind",
    "AgentExecutionError",
    "AgentExecutionErrorCode",
    "AgentExecutionResult",
    "AgentExecutionStatus",
    "AgentRequest",
    "AgentRuntimeTrace",
    "DEFAULT_DECISION_MAX_OUTPUT_TOKENS",
    "DEFAULT_DECISION_SOURCE_FEATURE",
    "DecisionParseError",
    "DecisionProvider",
    "MAX_DECISION_OUTPUT_CHARS",
    "ModelDecisionProvider",
    "SingleStepAgentExecutor",
    "build_decision_prompt",
    "build_single_step_executor",
    "build_tool_catalog",
    "parse_decision",
]
