"""Phase 2D failure-path, privacy, budget, and static invariant tests."""

from __future__ import annotations

import ast
import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from src.agent import (
    AgentDecision,
    AgentDecisionKind,
    AgentRequest,
    FinalAnswerStage,
    SingleStepAgentExecutor,
    SingleStepAgentService,
)
from src.agent.response import AgentResponseErrorCode, AgentResponseStatus
from src.agent.tools import (
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolMetadata,
    ToolReference,
    ToolResult,
    ToolResultStatus,
    build_phase1_registry,
)
from src.agent.tools.adapters import PageSearchAdapter
from src.ai.provider import (
    AIExecutionError,
    AIUnavailableError,
    AuditedAIProvider,
    CompletionResult,
)
from src.ai.rag_answer_service import RagAnswerService
from src.knowledge_context_packager import KnowledgeContextPackager
from src.migrations import SCHEMA_VERSION

KB_UUID = "12345678-1234-1234-1234-123456789abc"
SECRET = (
    "Authorization: Bearer PHASE2D_SYNTHETIC_SECRET "
    "D:\\private\\knowledge.db SELECT * FROM secrets"
)


class _DecisionProvider:
    def __init__(
        self,
        decision: AgentDecision | None = None,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.error = error
        self.calls = 0

    def decide(self, request: AgentRequest) -> AgentDecision:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


class _Handler:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


class _CompletionProvider:
    def __init__(self, result: CompletionResult | Exception | None = None) -> None:
        self.result = result
        self.calls = 0
        self.last_prompt = ""
        self.last_max_completion_tokens: int | None = None

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_max_completion_tokens = max_completion_tokens
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        return CompletionResult(text="可靠回答。依据：【来源 #1】。", model="fake")


class _Ledger:
    def __init__(self) -> None:
        self.records = []

    def record(self, call) -> None:
        self.records.append(call)


class _ExplodingLedger:
    def record(self, call) -> None:
        raise RuntimeError(SECRET)


class _AllowBudget:
    def ensure_allowed(self, capability: str) -> None:
        return None


class _DenyBudget:
    def ensure_allowed(self, capability: str) -> None:
        raise AIUnavailableError("预算拒绝")


class _ExplodingSearch:
    def search(self, query: str, *, limit: int) -> list[object]:
        raise RuntimeError(SECRET)


def _tool_result(
    status: ToolResultStatus = ToolResultStatus.SUCCESS,
    *,
    warnings: tuple[str, ...] = (),
    retryable: bool = False,
    content: str = "可靠资料",
) -> ToolResult:
    if status is ToolResultStatus.EMPTY:
        return ToolResult(
            status=status,
            data={"query": "x", "limit": 20, "total": 0, "results": []},
            metadata=ToolMetadata(tool_name="page_search"),
        )
    if status is ToolResultStatus.FAILED:
        return ToolResult(
            status=status,
            error=ToolError(
                code=ToolErrorCode.INTERNAL_FAILURE,
                message="工具失败",
                retryable=retryable,
            ),
            metadata=ToolMetadata(tool_name="page_search"),
        )
    return ToolResult(
        status=status,
        data={
            "query": "x",
            "limit": 20,
            "total": 1,
            "results": [{"id": 1, "document_title": "资料", "snippet": content}],
        },
        references=(
            ToolReference(
                stable_id=f"{KB_UUID}:page:1",
                anchor_label="页面 1",
            ),
        ),
        warnings=warnings,
        metadata=ToolMetadata(tool_name="page_search"),
    )


def _executor(handler: object) -> SingleStepAgentExecutor:
    registry = build_phase1_registry()
    empty = _Handler(_tool_result(ToolResultStatus.EMPTY))
    handlers = {
        definition.name: handler if definition.name == "page_search" else empty
        for definition in registry.list_definitions()
    }
    return SingleStepAgentExecutor(registry, handlers=handlers)  # type: ignore[arg-type]


def _stage(provider: object) -> FinalAnswerStage:
    return FinalAnswerStage(
        RagAnswerService(provider),  # type: ignore[arg-type]
        packager=KnowledgeContextPackager(kb_uuid=KB_UUID, app_version="test"),
    )


def _call_tool_decision() -> AgentDecision:
    return AgentDecision(
        kind=AgentDecisionKind.CALL_TOOL,
        tool_name="page_search",
        arguments={"query": "x"},
    )


@pytest.mark.parametrize(
    ("status", "warnings", "expected_status", "final_calls"),
    [
        (ToolResultStatus.SUCCESS, (), AgentResponseStatus.COMPLETED, 1),
        (ToolResultStatus.EMPTY, (), AgentResponseStatus.COMPLETED, 0),
        (ToolResultStatus.PARTIAL, ("stale/partial",), AgentResponseStatus.COMPLETED, 1),
        (ToolResultStatus.FAILED, (), AgentResponseStatus.FAILED, 0),
    ],
)
def test_tool_status_failure_path_matrix_stops_without_retry(
    status: ToolResultStatus,
    warnings: tuple[str, ...],
    expected_status: AgentResponseStatus,
    final_calls: int,
) -> None:
    handler = _Handler(
        _tool_result(status, warnings=warnings, retryable=status is ToolResultStatus.FAILED)
    )
    final = _CompletionProvider()
    decision = _DecisionProvider(_call_tool_decision())

    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="查询"), decision
    )

    assert response.status is expected_status
    assert decision.calls == 1
    assert handler.calls == 1
    assert final.calls == final_calls
    assert response.trace is not None
    assert response.trace.decision_call_count == 1
    assert response.trace.tool_call_count == 1
    assert response.trace.retry_count == 0
    if status is ToolResultStatus.PARTIAL:
        assert "stale/partial" in response.warnings


def test_handler_exception_is_safe_in_response_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    handler = _Handler(error=RuntimeError(SECRET))
    final = _CompletionProvider()
    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="查询"), _DecisionProvider(_call_tool_decision())
    )

    assert response.status is AgentResponseStatus.FAILED
    assert handler.calls == 1
    assert final.calls == 0
    assert SECRET not in caplog.text
    assert "private-token" not in caplog.text
    assert "D:\\private" not in caplog.text
    assert "Traceback" not in caplog.text


def test_malformed_handler_return_fails_closed_without_final_call() -> None:
    handler = _Handler({"status": "success"})
    final = _CompletionProvider()

    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="查询"), _DecisionProvider(_call_tool_decision())
    )

    assert response.status is AgentResponseStatus.FAILED
    assert handler.calls == 1
    assert final.calls == 0
    assert response.error is not None
    assert response.error.code is AgentResponseErrorCode.TOOL_FAILED


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(SECRET),
        AIExecutionError(SECRET, error_class="timeout"),
        RuntimeError(SECRET),
        AIUnavailableError("预算拒绝 " + SECRET),
    ],
)
def test_decision_failure_is_one_attempt_and_never_leaks(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    decision = _DecisionProvider(error=error)
    handler = _Handler(_tool_result())
    final = _CompletionProvider()

    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="私人问题"), decision
    )

    assert response.status is AgentResponseStatus.FAILED
    assert decision.calls == 1
    assert handler.calls == 0
    assert final.calls == 0
    assert response.trace is not None
    assert response.trace.decision_call_count == 1
    assert response.trace.tool_call_count == 0
    assert response.trace.retry_count == 0
    assert SECRET not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        AIExecutionError(SECRET, error_class="transport"),
        AIUnavailableError("provider unavailable " + SECRET),
        RuntimeError(SECRET),
    ],
)
def test_final_provider_failure_is_sanitized_and_not_retried(error: Exception) -> None:
    handler = _Handler(_tool_result())
    final = _CompletionProvider(error)
    decision = _DecisionProvider(_call_tool_decision())

    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="查询"), decision
    )

    assert response.status is AgentResponseStatus.FAILED
    assert decision.calls == 1
    assert handler.calls == 1
    assert final.calls == 1
    assert response.error is not None
    assert SECRET not in response.error.message
    assert "private-token" not in response.error.message
    assert response.trace is not None
    assert response.trace.retry_count == 0


def test_final_answer_budget_denial_blocks_network_and_retry() -> None:
    inner = _CompletionProvider()
    ledger = _Ledger()
    audited = AuditedAIProvider(
        inner,
        default_model="qwen",
        default_embedding_model="embedding",
        source_feature="test",
        ledger=ledger,
        budget_guard=_DenyBudget(),
    )
    handler = _Handler(_tool_result())

    response = SingleStepAgentService(_executor(handler), _stage(audited)).run(
        AgentRequest(text="查询"), _DecisionProvider(_call_tool_decision())
    )

    assert response.status is AgentResponseStatus.FAILED
    assert response.error is not None
    assert response.error.code is AgentResponseErrorCode.BUDGET_EXCEEDED
    assert inner.calls == 0
    assert handler.calls == 1
    assert len(ledger.records) == 1
    assert ledger.records[0].status == "rejected"
    assert ledger.records[0].retry_count == 0


def test_context_is_shaped_and_final_output_cap_remains_512() -> None:
    content = "A" * 25_000 + "SHOULD_NOT_REACH_PROMPT"
    handler = _Handler(_tool_result(content=content))
    final = _CompletionProvider()

    response = SingleStepAgentService(_executor(handler), _stage(final)).run(
        AgentRequest(text="查询"), _DecisionProvider(_call_tool_decision())
    )

    assert response.status is AgentResponseStatus.COMPLETED
    assert final.calls == 1
    assert final.last_max_completion_tokens == 512
    assert "（内容过长，已截断。）" in final.last_prompt
    assert "SHOULD_NOT_REACH_PROMPT" not in final.last_prompt


def test_audited_provider_persists_hashes_not_raw_private_prompt() -> None:
    ledger = _Ledger()
    inner = _CompletionProvider()
    audited = AuditedAIProvider(
        inner,
        default_model="qwen",
        default_embedding_model="embedding",
        source_feature="agent_decision",
        ledger=ledger,
        budget_guard=_AllowBudget(),
    )

    audited.complete("private user prompt " + SECRET)

    assert len(ledger.records) == 1
    serialized = repr(asdict(ledger.records[0]))
    assert "private user prompt" not in serialized
    assert "private-token" not in serialized
    assert "Authorization" not in serialized
    assert len(ledger.records[0].prompt_sha256) == 64


def test_audit_ledger_failure_log_is_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    inner = _CompletionProvider()
    audited = AuditedAIProvider(
        inner,
        default_model="qwen",
        default_embedding_model="embedding",
        source_feature="agent_decision",
        ledger=_ExplodingLedger(),
        budget_guard=_AllowBudget(),
    )

    result = audited.complete("private user prompt")

    assert result.text
    assert inner.calls == 1
    assert SECRET not in caplog.text
    assert "Traceback" not in caplog.text


def test_adapter_internal_exception_log_is_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    adapter = PageSearchAdapter(_ExplodingSearch(), kb_uuid=KB_UUID)  # type: ignore[arg-type]

    result = adapter(
        ToolInput(tool_name="page_search", arguments={"query": "x"}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.message == "页面检索执行失败"
    assert SECRET not in caplog.text
    assert "Traceback" not in caplog.text


def test_static_dependency_schema_and_no_persistence_invariants() -> None:
    assert SCHEMA_VERSION == 12
    source_root = Path("src")
    agent_root = source_root / "agent"

    for path in agent_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert "streamlit" not in imports
        assert "src.ai.qwen_client" not in imports

    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        assert not any(
            module == "src.agent" or module.startswith("src.agent.")
            for module in modules
        )

    migration_text = (source_root / "migrations.py").read_text(encoding="utf-8")
    assert "CREATE TABLE agent_runs" not in migration_text
    assert "CREATE TABLE agent_steps" not in migration_text
    assert "CREATE TABLE tool_calls" not in migration_text
