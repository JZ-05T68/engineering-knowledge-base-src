"""Final Answer Stage and Agent response pipeline (v0.6.0 Phase 2C).

This package contains the thin orchestration layer that closes the loop
between ``ToolResult`` and a validated, structured ``AgentResponse`` by
reusing the existing RAG Answer chain and citation validator. It does not
implement a second RAG pipeline, does not retry, does not plan, and does not
touch schema v13.
"""

from src.agent.response.contracts import (
    AgentResponse,
    AgentResponseError,
    AgentResponseErrorCode,
    AgentResponseStatus,
)
from src.agent.response.final_answer import (
    DEFAULT_FINAL_ANSWER_MAX_OUTPUT_TOKENS,
    DEFAULT_FINAL_ANSWER_SOURCE_FEATURE,
    FinalAnswerStage,
)
from src.agent.response.pipeline import SingleStepAgentService
from src.agent.response.tool_context import ToolResultContextMapper

__all__ = [
    "DEFAULT_FINAL_ANSWER_MAX_OUTPUT_TOKENS",
    "DEFAULT_FINAL_ANSWER_SOURCE_FEATURE",
    "AgentResponse",
    "AgentResponseError",
    "AgentResponseErrorCode",
    "AgentResponseStatus",
    "FinalAnswerStage",
    "SingleStepAgentService",
    "ToolResultContextMapper",
]
