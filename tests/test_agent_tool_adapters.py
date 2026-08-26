"""Mock-first and fixture-backed tests for the v0.6.0 Phase 1B read-only Tool Adapters.

The mock section proves adapter argument/error/result mapping with no real
services. The integration section proves the adapters call the real existing
services through a temporary SQLite database. No test touches the production
database, a real AI provider, or the network.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.tools import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolInput,
    ToolNotAllowedError,
    ToolResultStatus,
    ToolSideEffect,
    build_phase1_handlers,
    build_phase1_registry,
)
from src.agent.tools.adapters import (
    GET_EVIDENCE_DEFINITION,
    GET_KNOWLEDGE_MEMORY_DEFINITION,
    GET_KNOWLEDGE_OBJECT_DEFINITION,
    INSPECT_PROVENANCE_DEFINITION,
    INSPECT_SOURCE_INTEGRITY_DEFINITION,
    KNOWLEDGE_SEARCH_DEFINITION,
    PAGE_SEARCH_DEFINITION,
    KnowledgeMemoryAdapter,
    KnowledgeObjectAdapter,
    KnowledgeSearchAdapter,
    PageSearchAdapter,
)
from src.agent.tools.registry import Phase1ReadOnlyPolicy
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import (
    KnowledgeObjectNotFoundError,
    KnowledgeObjectService,
)
from src.models import (
    EVIDENCE_STABLE_TYPE,
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    KNOWLEDGE_OBJECT_STABLE_TYPE,
    KNOWLEDGE_SOURCE_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    KnowledgeAuthorship,
    KnowledgeConfirmationStatus,
    KnowledgeEpistemicBasis,
    KnowledgeLifecycle,
    KnowledgeMemoryEntry,
    KnowledgeMemoryEntryKind,
    KnowledgeMemoryStatus,
    KnowledgeObject,
    KnowledgeObjectKind,
    KnowledgeObjectSource,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeObjectView,
    KnowledgeSearchResult,
    KnowledgeSearchResultType,
    KnowledgeSourceStatus,
    NoteImportance,
    PageStatus,
    SearchResult,
    build_stable_id,
)

KB_UUID = "kb-1"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeSearchService:
    def __init__(
        self,
        results: list[SearchResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 20, **kwargs: object) -> list[SearchResult]:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.results


class _FakeKnowledgeSearchService:
    def __init__(
        self,
        results: tuple[KnowledgeSearchResult, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, limit: int = 20, **kwargs: object
    ) -> tuple[KnowledgeSearchResult, ...]:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.results


class _FakeKnowledgeObjectService:
    def __init__(
        self,
        view: KnowledgeObjectView | None = None,
        error: Exception | None = None,
    ) -> None:
        self.view = view
        self.error = error

    def get_view(self, knowledge_object_id: int) -> KnowledgeObjectView:
        if self.error is not None:
            raise self.error
        if self.view is None or self.view.knowledge_object.id != knowledge_object_id:
            raise KnowledgeObjectNotFoundError(
                f"知识对象不存在：{knowledge_object_id}"
            )
        return self.view


class _FakeKnowledgeMemoryService:
    def __init__(
        self, entry: KnowledgeMemoryEntry | None = None, error: Exception | None = None
    ) -> None:
        self.entry = entry
        self.error = error

    def get(self, entry_id: int) -> KnowledgeMemoryEntry | None:
        if self.error is not None:
            raise self.error
        if self.entry is None or self.entry.id != entry_id:
            return None
        return self.entry


# ---------------------------------------------------------------------------
# model builders
# ---------------------------------------------------------------------------


def _search_result(
    page_id: int,
    *,
    document_id: int = 1,
    title: str = "测试文档",
    page_number: int = 1,
    snippet: str = "命中片段",
) -> SearchResult:
    return SearchResult(
        page_id=page_id,
        document_id=document_id,
        document_title=title,
        filename="manual.pdf",
        page_number=page_number,
        image_path=Path("data/pages/1/1.png"),
        content="内容",
        snippet=snippet,
        rank=1.0,
        status=PageStatus.REVIEWED,
        tags=(),
        projects=(),
    )


def _knowledge_search_result(
    result_type: KnowledgeSearchResultType,
    local_id: int,
    title: str,
) -> KnowledgeSearchResult:
    stable_id = build_stable_id(KB_UUID, result_type.value, local_id)
    return KnowledgeSearchResult(
        result_type=result_type,
        id=local_id,
        stable_id=stable_id,
        title=title,
        content="内容",
        snippet="片段",
        status="active",
        status_label="现行",
        kind="concept",
        kind_label="概念",
    )


def _knowledge_object(
    local_id: int,
    *,
    title: str = "PID 整定",
    content: str = "比例积分微分参数整定经验",
) -> KnowledgeObject:
    return KnowledgeObject(
        id=local_id,
        kind=KnowledgeObjectKind.CONCEPT,
        authorship=KnowledgeAuthorship.USER,
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
        title=title,
        content=content,
        importance=NoteImportance.NORMAL,
        lifecycle=KnowledgeLifecycle.ACTIVE,
        superseded_by_ko_id=None,
        confirmation_status=KnowledgeConfirmationStatus.CONFIRMED,
        confirmed_at=datetime(2026, 1, 1),
        confirmed_revision=1,
        current_revision=1,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )


def _source_view(
    source_id: int,
    *,
    source_type: KnowledgeObjectSourceType = KnowledgeObjectSourceType.PAGE,
    status: KnowledgeSourceStatus = KnowledgeSourceStatus.VALID,
) -> KnowledgeObjectSourceView:
    source = KnowledgeObjectSource(
        id=1,
        knowledge_object_id=1,
        source_type=source_type,
        source_id=source_id,
        source_note="来源说明",
    )
    return KnowledgeObjectSourceView(source=source, status=status)


def _object_view(
    local_id: int,
    *,
    sources: tuple[KnowledgeObjectSourceView, ...] = (),
) -> KnowledgeObjectView:
    return KnowledgeObjectView(
        knowledge_object=_knowledge_object(local_id),
        sources=sources,
        outgoing_relations=(),
        incoming_relations=(),
    )


def _memory_entry(
    local_id: int,
    *,
    knowledge_object_id: int | None = 1,
    page_id: int | None = 1,
) -> KnowledgeMemoryEntry:
    return KnowledgeMemoryEntry(
        id=local_id,
        kind=KnowledgeMemoryEntryKind.EXPERIENCE,
        title="编码器异常",
        content="编码器中断配置错误",
        root_cause="中断优先级错误",
        lesson="优先检查时序",
        knowledge_object_id=knowledge_object_id,
        document_id=1,
        page_id=page_id,
        status=KnowledgeMemoryStatus.ACTIVE,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
        content_revision=2,
        outcome="已解决",
        context_conditions="高速控制",
    )


def _input(tool_name: str, arguments: dict[str, object]) -> ToolInput:
    return ToolInput(tool_name=tool_name, arguments=arguments)


# ---------------------------------------------------------------------------
# page_search mock tests
# ---------------------------------------------------------------------------


def test_page_search_success() -> None:
    service = _FakeSearchService(
        [
            _search_result(1, page_number=1),
            _search_result(2, page_number=2),
        ]
    )
    adapter = PageSearchAdapter(service, kb_uuid=KB_UUID)

    result = adapter(_input("page_search", {"query": "电机"}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.error is None
    assert result.data["total"] == 2  # type: ignore[index]
    assert result.references[0].stable_id == f"{KB_UUID}:{PAGE_STABLE_TYPE}:1"
    assert result.references[1].anchor_label == "测试文档 · 第 2 页"


def test_page_search_empty() -> None:
    adapter = PageSearchAdapter(_FakeSearchService(), kb_uuid=KB_UUID)

    result = adapter(_input("page_search", {"query": "不存在词"}), ToolContext())

    assert result.status is ToolResultStatus.EMPTY
    assert result.error is None
    assert result.data["results"] == []  # type: ignore[index]


def test_page_search_invalid_query() -> None:
    adapter = PageSearchAdapter(_FakeSearchService(), kb_uuid=KB_UUID)

    result = adapter(_input("page_search", {"query": "   "}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_page_search_unknown_argument_fails_closed() -> None:
    adapter = PageSearchAdapter(_FakeSearchService(), kb_uuid=KB_UUID)

    result = adapter(
        _input("page_search", {"query": "电机", "imaginary_option": True}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT
    assert "imaginary_option" in result.error.message


def test_page_search_service_failure_maps_to_internal_failure() -> None:
    service = _FakeSearchService(error=RuntimeError("db down"))
    adapter = PageSearchAdapter(service, kb_uuid=KB_UUID)

    result = adapter(_input("page_search", {"query": "电机"}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE
    assert result.error.message == "页面检索执行失败"
    assert result.error.detail == "RuntimeError"
    assert "detail" not in result.to_dict()["error"]


def test_page_search_ordering_retained() -> None:
    service = _FakeSearchService(
        [_search_result(2, page_number=2), _search_result(1, page_number=1)]
    )
    adapter = PageSearchAdapter(service, kb_uuid=KB_UUID)

    result = adapter(_input("page_search", {"query": "电机"}), ToolContext())

    page_ids = [item["page_id"] for item in result.data["results"]]  # type: ignore[index]
    assert page_ids == [2, 1]


def test_page_search_limit_is_passed_and_bounded() -> None:
    service = _FakeSearchService([_search_result(1)])
    adapter = PageSearchAdapter(service, kb_uuid=KB_UUID)

    adapter(_input("page_search", {"query": "电机", "limit": 5}), ToolContext())

    assert service.calls == [("电机", 5)]

    for bad_limit in (0, 101, "5", True):
        result = adapter(
            _input("page_search", {"query": "电机", "limit": bad_limit}),
            ToolContext(),
        )
        assert result.status is ToolResultStatus.FAILED
        assert result.error is not None
        assert result.error.code is ToolErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# knowledge_search mock tests
# ---------------------------------------------------------------------------


def test_knowledge_search_mixed_results() -> None:
    service = _FakeKnowledgeSearchService(
        (
            _knowledge_search_result(KnowledgeSearchResultType.KNOWLEDGE_OBJECT, 1, "PID"),
            _knowledge_search_result(KnowledgeSearchResultType.KNOWLEDGE_MEMORY, 2, "编码器"),
        )
    )
    adapter = KnowledgeSearchAdapter(service)

    result = adapter(_input("knowledge_search", {"query": "PID"}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    types = [item["result_type"] for item in result.data["results"]]  # type: ignore[index]
    assert types == ["knowledge_object", "knowledge_memory"]
    assert result.references[0].stable_id.endswith(":knowledge_object:1")
    assert result.references[1].stable_id.endswith(":knowledge_memory:2")


def test_knowledge_search_empty() -> None:
    adapter = KnowledgeSearchAdapter(_FakeKnowledgeSearchService())

    result = adapter(_input("knowledge_search", {"query": "不存在"}), ToolContext())

    assert result.status is ToolResultStatus.EMPTY
    assert result.error is None


def test_knowledge_search_invalid_input() -> None:
    adapter = KnowledgeSearchAdapter(_FakeKnowledgeSearchService())

    result = adapter(_input("knowledge_search", {"query": ""}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_knowledge_search_unknown_argument_fails_closed() -> None:
    adapter = KnowledgeSearchAdapter(_FakeKnowledgeSearchService())

    result = adapter(
        _input("knowledge_search", {"query": "PID", "semantic": True}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_knowledge_search_service_failure() -> None:
    adapter = KnowledgeSearchAdapter(
        _FakeKnowledgeSearchService(error=RuntimeError("fts down"))
    )

    result = adapter(_input("knowledge_search", {"query": "PID"}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE
    assert result.error.message == "知识检索执行失败"


# ---------------------------------------------------------------------------
# get_knowledge_object mock tests
# ---------------------------------------------------------------------------


def test_get_knowledge_object_success() -> None:
    view = _object_view(
        1,
        sources=(
            _source_view(7, source_type=KnowledgeObjectSourceType.PAGE),
            _source_view(8, source_type=KnowledgeObjectSourceType.EVIDENCE),
            _source_view(9, source_type=KnowledgeObjectSourceType.DOCUMENT),
        ),
    )
    adapter = KnowledgeObjectAdapter(_FakeKnowledgeObjectService(view), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["title"] == "PID 整定"  # type: ignore[index]
    assert result.data["lifecycle"] == "active"  # type: ignore[index]
    stable_ids = [reference.stable_id for reference in result.references]
    assert stable_id in stable_ids
    assert f"{KB_UUID}:{PAGE_STABLE_TYPE}:7" in stable_ids
    assert f"{KB_UUID}:evidence:8" in stable_ids
    assert f"{KB_UUID}:page:9" not in stable_ids


def test_get_knowledge_object_not_found() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 999)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_get_knowledge_object_invalid_stable_id() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(), kb_uuid=KB_UUID
    )

    result = adapter(
        _input("get_knowledge_object", {"stable_id": "not-a-stable-id"}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_object_wrong_type() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_MEMORY_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_object_wrong_kb_uuid() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id("other-kb", KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_get_knowledge_object_partial_on_changed_source() -> None:
    view = _object_view(
        1,
        sources=(_source_view(7, status=KnowledgeSourceStatus.CHANGED),),
    )
    adapter = KnowledgeObjectAdapter(_FakeKnowledgeObjectService(view), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.PARTIAL
    assert any("来源已变化" in warning for warning in result.warnings)


def test_get_knowledge_object_unknown_argument_fails_closed() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(), kb_uuid=KB_UUID
    )

    result = adapter(
        _input("get_knowledge_object", {"stable_id": "x", "extra": 1}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_object_service_failure() -> None:
    adapter = KnowledgeObjectAdapter(
        _FakeKnowledgeObjectService(error=RuntimeError("sqlite busy")),
        kb_uuid=KB_UUID,
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE


# ---------------------------------------------------------------------------
# get_knowledge_memory mock tests
# ---------------------------------------------------------------------------


def test_get_knowledge_memory_success() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(_memory_entry(1)), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_MEMORY_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["title"] == "编码器异常"  # type: ignore[index]
    assert result.data["content_revision"] == 2  # type: ignore[index]
    stable_ids = [reference.stable_id for reference in result.references]
    assert stable_id in stable_ids
    assert f"{KB_UUID}:{KNOWLEDGE_OBJECT_STABLE_TYPE}:1" in stable_ids
    assert f"{KB_UUID}:{PAGE_STABLE_TYPE}:1" in stable_ids


def test_get_knowledge_memory_not_found() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_MEMORY_STABLE_TYPE, 999)

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_get_knowledge_memory_invalid_stable_id() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(), kb_uuid=KB_UUID
    )

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": "bad"}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_memory_wrong_type() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_memory_unknown_argument_fails_closed() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(), kb_uuid=KB_UUID
    )

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": "x", "extra": 1}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_knowledge_memory_service_failure() -> None:
    adapter = KnowledgeMemoryAdapter(
        _FakeKnowledgeMemoryService(error=RuntimeError("sqlite busy")),
        kb_uuid=KB_UUID,
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_MEMORY_STABLE_TYPE, 1)

    result = adapter(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE


# ---------------------------------------------------------------------------
# Phase 1 registry / policy
# ---------------------------------------------------------------------------


def test_phase1_registry_exact_deterministic_list() -> None:
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


def test_phase1_registry_has_no_forbidden_tools() -> None:
    registry = build_phase1_registry()
    names = {item.name for item in registry.list_definitions()}

    assert names == {
        "page_search",
        "knowledge_search",
        "get_knowledge_object",
        "get_knowledge_memory",
        "inspect_provenance",
        "inspect_source_integrity",
        "get_evidence",
    }
    assert not names & {
        "rag_answer",
        "confirm_evidence",
        "write_memory",
        "create_knowledge_object",
        "reindex",
        "ai_ledger",
    }


def test_phase1_all_definitions_are_read_only() -> None:
    policy = Phase1ReadOnlyPolicy()
    for definition in (
        PAGE_SEARCH_DEFINITION,
        KNOWLEDGE_SEARCH_DEFINITION,
        GET_KNOWLEDGE_OBJECT_DEFINITION,
        GET_KNOWLEDGE_MEMORY_DEFINITION,
        INSPECT_PROVENANCE_DEFINITION,
        INSPECT_SOURCE_INTEGRITY_DEFINITION,
        GET_EVIDENCE_DEFINITION,
    ):
        assert definition.side_effect is ToolSideEffect.READ_ONLY
        policy.validate(definition)


def test_phase1_registry_resolves_all_seven_tools() -> None:
    registry = build_phase1_registry()

    for name in (
        "page_search",
        "knowledge_search",
        "get_knowledge_object",
        "get_knowledge_memory",
        "inspect_provenance",
        "inspect_source_integrity",
        "get_evidence",
    ):
        assert registry.resolve(name).name == name


def test_fake_write_tool_rejected_by_phase1_registry() -> None:
    registry = build_phase1_registry()
    write_definition = ToolDefinition(
        name="fake_write",
        description="测试写工具",
        side_effect=ToolSideEffect.WRITE_REVERSIBLE,
    )
    registry.register(write_definition)

    with pytest.raises(ToolNotAllowedError, match="READ_ONLY"):
        registry.resolve("fake_write")


# ---------------------------------------------------------------------------
# fixture-backed integration tests
# ---------------------------------------------------------------------------


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
        source_links=(
            (KnowledgeObjectSourceType.PAGE.value, page.id, "来自页面"),
        ),
    )
    memories = KnowledgeMemoryService(database)
    memory = memories.create_entry(
        kind="experience",
        title="编码器异常",
        content="编码器中断配置错误",
        root_cause="中断优先级错误",
        lesson="优先检查时序",
        knowledge_object_id=object_view.knowledge_object.id,
        document_id=document.id,
        page_id=page.id,
    )
    evidence_service = EvidenceBasketService(database)
    evidence = evidence_service.add_item(
        document_id=document.id,
        page_id=page.id,
        evidence_text="电机控制 PID 整定经验",
    )
    evidence = evidence_service.set_confirmation(evidence.id, True)
    source_link_id = objects.source_views(object_view.knowledge_object.id)[0].source.id
    return SimpleNamespace(
        database=database,
        document=document,
        page=page,
        object_id=object_view.knowledge_object.id,
        memory=memory,
        evidence=evidence,
        source_link_id=source_link_id,
        kb_uuid=database.get_knowledge_base_uuid(),
        handlers=build_phase1_handlers(database),
    )


def test_page_search_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["page_search"]

    found = handler(
        _input("page_search", {"query": "电机"}), ToolContext()
    )
    assert found.status is ToolResultStatus.SUCCESS
    assert found.data["total"] == 1  # type: ignore[index]
    assert found.references[0].stable_id.startswith(
        f"{library.kb_uuid}:{PAGE_STABLE_TYPE}:"
    )

    empty = handler(
        _input("page_search", {"query": "完全不存在的词"}), ToolContext()
    )
    assert empty.status is ToolResultStatus.EMPTY


def test_knowledge_search_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["knowledge_search"]

    objects = handler(_input("knowledge_search", {"query": "PID"}), ToolContext())
    assert objects.status is ToolResultStatus.SUCCESS
    assert any(
        item["result_type"] == "knowledge_object"
        for item in objects.data["results"]  # type: ignore[index]
    )

    memories = handler(
        _input("knowledge_search", {"query": "编码器"}), ToolContext()
    )
    assert memories.status is ToolResultStatus.SUCCESS
    assert any(
        item["result_type"] == "knowledge_memory"
        for item in memories.data["results"]  # type: ignore[index]
    )


def test_get_knowledge_object_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["get_knowledge_object"]
    stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
    )

    result = handler(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["title"] == "PID 整定"  # type: ignore[index]
    assert result.references[0].stable_id == stable_id


def test_get_knowledge_object_integration_not_found(library: SimpleNamespace) -> None:
    handler = library.handlers["get_knowledge_object"]
    stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, 9999
    )

    result = handler(
        _input("get_knowledge_object", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_get_knowledge_memory_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["get_knowledge_memory"]
    stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_MEMORY_STABLE_TYPE, library.memory.id
    )

    result = handler(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["title"] == "编码器异常"  # type: ignore[index]
    assert result.references[0].stable_id == stable_id


def test_get_knowledge_memory_integration_not_found(library: SimpleNamespace) -> None:
    handler = library.handlers["get_knowledge_memory"]
    stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_MEMORY_STABLE_TYPE, 9999
    )

    result = handler(
        _input("get_knowledge_memory", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def _phase1_smoke_input(
    name: str, library: SimpleNamespace
) -> ToolInput:
    if name == "page_search":
        return _input(name, {"query": "电机"})
    if name == "knowledge_search":
        return _input(name, {"query": "PID"})
    if name == "get_knowledge_object":
        return _input(
            name,
            {
                "stable_id": build_stable_id(
                    library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
                )
            },
        )
    if name == "get_knowledge_memory":
        return _input(
            name,
            {
                "stable_id": build_stable_id(
                    library.kb_uuid,
                    KNOWLEDGE_MEMORY_STABLE_TYPE,
                    library.memory.id,
                )
            },
        )
    if name == "inspect_provenance":
        return _input(
            name,
            {
                "stable_id": build_stable_id(
                    library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
                )
            },
        )
    if name == "inspect_source_integrity":
        return _input(
            name,
            {
                "stable_id": build_stable_id(
                    library.kb_uuid,
                    KNOWLEDGE_SOURCE_STABLE_TYPE,
                    library.source_link_id,
                )
            },
        )
    return _input(
        name,
        {
            "stable_id": build_stable_id(
                library.kb_uuid, EVIDENCE_STABLE_TYPE, library.evidence.id
            )
        },
    )


def test_phase1_programmatic_smoke(library: SimpleNamespace) -> None:
    registry = build_phase1_registry()
    for name in (
        "page_search",
        "knowledge_search",
        "get_knowledge_object",
        "get_knowledge_memory",
        "inspect_provenance",
        "inspect_source_integrity",
        "get_evidence",
    ):
        assert registry.resolve(name).name == name
        result = library.handlers[name](
            _phase1_smoke_input(name, library), ToolContext()
        )
        assert result.status in (ToolResultStatus.SUCCESS, ToolResultStatus.EMPTY)


def test_hidden_write_audit_database_unchanged(library: SimpleNamespace) -> None:
    database_path = Path(library.database.database_path)
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    results: list[ToolResultStatus] = []
    for name in (
        "page_search",
        "knowledge_search",
        "get_knowledge_object",
        "get_knowledge_memory",
        "inspect_provenance",
        "inspect_source_integrity",
        "get_evidence",
    ):
        result = library.handlers[name](
            _phase1_smoke_input(name, library), ToolContext()
        )
        results.append(result.status)

    after = hashlib.sha256(database_path.read_bytes()).hexdigest()

    assert results == [ToolResultStatus.SUCCESS] * 7
    assert before == after


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


def test_adapter_modules_do_not_import_streamlit_or_pages() -> None:
    for module_name in (
        "src.agent.tools.adapters",
        "src.agent.tools.adapters._common",
        "src.agent.tools.adapters.page_search",
        "src.agent.tools.adapters.knowledge_search",
        "src.agent.tools.adapters.knowledge_read",
        "src.agent.tools.bootstrap",
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


def test_adapter_modules_do_not_import_provider_or_network_transport() -> None:
    forbidden_prefixes = ("src.ai.qwen_client", "src.ai.provider")
    for module_name in (
        "src.agent.tools.adapters",
        "src.agent.tools.adapters._common",
        "src.agent.tools.adapters.page_search",
        "src.agent.tools.adapters.knowledge_search",
        "src.agent.tools.adapters.knowledge_read",
        "src.agent.tools.bootstrap",
    ):
        imports = _module_imports(module_name)
        assert not any(
            item == prefix or item.startswith(prefix + ".")
            for item in imports
            for prefix in forbidden_prefixes
        ), f"{module_name} 导入了 provider 依赖"
        assert "urllib.request" not in imports
