"""Focused tests for the v0.6.0 Phase 2A single-step execution kernel.

The execution kernel is tested with fake decision providers and fake Tool
handlers to prove safety invariants without any real model, network, or
production database. A final integration test wires the kernel to the real
Phase 1 registry with a temporary database.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import (
    AgentDecision,
    AgentDecisionKind,
    AgentExecutionErrorCode,
    AgentExecutionStatus,
    AgentRequest,
    DecisionProvider,
    SingleStepAgentExecutor,
    build_single_step_executor,
)
from src.agent.tools import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolReference,
    ToolResult,
    ToolResultStatus,
    ToolSideEffect,
    build_phase1_registry,
)
from src.agent.tools.adapters import PAGE_SEARCH_DEFINITION
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.models import (
    KnowledgeObjectSourceType,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeDecisionProvider:
    """Configurable fake DecisionProvider recording every call."""

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
        if self.decision is None:
            raise AssertionError("FakeDecisionProvider 未配置 decision")
        return self.decision


class FakeToolHandler:
    """Configurable fake Tool handler recording every call."""

    def __init__(
        self,
        result: ToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.last_input: ToolInput | None = None

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        self.last_input = tool_input
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("FakeToolHandler 未配置 result")
        return self.result


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _read_only_definition(name: str = "page_search") -> ToolDefinition:
    if name == "page_search":
        return PAGE_SEARCH_DEFINITION
    return ToolDefinition(
        name=name,
        description="测试只读工具",
        side_effect=ToolSideEffect.READ_ONLY,
    )


def _write_definition(name: str, side_effect: ToolSideEffect) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="测试写工具",
        side_effect=side_effect,
    )


def _registry_and_handlers(
    definitions: list[ToolDefinition],
    handlers: dict[str, FakeToolHandler],
) -> tuple[object, dict[str, FakeToolHandler]]:
    from src.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry, handlers


def _success_result() -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={"ok": True},
        references=(),
        metadata=None,
    )


def _request(request_id: str = "req-1") -> AgentRequest:
    return AgentRequest(request_id=request_id, text="用户请求")


def _executor(
    registry: object, handlers: dict[str, FakeToolHandler]
) -> SingleStepAgentExecutor:
    return SingleStepAgentExecutor(
        registry,  # type: ignore[arg-type]
        handlers=handlers,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# NO_TOOL / ANSWER_DIRECTLY
# ---------------------------------------------------------------------------


def test_no_tool_executes_zero_tools() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY)
    )

    result = executor.execute(_request(), provider)

    assert provider.calls == 1
    assert handlers["page_search"].calls == 0
    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_called is False
    assert result.tool_result is None
    assert result.error is None
    assert result.trace.tool_call_count == 0
    assert result.trace.retry_count == 0


def test_answer_directly_cannot_smuggle_tool_request() -> None:
    with pytest.raises(ValueError, match="tool_name"):
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY, tool_name="page_search")
    with pytest.raises(ValueError, match="arguments"):
        AgentDecision(
            kind=AgentDecisionKind.ANSWER_DIRECTLY, arguments={"query": "x"}
        )


# ---------------------------------------------------------------------------
# valid CALL_TOOL
# ---------------------------------------------------------------------------


def test_valid_call_tool_executes_exactly_once() -> None:
    handler = FakeToolHandler(_success_result())
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert provider.calls == 1
    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_called is True
    assert result.selected_tool == "page_search"
    assert result.tool_result is not None
    assert result.tool_result.data == {"ok": True}
    assert result.trace.tool_call_count == 1
    assert result.trace.selected_tool == "page_search"


def test_valid_call_tool_preserves_references() -> None:
    reference = ToolReference(stable_id="kb-1:page:1")
    handler = FakeToolHandler(
        ToolResult(
            status=ToolResultStatus.SUCCESS,
            data={"total": 1},
            references=(reference,),
        )
    )
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert result.tool_result is not None
    assert result.tool_result.references[0].stable_id == "kb-1:page:1"


# ---------------------------------------------------------------------------
# unknown / write tools
# ---------------------------------------------------------------------------


def test_unknown_tool_fails_closed() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="imaginary_tool",
            arguments={},
        )
    )

    result = executor.execute(_request(), provider)

    assert provider.calls == 1
    assert handlers["page_search"].calls == 0
    assert result.status is AgentExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentExecutionErrorCode.UNKNOWN_TOOL
    assert result.trace.tool_call_count == 0


@pytest.mark.parametrize(
    ("name", "side_effect"),
    [
        ("write_reversible", ToolSideEffect.WRITE_REVERSIBLE),
        ("write_destructive", ToolSideEffect.WRITE_DESTRUCTIVE),
    ],
)
def test_write_tools_rejected_without_handler_call(
    name: str, side_effect: ToolSideEffect
) -> None:
    handler = FakeToolHandler(_success_result())
    registry, handlers = _registry_and_handlers(
        [_write_definition(name, side_effect)], {name: handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.CALL_TOOL, tool_name=name, arguments={})
    )

    result = executor.execute(_request(), provider)

    assert provider.calls == 1
    assert handler.calls == 0
    assert result.status is AgentExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentExecutionErrorCode.TOOL_NOT_ALLOWED
    assert result.trace.tool_call_count == 0


# ---------------------------------------------------------------------------
# Tool status handling
# ---------------------------------------------------------------------------


def test_tool_invalid_input_is_preserved_and_stops() -> None:
    handler = FakeToolHandler(
        ToolResult(
            status=ToolResultStatus.FAILED,
            error=ToolError(
                code=ToolErrorCode.INVALID_INPUT,
                message="参数错误",
                retryable=False,
            ),
        )
    )
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": ""},
        )
    )

    result = executor.execute(_request(), provider)

    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.FAILED
    assert result.tool_result is not None
    assert result.tool_result.error is not None
    assert result.tool_result.error.code is ToolErrorCode.INVALID_INPUT
    assert result.trace.tool_call_count == 1


def test_tool_empty_stops_without_fallback() -> None:
    handler = FakeToolHandler(
        ToolResult(status=ToolResultStatus.EMPTY, data={"results": []})
    )
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "不存在"},
        )
    )

    result = executor.execute(_request(), provider)

    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_result is not None
    assert result.tool_result.status is ToolResultStatus.EMPTY
    assert result.trace.tool_call_count == 1


def test_tool_partial_stops_and_preserves_warnings() -> None:
    handler = FakeToolHandler(
        ToolResult(
            status=ToolResultStatus.PARTIAL,
            data={"hits": []},
            warnings=("来源已变化",),
        )
    )
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_result is not None
    assert result.tool_result.warnings == ("来源已变化",)
    assert result.trace.tool_call_count == 1


def test_tool_failed_retryable_does_not_retry() -> None:
    handler = FakeToolHandler(
        ToolResult(
            status=ToolResultStatus.FAILED,
            error=ToolError(
                code=ToolErrorCode.INTERNAL_FAILURE,
                message="工具内部失败",
                retryable=True,
            ),
        )
    )
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.FAILED
    assert result.tool_result is not None
    assert result.tool_result.error is not None
    assert result.tool_result.error.retryable is True
    assert result.trace.tool_call_count == 1
    assert result.trace.retry_count == 0


# ---------------------------------------------------------------------------
# failure boundaries
# ---------------------------------------------------------------------------


def test_decision_provider_failure_stops_with_zero_tools() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(error=RuntimeError("provider down"))

    result = executor.execute(_request(), provider)

    assert provider.calls == 1
    assert handlers["page_search"].calls == 0
    assert result.status is AgentExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentExecutionErrorCode.DECISION_PROVIDER_FAILED
    assert result.error.detail == "RuntimeError"
    assert result.trace.tool_call_count == 0
    assert result.trace.retry_count == 0


def test_handler_unexpected_exception_fails_closed() -> None:
    handler = FakeToolHandler(error=RuntimeError("boom"))
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": handler}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert handler.calls == 1
    assert result.status is AgentExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentExecutionErrorCode.TOOL_EXECUTION_FAILED
    assert result.error.message == "工具执行失败"
    assert result.trace.tool_call_count == 1
    assert "Traceback" not in result.error.message
    assert "C:\\" not in result.error.message


def test_provider_returning_non_decision_is_invalid() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)

    class _BadProvider:
        def decide(self, request: AgentRequest) -> object:
            return {"kind": "call_tool"}

    result = executor.execute(_request(), _BadProvider())  # type: ignore[arg-type]

    assert result.status is AgentExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code is AgentExecutionErrorCode.INVALID_DECISION
    assert result.trace.tool_call_count == 0


# ---------------------------------------------------------------------------
# decision contract type-level safety
# ---------------------------------------------------------------------------


def test_decision_contract_rejects_multiple_tool_calls() -> None:
    with pytest.raises(TypeError):
        AgentDecision(  # type: ignore[call-arg]
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={},
            tool_calls=[{"tool_name": "page_search"}, {"tool_name": "knowledge_search"}],
        )


def test_decision_contract_rejects_extra_fields() -> None:
    with pytest.raises(TypeError):
        AgentDecision(  # type: ignore[call-arg]
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={},
            run_shell=True,
        )


def test_decision_contract_requires_tool_name_for_call_tool() -> None:
    with pytest.raises(ValueError, match="tool_name"):
        AgentDecision(kind=AgentDecisionKind.CALL_TOOL)


def test_decision_provider_protocol_is_runtime_checkable() -> None:
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY)
    )
    assert isinstance(provider, DecisionProvider)


# ---------------------------------------------------------------------------
# determinism and trace
# ---------------------------------------------------------------------------


def test_execution_is_deterministic() -> None:
    def _run() -> tuple[str, int, object | None]:
        handler = FakeToolHandler(_success_result())
        registry, handlers = _registry_and_handlers(
            [_read_only_definition()], {"page_search": handler}
        )
        executor = _executor(registry, handlers)
        provider = FakeDecisionProvider(
            AgentDecision(
                kind=AgentDecisionKind.CALL_TOOL,
                tool_name="page_search",
                arguments={"query": "电机"},
            )
        )
        result = executor.execute(_request(), provider)
        data = result.tool_result.data if result.tool_result else None
        return result.status.value, result.trace.tool_call_count, data

    first = _run()
    second = _run()
    assert first == second


def test_runtime_trace_no_tool_records_counts() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY)
    )

    result = executor.execute(_request("req-abc"), provider)

    assert result.trace.request_id == "req-abc"
    assert result.trace.decision_call_count == 1
    assert result.trace.tool_call_count == 0
    assert result.trace.decision_kind == "answer_directly"
    assert result.trace.outcome == "completed"


def test_runtime_trace_success_records_selected_tool() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert result.trace.selected_tool == "page_search"
    assert result.trace.tool_status == "success"
    assert result.trace.error_code is None


def test_runtime_trace_failed_records_error_without_secret() -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(error=RuntimeError("secret-key-leak"))

    result = executor.execute(_request(), provider)

    assert result.trace.error_code == "decision_provider_failed"
    assert "secret-key-leak" not in (result.trace.error_message or "")
    assert result.trace.retry_count == 0


def test_trace_is_in_memory_only(tmp_path: Path) -> None:
    registry, handlers = _registry_and_handlers(
        [_read_only_definition()], {"page_search": FakeToolHandler(_success_result())}
    )
    executor = _executor(registry, handlers)
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY)
    )

    executor.execute(_request(), provider)

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# production registry + integration
# ---------------------------------------------------------------------------


def test_production_registry_still_exactly_seven_tools() -> None:
    registry = build_phase1_registry()

    assert [item.name for item in registry.list_definitions()] == [
        "get_evidence",
        "get_knowledge_memory",
        "get_knowledge_object",
        "inspect_provenance",
        "inspect_source_integrity",
        "knowledge_search",
        "page_search",
    ]
    assert all(
        item.side_effect is ToolSideEffect.READ_ONLY
        for item in registry.list_definitions()
    )


@pytest.fixture()
def library(tmp_path: Path) -> SimpleNamespace:
    database = Database(tmp_path / "knowledge.db")
    source_path = tmp_path / "raw" / "manual.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"pdf")
    image_path = tmp_path / "pages" / "1" / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")
    document = database.create_document(
        title="测试文档",
        filename="manual.pdf",
        source_path=source_path,
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="电机控制 PID 整定经验",
    )
    objects = KnowledgeObjectService(database)
    object_view = objects.create(
        kind="concept",
        title="PID 整定",
        content="比例积分微分参数整定经验",
        epistemic_basis="personal_experience",
        source_links=((KnowledgeObjectSourceType.PAGE.value, page.id, "来自页面"),),
    )
    memories = KnowledgeMemoryService(database)
    memory = memories.create_entry(
        kind="experience",
        title="编码器异常",
        content="编码器中断配置错误",
        knowledge_object_id=object_view.knowledge_object.id,
        document_id=document.id,
        page_id=page.id,
    )
    evidence_service = EvidenceBasketService(database)
    evidence = evidence_service.add_item(
        document_id=document.id, page_id=page.id, evidence_text="电机控制 PID 整定经验"
    )
    evidence = evidence_service.set_confirmation(evidence.id, True)
    return SimpleNamespace(
        database=database,
        object_id=object_view.knowledge_object.id,
        memory=memory,
        evidence=evidence,
        kb_uuid=database.get_knowledge_base_uuid(),
    )


def test_production_executor_integration_page_search(library: SimpleNamespace) -> None:
    executor = build_single_step_executor(library.database)
    provider = FakeDecisionProvider(
        AgentDecision(
            kind=AgentDecisionKind.CALL_TOOL,
            tool_name="page_search",
            arguments={"query": "电机"},
        )
    )

    result = executor.execute(_request(), provider)

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_called is True
    assert result.selected_tool == "page_search"
    assert result.tool_result is not None
    assert result.tool_result.data["total"] == 1  # type: ignore[index]
    assert result.trace.tool_call_count == 1


def test_production_executor_integration_no_tool(library: SimpleNamespace) -> None:
    executor = build_single_step_executor(library.database)
    provider = FakeDecisionProvider(
        AgentDecision(kind=AgentDecisionKind.ANSWER_DIRECTLY)
    )

    result = executor.execute(_request(), provider)

    assert result.status is AgentExecutionStatus.COMPLETED
    assert result.tool_called is False
    assert result.trace.tool_call_count == 0


# ---------------------------------------------------------------------------
# UI / provider / network independence
# ---------------------------------------------------------------------------


def _module_imports(module_name: str) -> tuple[str, ...]:
    module = pytest.importorskip(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return tuple(imports)


def test_execution_modules_do_not_import_ui_or_provider() -> None:
    forbidden_prefixes = ("src.ai.qwen_client", "src.ai.provider", "streamlit", "pages")
    for module_name in (
        "src.agent.execution",
        "src.agent.execution.contracts",
        "src.agent.execution.executor",
    ):
        imports = _module_imports(module_name)
        assert not any(
            item == prefix or item.startswith(prefix + ".")
            for item in imports
            for prefix in forbidden_prefixes
        ), f"{module_name} 导入了 UI/provider 依赖"
        assert "urllib.request" not in imports
