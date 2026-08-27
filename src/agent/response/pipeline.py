"""Single-step Agent orchestration pipeline (v0.6.0 Phase 2C).

``SingleStepAgentService`` is a thin composition of the frozen Phase 2A
executor and the Phase 2C Final Answer Stage. It does not add retry, loops,
planning, or multi-step behavior: the pipeline is exactly

    Decision → 0/1 Tool → Final Answer → STOP.
"""

from __future__ import annotations

from src.agent.execution.contracts import AgentRequest, DecisionProvider
from src.agent.execution.executor import SingleStepAgentExecutor
from src.agent.response.contracts import AgentResponse
from src.agent.response.final_answer import FinalAnswerStage

__all__ = ["SingleStepAgentService"]


class SingleStepAgentService:
    """Run one single-step Agent request and produce a structured response."""

    def __init__(
        self,
        executor: SingleStepAgentExecutor,
        final_answer: FinalAnswerStage,
    ) -> None:
        self._executor = executor
        self._final_answer = final_answer

    def run(
        self, request: AgentRequest, decision_provider: DecisionProvider
    ) -> AgentResponse:
        """Execute one decision, at most one Tool, then the Final Answer Stage."""
        execution = self._executor.execute(request, decision_provider)
        return self._final_answer.answer(request, execution)
