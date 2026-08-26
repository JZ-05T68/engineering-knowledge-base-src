"""Focused mock-first tests for the v0.6.0 Phase 1A Tool Contract.

These tests are offline-only: they never construct a provider transport, never
read an API key, and never perform network I/O. They cover the frozen contract
surface, registry policy, immutability, and UI/provider/database independence.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from src.agent.tools import (
    MAX_DESCRIPTION_LENGTH,
    DuplicateToolError,
    Phase1ReadOnlyPolicy,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolErrorCode,
    ToolHandler,
    ToolInput,
    ToolMetadata,
    ToolNotAllowedError,
    ToolReference,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    ToolSideEffect,
    UnknownToolError,
)

# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------


def make_read_only(name: str = "page_search", **kwargs: object) -> ToolDefinition:
    """Build a minimal valid READ_ONLY ToolDefinition for tests."""
    return ToolDefinition(
        name=name,
        description="只读搜索页面",
        side_effect=ToolSideEffect.READ_ONLY,
        **kwargs,
    )


def make_write(name: str = "fake_write") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="测试用写工具",
        side_effect=ToolSideEffect.WRITE_REVERSIBLE,
    )


def make_destructive(name: str = "fake_destructive") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="测试用破坏性工具",
        side_effect=ToolSideEffect.WRITE_DESTRUCTIVE,
    )


def make_result(status: ToolResultStatus, **kwargs: object) -> ToolResult:
    return ToolResult(status=status, **kwargs)


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


# ---------------------------------------------------------------------------
# side-effect classification
# ---------------------------------------------------------------------------


def test_side_effect_enum_is_stable_and_serializable() -> None:
    assert [item.value for item in ToolSideEffect] == [
        "read_only",
        "write_reversible",
        "write_destructive",
    ]
    assert ToolSideEffect("read_only") is ToolSideEffect.READ_ONLY


def test_phase1_policy_accepts_read_only_only() -> None:
    policy = Phase1ReadOnlyPolicy()
    assert policy.is_allowed(ToolSideEffect.READ_ONLY)
    assert not policy.is_allowed(ToolSideEffect.WRITE_REVERSIBLE)
    assert not policy.is_allowed(ToolSideEffect.WRITE_DESTRUCTIVE)


def test_phase1_policy_validate_rejects_write_and_destructive() -> None:
    policy = Phase1ReadOnlyPolicy()
    policy.validate(make_read_only())
    with pytest.raises(ToolNotAllowedError):
        policy.validate(make_write())
    with pytest.raises(ToolNotAllowedError):
        policy.validate(make_destructive())


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------


def test_tool_definition_valid() -> None:
    definition = ToolDefinition(
        name="knowledge_search",
        description="在本地知识库中执行离线全文检索。",
        side_effect=ToolSideEffect.READ_ONLY,
        input_schema={"query": {"type": "string"}, "limit": {"type": "integer"}},
        timeout_seconds=30,
    )
    assert definition.name == "knowledge_search"
    assert definition.side_effect is ToolSideEffect.READ_ONLY
    assert dict(definition.input_schema) == {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
    }
    assert definition.timeout_seconds == 30.0


def test_tool_definition_empty_name_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ToolDefinition(name="", description="x", side_effect=ToolSideEffect.READ_ONLY)


def test_tool_definition_whitespace_name_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ToolDefinition(name="   ", description="x", side_effect=ToolSideEffect.READ_ONLY)


@pytest.mark.parametrize(
    "name",
    [
        "PageSearch",
        "page-search",
        "page search",
        "page_search_",
        "1page_search",
        "页面搜索",
        "page.search",
    ],
)
def test_tool_definition_invalid_name_formats_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="snake_case"):
        ToolDefinition(name=name, description="x", side_effect=ToolSideEffect.READ_ONLY)


def test_tool_definition_empty_description_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ToolDefinition(
            name="page_search", description="", side_effect=ToolSideEffect.READ_ONLY
        )


def test_tool_definition_overlong_description_rejected() -> None:
    with pytest.raises(ValueError, match="不能超过"):
        ToolDefinition(
            name="page_search",
            description="x" * (MAX_DESCRIPTION_LENGTH + 1),
            side_effect=ToolSideEffect.READ_ONLY,
        )


def test_tool_definition_unicode_description_allowed() -> None:
    definition = ToolDefinition(
        name="page_search",
        description="在本地 PDF 页面中检索：支持全文与图像文本。",
        side_effect=ToolSideEffect.READ_ONLY,
    )
    assert "PDF" in definition.description


def test_tool_definition_equality_and_repr_are_stable() -> None:
    first = make_read_only(input_schema={"limit": "int"})
    second = make_read_only(input_schema={"limit": "int"})
    assert first == second
    assert "ToolDefinition" in repr(first)
    assert repr(first) == repr(second)


def test_tool_definition_is_frozen() -> None:
    definition = make_read_only()
    with pytest.raises(dataclasses.FrozenInstanceError):
        definition.name = "renamed"  # type: ignore[misc]


def test_tool_definition_input_schema_is_isolated_from_caller_dict() -> None:
    source = {"limit": "integer"}
    definition = make_read_only(input_schema=source)
    source["limit"] = "string"
    assert dict(definition.input_schema) == {"limit": "integer"}


def test_tool_definition_input_schema_mapping_is_read_only() -> None:
    definition = make_read_only(input_schema={"limit": "integer"})
    with pytest.raises(TypeError):
        definition.input_schema["limit"] = "string"  # type: ignore[index]


def test_tool_definition_rejects_non_positive_timeout() -> None:
    for timeout in (0, -1, True):
        with pytest.raises(ValueError, match="timeout_seconds"):
            make_read_only(timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_get() -> None:
    registry = ToolRegistry()
    definition = make_read_only()
    registry.register(definition)
    assert registry.get("page_search") is definition
    assert registry.contains("page_search")
    assert not registry.contains("unknown")


def test_registry_register_trims_lookup_whitespace() -> None:
    registry = ToolRegistry()
    registry.register(make_read_only())
    assert registry.get("  page_search  ") is not None


def test_registry_duplicate_register_fails_closed() -> None:
    registry = ToolRegistry()
    registry.register(make_read_only())
    with pytest.raises(DuplicateToolError, match="禁止覆盖"):
        registry.register(make_read_only())


def test_registry_unknown_tool_raises_typed_error() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError, match="未注册"):
        registry.get("missing_tool")
    with pytest.raises(UnknownToolError, match="未注册"):
        registry.resolve("missing_tool")


def test_registry_empty_listing_is_deterministic_empty() -> None:
    assert ToolRegistry().list_definitions() == ()


def test_registry_listing_is_sorted_not_insertion_ordered() -> None:
    registry = ToolRegistry()
    registry.register(make_read_only(name="zeta_tool"))
    registry.register(make_read_only(name="alpha_tool"))
    registry.register(make_read_only(name="beta_tool"))
    names = [item.name for item in registry.list_definitions()]
    assert names == ["alpha_tool", "beta_tool", "zeta_tool"]


def test_registry_names_are_case_sensitive() -> None:
    registry = ToolRegistry()
    registry.register(make_read_only(name="page_search"))
    assert registry.contains("page_search")
    assert not registry.contains("Page_Search")
    with pytest.raises(UnknownToolError):
        registry.get("Page_Search")


def test_registry_resolve_accepts_read_only() -> None:
    registry = ToolRegistry()
    registry.register(make_read_only())
    assert registry.resolve("page_search").name == "page_search"


def test_registry_resolve_rejects_write_tools_fail_closed() -> None:
    registry = ToolRegistry()
    registry.register(make_write())
    with pytest.raises(ToolNotAllowedError, match="READ_ONLY"):
        registry.resolve("fake_write")


def test_registry_resolve_rejects_destructive_tools_fail_closed() -> None:
    registry = ToolRegistry()
    registry.register(make_destructive())
    with pytest.raises(ToolNotAllowedError, match="READ_ONLY"):
        registry.resolve("fake_destructive")


def test_registry_get_does_not_apply_policy() -> None:
    registry = ToolRegistry()
    registry.register(make_write())
    assert registry.get("fake_write").side_effect is ToolSideEffect.WRITE_REVERSIBLE


def test_registry_rejects_non_definition_register() -> None:
    with pytest.raises(TypeError, match="ToolDefinition"):
        ToolRegistry().register(object())  # type: ignore[arg-type]


def test_registry_custom_policy_can_allow_more() -> None:
    allow_write = Phase1ReadOnlyPolicy(
        allowed_side_effects=frozenset(
            {ToolSideEffect.READ_ONLY, ToolSideEffect.WRITE_REVERSIBLE}
        )
    )
    registry = ToolRegistry(policy=allow_write)
    registry.register(make_write())
    assert registry.resolve("fake_write").name == "fake_write"


# ---------------------------------------------------------------------------
# ToolInput / ToolContext immutability
# ---------------------------------------------------------------------------


def test_tool_input_valid() -> None:
    tool_input = ToolInput(tool_name="page_search", arguments={"query": "电机"})
    assert tool_input.tool_name == "page_search"
    assert dict(tool_input.arguments) == {"query": "电机"}


def test_tool_input_is_isolated_from_caller_dict() -> None:
    source = {"query": "原始"}
    tool_input = ToolInput(tool_name="page_search", arguments=source)
    source["query"] = "篡改"
    assert dict(tool_input.arguments) == {"query": "原始"}


def test_tool_input_arguments_are_read_only() -> None:
    tool_input = ToolInput(tool_name="page_search", arguments={"query": "x"})
    with pytest.raises(TypeError):
        tool_input.arguments["query"] = "y"  # type: ignore[index]


def test_tool_input_is_frozen() -> None:
    tool_input = ToolInput(tool_name="page_search")
    with pytest.raises(dataclasses.FrozenInstanceError):
        tool_input.tool_name = "other"  # type: ignore[misc]


def test_tool_input_invalid_tool_name_rejected() -> None:
    with pytest.raises(ValueError, match="snake_case"):
        ToolInput(tool_name="BadName")


def test_tool_input_has_no_shared_mutable_default() -> None:
    first = ToolInput(tool_name="page_search")
    second = ToolInput(tool_name="page_search")
    assert dict(first.arguments) == {}
    assert dict(second.arguments) == {}
    assert first.arguments is not second.arguments


def test_tool_context_minimal_and_frozen() -> None:
    context = ToolContext(run_id="run-1", request_id="req-1", deadline_epoch_ms=123)
    assert context.run_id == "run-1"
    assert context.deadline_epoch_ms == 123
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.run_id = "other"  # type: ignore[misc]


def test_tool_context_defaults_are_none() -> None:
    context = ToolContext()
    assert context.run_id is None
    assert context.request_id is None
    assert context.deadline_epoch_ms is None


def test_tool_context_rejects_invalid_deadline() -> None:
    with pytest.raises(ValueError, match="deadline_epoch_ms"):
        ToolContext(deadline_epoch_ms=0)
    with pytest.raises(ValueError, match="deadline_epoch_ms"):
        ToolContext(deadline_epoch_ms=-5)
    with pytest.raises(ValueError, match="deadline_epoch_ms"):
        ToolContext(deadline_epoch_ms=True)


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


def test_tool_result_success() -> None:
    result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={"hits": [1, 2]},
        metadata=ToolMetadata(tool_name="page_search", duration_ms=12),
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.error is None
    assert result.to_dict()["status"] == "success"


def test_tool_result_empty_is_legal_success() -> None:
    result = ToolResult(status=ToolResultStatus.EMPTY, data=())
    assert result.status is ToolResultStatus.EMPTY
    assert result.error is None


def test_tool_result_partial_requires_warnings() -> None:
    result = ToolResult(
        status=ToolResultStatus.PARTIAL,
        data={"hits": [1]},
        warnings=("部分来源缺失",),
    )
    assert result.warnings == ("部分来源缺失",)
    with pytest.raises(ValueError, match="warning"):
        ToolResult(status=ToolResultStatus.PARTIAL, data={})


def test_tool_result_failed_requires_error() -> None:
    error = ToolError(
        code=ToolErrorCode.NOT_FOUND,
        message="目标不存在",
        retryable=False,
    )
    result = ToolResult(status=ToolResultStatus.FAILED, data=None, error=error)
    assert result.error is error
    with pytest.raises(ValueError, match="error"):
        ToolResult(status=ToolResultStatus.FAILED)


def test_tool_result_non_failed_cannot_carry_error() -> None:
    error = ToolError(code=ToolErrorCode.NOT_FOUND, message="x")
    with pytest.raises(ValueError, match="不允许携带 error"):
        ToolResult(status=ToolResultStatus.SUCCESS, data=1, error=error)


def test_tool_result_warnings_list_is_copied_to_tuple() -> None:
    warnings = ["w1", "w2"]
    result = ToolResult(status=ToolResultStatus.PARTIAL, data={}, warnings=warnings)
    warnings.append("w3")
    assert result.warnings == ("w1", "w2")


def test_tool_result_references_support_citation_lineage() -> None:
    reference = ToolReference(
        stable_id="kb-1:knowledge_object:42",
        anchor_label="知识对象 42",
        fingerprint_state="valid",
    )
    result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={"title": "轴承选型"},
        references=(reference,),
    )
    assert result.references[0].stable_id == "kb-1:knowledge_object:42"
    assert result.to_dict()["references"][0]["stable_id"] == "kb-1:knowledge_object:42"


def test_tool_reference_requires_stable_id() -> None:
    with pytest.raises(ValueError, match="stable_id"):
        ToolReference(stable_id="")


def test_tool_result_empty_vs_failed_are_distinct() -> None:
    empty = ToolResult(status=ToolResultStatus.EMPTY, data=())
    failed = ToolResult(
        status=ToolResultStatus.FAILED,
        error=ToolError(code=ToolErrorCode.INTERNAL_FAILURE, message="内部错误"),
    )
    assert empty.status is not failed.status
    assert empty.error is None
    assert failed.error is not None


# ---------------------------------------------------------------------------
# ToolError
# ---------------------------------------------------------------------------


def test_tool_error_codes_are_frozen_closed_set() -> None:
    assert [item.value for item in ToolErrorCode] == [
        "tool_unavailable",
        "invalid_input",
        "not_found",
        "empty_result",
        "stale_source",
        "missing_source",
        "provider_unavailable",
        "timeout",
        "budget_exceeded",
        "internal_failure",
        "citation_invalid",
    ]


def test_tool_error_structured_retryable_metadata() -> None:
    error = ToolError(
        code=ToolErrorCode.BUDGET_EXCEEDED,
        message="预算已超限",
        retryable=False,
        metadata={"capability": "completion"},
    )
    assert error.code is ToolErrorCode.BUDGET_EXCEEDED
    assert error.retryable is False
    assert dict(error.metadata) == {"capability": "completion"}


def test_tool_error_message_required() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ToolError(code=ToolErrorCode.INTERNAL_FAILURE, message="")


def test_tool_error_safe_representation_omits_detail_by_default() -> None:
    error = ToolError(
        code=ToolErrorCode.INTERNAL_FAILURE,
        message="工具执行失败",
        retryable=True,
        detail="C:\\secret\\traceback 信息",
        metadata={"source": "test"},
    )
    public = error.to_dict()
    assert public == {
        "code": "internal_failure",
        "message": "工具执行失败",
        "retryable": True,
        "metadata": {"source": "test"},
    }
    assert "detail" not in public
    assert "secret" not in public


def test_tool_error_to_dict_can_include_detail_for_audit() -> None:
    error = ToolError(
        code=ToolErrorCode.TIMEOUT,
        message="工具超时",
        retryable=True,
        detail="transport retry exhausted",
    )
    assert error.to_dict(include_detail=True)["detail"] == "transport retry exhausted"


def test_tool_error_metadata_is_isolated() -> None:
    source = {"retry_count": 1}
    error = ToolError(code=ToolErrorCode.TIMEOUT, message="超时", metadata=source)
    source["retry_count"] = 99
    assert dict(error.metadata) == {"retry_count": 1}


# ---------------------------------------------------------------------------
# ToolHandler protocol (minimal executable boundary, no executor)
# ---------------------------------------------------------------------------


class _FakeReadOnlyHandler:
    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            data={"tool_name": tool_input.tool_name, "run_id": context.run_id},
        )


def test_tool_handler_protocol_is_expressible_without_executor() -> None:
    handler: ToolHandler = _FakeReadOnlyHandler()
    result = handler(
        ToolInput(tool_name="page_search"),
        ToolContext(run_id="run-1"),
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"tool_name": "page_search", "run_id": "run-1"}


def test_tool_handler_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeReadOnlyHandler(), ToolHandler)


# ---------------------------------------------------------------------------
# UI / provider / database independence and no real API transport
# ---------------------------------------------------------------------------


def test_agent_tool_modules_do_not_import_streamlit_or_pages() -> None:
    for module_name in (
        "src.agent",
        "src.agent.tools",
        "src.agent.tools.contracts",
        "src.agent.tools.registry",
    ):
        imports = _module_imports(module_name)
        assert not any(
            item == "streamlit" or item.startswith("streamlit.")
            for item in imports
        ), f"{module_name} 导入了 Streamlit"
        assert not any(
            item == "pages" or item.startswith("pages.")
            for item in imports
        ), f"{module_name} 导入了 pages"


def test_agent_tool_modules_do_not_import_provider_or_database() -> None:
    forbidden_prefixes = (
        "src.ai.qwen_client",
        "src.ai.provider",
        "src.database",
    )
    for module_name in (
        "src.agent",
        "src.agent.tools",
        "src.agent.tools.contracts",
        "src.agent.tools.registry",
    ):
        imports = _module_imports(module_name)
        assert not any(
            item == prefix or item.startswith(prefix + ".")
            for item in imports
            for prefix in forbidden_prefixes
        ), f"{module_name} 导入了 provider/database 依赖"


def test_agent_tool_modules_have_no_rag_answer_tool() -> None:
    module = pytest.importorskip("src.agent.tools.contracts")
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "ToolDefinition":
                for keyword in node.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        assert keyword.value.value != "rag_answer"
    assert "RAG_ANSWER" not in source


def test_no_network_transport_imported_in_contract_path() -> None:
    assert "urllib.request" not in _module_imports("src.agent.tools.contracts")
    assert "urllib.request" not in _module_imports("src.agent.tools.registry")


# ---------------------------------------------------------------------------
# deterministic serialization
# ---------------------------------------------------------------------------


def test_contract_types_serialize_to_plain_structures() -> None:
    definition = make_read_only()
    tool_input = ToolInput(tool_name="page_search", arguments={"query": "电机"})
    context = ToolContext(run_id="run-1")
    error = ToolError(code=ToolErrorCode.NOT_FOUND, message="未找到")
    result = ToolResult(
        status=ToolResultStatus.FAILED,
        error=error,
        metadata=ToolMetadata(tool_name="page_search"),
    )
    assert definition.to_dict()["name"] == "page_search"
    assert tool_input.to_dict()["arguments"] == {"query": "电机"}
    assert context.to_dict()["run_id"] == "run-1"
    assert result.to_dict()["error"]["code"] == "not_found"
