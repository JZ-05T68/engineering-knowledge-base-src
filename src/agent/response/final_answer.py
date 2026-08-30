"""Final Answer Stage (v0.6.0 Phase 2C).

This component is a thin orchestration layer between the Phase 2A execution
result and the existing audited RAG Answer chain:

- SUCCESS / PARTIAL ToolResult → ToolResultContextMapper →
  KnowledgeContextPackage → RagAnswerService (max one completion);
- EMPTY / FAILED ToolResult → deterministic no-evidence / structured failure
  (zero Final Answer model calls);
- ANSWER_DIRECTLY → deterministic no-evidence response (zero model calls)
  because the existing RAG contract requires grounded context.

It never retries, never repairs citations, never selects a second Tool, and
never fabricates references.
"""

from __future__ import annotations

from src.agent.execution.contracts import (
    AgentDecisionKind,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentRequest,
)
from src.agent.response.contracts import (
    AgentResponse,
    AgentResponseError,
    AgentResponseErrorCode,
    AgentResponseStatus,
)
from src.agent.response.tool_context import ToolResultContextMapper
from src.agent.tools.contracts import ToolResultStatus
from src.ai.provider import (
    AIBudgetExceededError,
    AIExecutionError,
    AIUnavailableError,
)
from src.ai.rag_answer_service import (
    RagAnswerError,
    RagAnswerErrorCode,
    RagAnswerService,
)
from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackager,
)

DEFAULT_FINAL_ANSWER_MAX_OUTPUT_TOKENS = 512
DEFAULT_FINAL_ANSWER_SOURCE_FEATURE = "agent_final_answer"

_NO_EVIDENCE_MESSAGE = "没有在当前知识库中找到可支持该问题的资料。"
_ANSWER_DIRECTLY_MESSAGE = "该请求不需要调用知识库工具，无法提供基于知识库的答案。"

__all__ = [
    "DEFAULT_FINAL_ANSWER_MAX_OUTPUT_TOKENS",
    "DEFAULT_FINAL_ANSWER_SOURCE_FEATURE",
    "FinalAnswerStage",
]


class FinalAnswerStage:
    """Turn one AgentExecutionResult into a validated AgentResponse."""

    def __init__(
        self,
        rag_answer_service: RagAnswerService | None = None,
        *,
        packager: KnowledgeContextPackager | None = None,
        model: str | None = None,
        max_completion_tokens: int = DEFAULT_FINAL_ANSWER_MAX_OUTPUT_TOKENS,
        source_feature: str = DEFAULT_FINAL_ANSWER_SOURCE_FEATURE,
    ) -> None:
        self._rag = rag_answer_service
        self._packager = packager or KnowledgeContextPackager()
        self._mapper = ToolResultContextMapper()
        self._model = model
        self._max_completion_tokens = max_completion_tokens
        self._source_feature = source_feature

    def answer(
        self, request: AgentRequest, execution: AgentExecutionResult
    ) -> AgentResponse:
        """Produce the structured final response for one execution result."""
        if execution.status is AgentExecutionStatus.FAILED:
            return self._execution_failure(execution)

        if execution.decision.kind is AgentDecisionKind.ANSWER_DIRECTLY:
            return self._no_evidence_response(
                _ANSWER_DIRECTLY_MESSAGE, trace=execution.trace
            )

        tool_result = execution.tool_result
        if tool_result is None:
            return self._no_evidence_response(
                _NO_EVIDENCE_MESSAGE, trace=execution.trace
            )

        if tool_result.status is ToolResultStatus.EMPTY:
            return self._no_evidence_response(
                _NO_EVIDENCE_MESSAGE,
                warnings=tool_result.warnings,
                trace=execution.trace,
            )

        if tool_result.status is ToolResultStatus.FAILED:
            message = (
                tool_result.error.message
                if tool_result.error is not None
                else "工具执行失败"
            )
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.TOOL_FAILED,
                    message=message,
                ),
                trace=execution.trace,
            )

        if self._rag is None:
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.PROVIDER_UNAVAILABLE,
                    message="Final Answer 服务未配置。",
                ),
                trace=execution.trace,
            )

        try:
            package = self._mapper.build(
                tool_result,
                question=request.text,
                packager=self._packager,
            )
        except KnowledgeContextError:
            return self._no_evidence_response(
                _NO_EVIDENCE_MESSAGE,
                warnings=tool_result.warnings,
                trace=execution.trace,
            )

        try:
            output = self._rag.answer(
                request.text,
                package,
                model=self._model,
                source_feature=self._source_feature,
                max_completion_tokens=self._max_completion_tokens,
            )
        except RagAnswerError as exc:
            if exc.code is RagAnswerErrorCode.EMPTY_CONTEXT:
                return self._no_evidence_response(
                    _NO_EVIDENCE_MESSAGE,
                    warnings=tool_result.warnings,
                    trace=execution.trace,
                )
            if exc.code is RagAnswerErrorCode.CITATION_INVALID:
                return AgentResponse(
                    status=AgentResponseStatus.FAILED,
                    answer="",
                    grounded=False,
                    warnings=tool_result.warnings,
                    error=AgentResponseError(
                        code=AgentResponseErrorCode.CITATION_INVALID,
                        message=str(exc),
                    ),
                    trace=execution.trace,
                )
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.INTERNAL_FAILURE,
                    message=str(exc),
                ),
                trace=execution.trace,
            )
        except AIBudgetExceededError:
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.BUDGET_EXCEEDED,
                    message="Final Answer 调用被预算限制拒绝。",
                ),
                trace=execution.trace,
            )
        except AIUnavailableError:
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.PROVIDER_UNAVAILABLE,
                    message="Final Answer 服务不可用。",
                ),
                trace=execution.trace,
            )
        except AIExecutionError as exc:
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.FINAL_ANSWER_FAILED,
                    message="Final Answer 模型调用失败。",
                    detail=exc.error_class,
                ),
                trace=execution.trace,
            )
        except Exception as exc:
            return AgentResponse(
                status=AgentResponseStatus.FAILED,
                answer="",
                grounded=False,
                warnings=tool_result.warnings,
                error=AgentResponseError(
                    code=AgentResponseErrorCode.INTERNAL_FAILURE,
                    message="Final Answer 执行失败。",
                    detail=type(exc).__name__,
                ),
                trace=execution.trace,
            )

        warnings = tuple(tool_result.warnings) + output.warnings
        return AgentResponse(
            status=AgentResponseStatus.COMPLETED,
            answer=output.answer,
            grounded=True,
            citations=output.answer_citations,
            context_stable_ids=output.context_stable_ids,
            warnings=warnings,
            trace=execution.trace,
            token_usage=output.token_usage,
            model=output.model,
        )

    def _execution_failure(self, execution: AgentExecutionResult) -> AgentResponse:
        error = execution.error
        message = error.message if error is not None else "Agent 执行失败"
        code = AgentResponseErrorCode.INTERNAL_FAILURE
        if error is not None and error.code.value in {
            "unknown_tool",
            "tool_not_allowed",
            "tool_execution_failed",
        }:
            code = AgentResponseErrorCode.TOOL_FAILED
        return AgentResponse(
            status=AgentResponseStatus.FAILED,
            answer="",
            grounded=False,
            error=AgentResponseError(
                code=code,
                message=message,
                detail=error.code.value if error is not None else None,
            ),
            trace=execution.trace,
        )

    def _no_evidence_response(
        self,
        message: str,
        *,
        warnings: tuple[str, ...] = (),
        trace=None,
    ) -> AgentResponse:
        return AgentResponse(
            status=AgentResponseStatus.COMPLETED,
            answer=message,
            grounded=False,
            warnings=warnings,
            trace=trace,
        )
