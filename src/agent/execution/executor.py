"""Single-step read-only Agent execution kernel (v0.6.0 Phase 2A).

The executor enforces ADR-006's Phase 1 safety invariants structurally:

- DecisionProvider is called at most once;
- the Tool Registry is the only Tool authority;
- at most one READ_ONLY Tool is executed;
- Agent autonomous retry is always 0;
- there is no loop and no multi-step path.

Every failure path returns a structured :class:`AgentExecutionResult` with a
safe message; raw exceptions never escape the kernel.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from src.agent.execution.contracts import (
    AgentDecision,
    AgentDecisionKind,
    AgentExecutionError,
    AgentExecutionErrorCode,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentRequest,
    AgentRuntimeTrace,
    DecisionProvider,
    new_run_id,
    utc_timestamp,
)
from src.agent.tools.contracts import (
    ToolContext,
    ToolHandler,
    ToolInput,
    ToolResult,
)
from src.agent.tools.registry import (
    ToolNotAllowedError,
    ToolRegistry,
    UnknownToolError,
)
from src.database import Database

LOGGER = logging.getLogger(__name__)


def build_single_step_executor(database: Database) -> SingleStepAgentExecutor:
    """Build the Phase 2A executor over the frozen Phase 1 seven-tool set.

    The caller provides the database; no global singleton and no Streamlit
    session is used. This factory is the agent-side composition convenience for
    tests and the future runtime.
    """
    from src.agent.tools.bootstrap import (
        build_phase1_handlers,
        build_phase1_registry,
    )

    return SingleStepAgentExecutor(
        build_phase1_registry(),
        handlers=build_phase1_handlers(database),
    )


class SingleStepAgentExecutor:
    """Execute exactly one decision and at most one READ_ONLY Tool.

    ``registry`` is the frozen Phase 1 Tool Registry (the only Tool authority).
    ``handlers`` maps registered Tool names to their callable adapters; the
    executor never imports or constructs a concrete service itself.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        self._registry = registry
        self._handlers = dict(handlers)

    def execute(
        self, request: AgentRequest, provider: DecisionProvider
    ) -> AgentExecutionResult:
        """Run one single-step execution and return a structured result.

        The kernel never retries, never loops, never selects a second Tool, and
        never lets a write Tool through the Phase 1 policy.
        """
        run_id = new_run_id()
        started_at = utc_timestamp()
        started_monotonic = time.monotonic()
        decision_call_count = 0

        try:
            decision = provider.decide(request)
            decision_call_count += 1
        except Exception as exc:
            LOGGER.exception("Agent decision provider 执行失败")
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=None,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.DECISION_PROVIDER_FAILED,
                    message="决策提供失败",
                    detail=type(exc).__name__,
                ),
            )

        if not isinstance(decision, AgentDecision):
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=None,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.INVALID_DECISION,
                    message="决策必须是结构化的 AgentDecision",
                ),
            )

        if decision.kind is AgentDecisionKind.ANSWER_DIRECTLY:
            return self._completed(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                tool_result=None,
            )

        return self._execute_tool(
            run_id=run_id,
            request=request,
            started_at=started_at,
            started_monotonic=started_monotonic,
            decision=decision,
            decision_call_count=decision_call_count,
        )

    def _execute_tool(
        self,
        *,
        run_id: str,
        request: AgentRequest,
        started_at: str,
        started_monotonic: float,
        decision: AgentDecision,
        decision_call_count: int,
    ) -> AgentExecutionResult:
        tool_name = decision.tool_name or ""
        try:
            self._registry.resolve(tool_name)
        except UnknownToolError:
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.UNKNOWN_TOOL,
                    message=f"Tool 未注册：{tool_name}",
                ),
            )
        except ToolNotAllowedError:
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.TOOL_NOT_ALLOWED,
                    message=f"Phase 1 只允许 READ_ONLY Tool：{tool_name}",
                ),
            )

        handler = self._handlers.get(tool_name)
        if handler is None:
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=0,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.TOOL_EXECUTION_FAILED,
                    message=f"Tool 未配置执行器：{tool_name}",
                ),
            )

        tool_input = ToolInput(tool_name=tool_name, arguments=decision.arguments)
        try:
            tool_result = handler(
                tool_input, ToolContext(run_id=run_id, request_id=request.request_id)
            )
            tool_call_count = 1
        except Exception as exc:
            LOGGER.exception("Tool handler 意外抛错：%s", tool_name)
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=1,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.TOOL_EXECUTION_FAILED,
                    message="工具执行失败",
                    detail=type(exc).__name__,
                ),
            )

        if tool_result.status.value == "failed":
            safe_message = (
                tool_result.error.message
                if tool_result.error is not None
                else "工具执行失败"
            )
            return self._fail(
                run_id=run_id,
                request=request,
                started_at=started_at,
                started_monotonic=started_monotonic,
                decision=decision,
                decision_call_count=decision_call_count,
                tool_call_count=tool_call_count,
                tool_result=tool_result,
                error=AgentExecutionError(
                    code=AgentExecutionErrorCode.TOOL_EXECUTION_FAILED,
                    message=safe_message,
                ),
            )

        return self._completed(
            run_id=run_id,
            request=request,
            started_at=started_at,
            started_monotonic=started_monotonic,
            decision=decision,
            decision_call_count=decision_call_count,
            tool_call_count=tool_call_count,
            tool_result=tool_result,
        )

    def _completed(
        self,
        *,
        run_id: str,
        request: AgentRequest,
        started_at: str,
        started_monotonic: float,
        decision: AgentDecision,
        decision_call_count: int,
        tool_call_count: int,
        tool_result: ToolResult | None,
    ) -> AgentExecutionResult:
        trace = self._build_trace(
            run_id=run_id,
            request=request,
            started_at=started_at,
            started_monotonic=started_monotonic,
            decision=decision,
            decision_call_count=decision_call_count,
            tool_call_count=tool_call_count,
            outcome=AgentExecutionStatus.COMPLETED.value,
            tool_result=tool_result,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.COMPLETED,
            decision=decision,
            tool_called=tool_call_count > 0,
            selected_tool=decision.tool_name if tool_call_count > 0 else None,
            tool_result=tool_result,
            error=None,
            trace=trace,
        )

    def _fail(
        self,
        *,
        run_id: str,
        request: AgentRequest,
        started_at: str,
        started_monotonic: float,
        decision: AgentDecision | None,
        decision_call_count: int,
        tool_call_count: int,
        error: AgentExecutionError,
        tool_result: ToolResult | None = None,
    ) -> AgentExecutionResult:
        trace = self._build_trace(
            run_id=run_id,
            request=request,
            started_at=started_at,
            started_monotonic=started_monotonic,
            decision=decision,
            decision_call_count=decision_call_count,
            tool_call_count=tool_call_count,
            outcome=AgentExecutionStatus.FAILED.value,
            tool_result=tool_result,
            error=error,
        )
        return AgentExecutionResult(
            status=AgentExecutionStatus.FAILED,
            decision=decision
            if decision is not None
            else AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY),
            tool_called=tool_call_count > 0,
            selected_tool=decision.tool_name if decision is not None else None,
            tool_result=tool_result,
            error=error,
            trace=trace,
        )

    @staticmethod
    def _build_trace(
        *,
        run_id: str,
        request: AgentRequest,
        started_at: str,
        started_monotonic: float,
        decision: AgentDecision | None,
        decision_call_count: int,
        tool_call_count: int,
        outcome: str,
        tool_result: ToolResult | None,
        error: AgentExecutionError | None = None,
    ) -> AgentRuntimeTrace:
        return AgentRuntimeTrace(
            run_id=run_id,
            request_id=request.request_id,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
            decision_kind=decision.kind.value if decision is not None else None,
            selected_tool=decision.tool_name if decision is not None else None,
            decision_call_count=decision_call_count,
            tool_call_count=tool_call_count,
            retry_count=0,
            tool_status=tool_result.status.value if tool_result is not None else None,
            outcome=outcome,
            error_code=error.code.value if error is not None else None,
            error_message=error.message if error is not None else None,
        )


__all__ = ["SingleStepAgentExecutor"]
