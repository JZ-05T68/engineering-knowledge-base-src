"""Single-step read-only Agent execution kernel (v0.6.0 Phase 2A).

This package contains only the execution boundary: structured decision
contracts, the vendor-independent DecisionProvider protocol, and the
single-step executor that enforces at most one READ_ONLY Tool call with zero
Agent retry. No real model integration, no Final Answer Stage, no loop, and no
schema v13 live here.
"""

from src.agent.execution.contracts import (
    MAX_AGENT_REQUEST_CHARS,
    AgentDecision,
    AgentDecisionKind,
    AgentExecutionError,
    AgentExecutionErrorCode,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentRequest,
    AgentRuntimeTrace,
    DecisionProvider,
)
from src.agent.execution.executor import (
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
    "DecisionProvider",
    "MAX_AGENT_REQUEST_CHARS",
    "SingleStepAgentExecutor",
    "build_single_step_executor",
]
