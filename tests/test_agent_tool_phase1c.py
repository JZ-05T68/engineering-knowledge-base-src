"""Mock-first and fixture-backed tests for the v0.6.0 Phase 1C read-only Tools.

Covers ``inspect_provenance``, ``inspect_source_integrity`` and ``get_evidence``.
The mock section proves adapter mapping with no real services; the integration
section proves the adapters call real existing services through a temporary
SQLite database. No production DB, AI provider, or network is touched.
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
    ToolErrorCode,
    ToolInput,
    ToolResultStatus,
    ToolSideEffect,
    build_phase1_handlers,
    build_phase1_registry,
)
from src.agent.tools.adapters import (
    GET_EVIDENCE_DEFINITION,
    INSPECT_PROVENANCE_DEFINITION,
    INSPECT_SOURCE_INTEGRITY_DEFINITION,
    GetEvidenceAdapter,
    InspectProvenanceAdapter,
    InspectSourceIntegrityAdapter,
)
from src.agent.tools.registry import Phase1ReadOnlyPolicy
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_context import ContextProjectionError
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
    ContextAnchorType,
    ContextItem,
    ContextItemType,
    ContextRelationRef,
    ContextSourceAnchor,
    EvidenceConfirmationStatus,
    EvidenceContextKind,
    EvidenceItem,
    EvidenceTextKind,
    EvidenceType,
    KnowledgeObjectSource,
    KnowledgeObjectSourceType,
    KnowledgeObjectSourceView,
    KnowledgeSourceStatus,
    PageStatus,
    build_stable_id,
)

KB_UUID = "kb-1"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeProjector:
    def __init__(
        self,
        item: ContextItem | None = None,
        error: Exception | None = None,
    ) -> None:
        self.item = item
        self.error = error
        self.calls: list[tuple[ContextItemType, int]] = []

    def project(self, item_type: ContextItemType, local_id: int) -> ContextItem:
        self.calls.append((item_type, local_id))
        if self.error is not None:
            raise self.error
        if self.item is None or self.item.local_id != local_id:
            raise ContextProjectionError(f"目标不存在：{local_id}")
        return self.item


class _FakeIntegrityService:
    def __init__(
        self,
        object_views: tuple[KnowledgeObjectSourceView, ...] = (),
        single_view: KnowledgeObjectSourceView | None = None,
        object_error: Exception | None = None,
        source_error: Exception | None = None,
    ) -> None:
        self.object_views = object_views
        self.single_view = single_view
        self.object_error = object_error
        self.source_error = source_error
        self.calls: list[tuple[str, int]] = []

    def source_views(self, knowledge_object_id: int) -> tuple[KnowledgeObjectSourceView, ...]:
        self.calls.append(("source_views", knowledge_object_id))
        if self.object_error is not None:
            raise self.object_error
        return self.object_views

    def source_view(self, source_id: int) -> KnowledgeObjectSourceView:
        self.calls.append(("source_view", source_id))
        if self.source_error is not None:
            raise self.source_error
        if self.single_view is None or self.single_view.source.id != source_id:
            raise KnowledgeObjectNotFoundError(f"知识对象来源不存在：{source_id}")
        return self.single_view


class _FakeEvidenceService:
    def __init__(self, item: EvidenceItem | None = None, error: Exception | None = None) -> None:
        self.item = item
        self.error = error
        self.calls: list[int] = []

    def get_item(self, item_id: int) -> EvidenceItem | None:
        self.calls.append(item_id)
        if self.error is not None:
            raise self.error
        if self.item is None or self.item.id != item_id:
            return None
        return self.item


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _context_item(
    local_id: int,
    *,
    item_type: ContextItemType = ContextItemType.KNOWLEDGE_OBJECT,
    anchors: tuple[ContextSourceAnchor, ...] = (),
    relations: tuple[ContextRelationRef, ...] = (),
) -> ContextItem:
    return ContextItem(
        type=item_type,
        local_id=local_id,
        stable_id=build_stable_id(KB_UUID, item_type.value, local_id),
        title="测试对象",
        content="",
        kind="concept",
        kind_label="概念",
        status="active",
        status_label="现行",
        importance=None,
        updated_at=datetime(2026, 1, 1),
        revision_ref="第 1 版",
        source_anchors=anchors,
        relation_refs=relations,
    )


def _anchor(
    anchor_type: str,
    anchor_id: int | None,
    label: str,
    *,
    fingerprint_state: str = "not_applicable",
) -> ContextSourceAnchor:
    return ContextSourceAnchor(
        anchor_type=anchor_type,
        anchor_id=anchor_id,
        anchor_label=label,
        fingerprint_state=fingerprint_state,
    )


def _source_view(
    source_link_id: int,
    *,
    source_type: KnowledgeObjectSourceType = KnowledgeObjectSourceType.PAGE,
    source_id: int = 7,
    status: KnowledgeSourceStatus = KnowledgeSourceStatus.VALID,
) -> KnowledgeObjectSourceView:
    source = KnowledgeObjectSource(
        id=source_link_id,
        knowledge_object_id=1,
        source_type=source_type,
        source_id=source_id,
        source_note="来源说明",
        source_fingerprint="a" * 64,
        fingerprint_version=1,
        captured_at=datetime(2026, 1, 1),
        created_at=datetime(2026, 1, 1),
    )
    return KnowledgeObjectSourceView(source=source, status=status)


def _evidence_item(
    local_id: int,
    *,
    evidence_type: EvidenceType = EvidenceType.TEXT_SELECTION,
    confirmation_status: EvidenceConfirmationStatus = EvidenceConfirmationStatus.CONFIRMED,
) -> EvidenceItem:
    return EvidenceItem(
        id=local_id,
        basket_id=1,
        document_id=1,
        page_id=1,
        document_title="测试文档",
        filename="manual.pdf",
        page_number=1,
        review_status=PageStatus.REVIEWED,
        projects=(),
        tags=(),
        evidence_text="选区文本",
        text_kind=EvidenceTextKind.ORIGINAL,
        context="上下文",
        context_kind=EvidenceContextKind.SYSTEM_GENERATED,
        user_note="",
        source_text_sha256="b" * 64,
        source_locator="document_id=1;page_id=1",
        added_at=datetime(2026, 1, 1),
        position=1,
        evidence_type=evidence_type,
        confirmation_status=confirmation_status,
        confirmed_at=(
            datetime(2026, 1, 2)
            if confirmation_status == EvidenceConfirmationStatus.CONFIRMED
            else None
        ),
        region_image_sha256="c" * 64 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_image_width=100 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_image_height=200 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_x0=10 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_y0=20 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_x1=30 if evidence_type is EvidenceType.IMAGE_REGION else None,
        region_y1=40 if evidence_type is EvidenceType.IMAGE_REGION else None,
    )


def _input(tool_name: str, arguments: dict[str, object]) -> ToolInput:
    return ToolInput(tool_name=tool_name, arguments=arguments)


# ---------------------------------------------------------------------------
# inspect_provenance mock tests
# ---------------------------------------------------------------------------


def test_inspect_provenance_knowledge_object_success() -> None:
    item = _context_item(
        1,
        anchors=(
            _anchor(ContextAnchorType.PAGE.value, 7, "页面 7"),
            _anchor(ContextAnchorType.EVIDENCE.value, 8, "证据 8"),
            _anchor(ContextAnchorType.DOCUMENT.value, 1, "文档 1"),
        ),
        relations=(
            ContextRelationRef(
                relation_type="relates_to",
                relation_label="相关",
                direction="outgoing",
                target_stable_id=f"{KB_UUID}:{KNOWLEDGE_OBJECT_STABLE_TYPE}:2",
            ),
        ),
    )
    adapter = InspectProvenanceAdapter(_FakeProjector(item), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["subject_type"] == "knowledge_object"  # type: ignore[index]
    assert len(result.data["source_anchors"]) == 3  # type: ignore[index]
    assert len(result.data["relation_refs"]) == 1  # type: ignore[index]
    stable_ids = [reference.stable_id for reference in result.references]
    assert stable_id in stable_ids
    assert f"{KB_UUID}:{PAGE_STABLE_TYPE}:7" in stable_ids
    assert f"{KB_UUID}:{EVIDENCE_STABLE_TYPE}:8" in stable_ids
    assert f"{KB_UUID}:page:1" not in stable_ids


def test_inspect_provenance_knowledge_memory_success() -> None:
    item = _context_item(
        2,
        item_type=ContextItemType.KNOWLEDGE_MEMORY,
        anchors=(
            _anchor("knowledge_object", 1, "知识对象 1"),
            _anchor(ContextAnchorType.PAGE.value, 7, "页面 7"),
        ),
    )
    adapter = InspectProvenanceAdapter(_FakeProjector(item), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_MEMORY_STABLE_TYPE, 2)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    stable_ids = [reference.stable_id for reference in result.references]
    assert f"{KB_UUID}:{KNOWLEDGE_OBJECT_STABLE_TYPE}:1" in stable_ids
    assert f"{KB_UUID}:{PAGE_STABLE_TYPE}:7" in stable_ids


def test_inspect_provenance_missing_provenance_is_partial() -> None:
    item = _context_item(1, anchors=())
    adapter = InspectProvenanceAdapter(_FakeProjector(item), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert any("没有可追溯的来源锚点" in warning for warning in result.warnings)


def test_inspect_provenance_changed_source_is_partial() -> None:
    item = _context_item(
        1,
        anchors=(_anchor(ContextAnchorType.PAGE.value, 7, "页面 7", fingerprint_state="changed"),),
    )
    adapter = InspectProvenanceAdapter(_FakeProjector(item), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert any("来源已变化" in warning for warning in result.warnings)


def test_inspect_provenance_invalid_input() -> None:
    adapter = InspectProvenanceAdapter(_FakeProjector(), kb_uuid=KB_UUID)

    result = adapter(_input("inspect_provenance", {"stable_id": "bad"}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_inspect_provenance_unknown_stable_id_not_found() -> None:
    adapter = InspectProvenanceAdapter(_FakeProjector(), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 999)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_inspect_provenance_unknown_argument_fails_closed() -> None:
    adapter = InspectProvenanceAdapter(_FakeProjector(), kb_uuid=KB_UUID)

    result = adapter(
        _input("inspect_provenance", {"stable_id": "x", "extra": 1}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_inspect_provenance_service_failure() -> None:
    adapter = InspectProvenanceAdapter(
        _FakeProjector(error=RuntimeError("boom")), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE


def test_inspect_provenance_deterministic_anchor_order() -> None:
    item = _context_item(
        1,
        anchors=(
            _anchor(ContextAnchorType.PAGE.value, 7, "页面 7"),
            _anchor(ContextAnchorType.EVIDENCE.value, 8, "证据 8"),
        ),
    )
    adapter = InspectProvenanceAdapter(_FakeProjector(item), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_provenance", {"stable_id": stable_id}), ToolContext())

    anchor_types = [anchor["anchor_type"] for anchor in result.data["source_anchors"]]  # type: ignore[index]
    assert anchor_types == ["page", "evidence"]


# ---------------------------------------------------------------------------
# inspect_source_integrity mock tests
# ---------------------------------------------------------------------------


def test_source_integrity_valid_object_sources() -> None:
    service = _FakeIntegrityService(
        object_views=(_source_view(1, status=KnowledgeSourceStatus.VALID),)
    )
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["total_sources"] == 1  # type: ignore[index]
    assert result.data["aggregate_state"] == "valid"  # type: ignore[index]
    assert result.references[1].stable_id == f"{KB_UUID}:{KNOWLEDGE_SOURCE_STABLE_TYPE}:1"


def test_source_integrity_changed_source_is_partial() -> None:
    service = _FakeIntegrityService(
        object_views=(_source_view(1, status=KnowledgeSourceStatus.CHANGED),)
    )
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert any("来源已变化" in warning for warning in result.warnings)


def test_source_integrity_missing_backing_source_is_partial() -> None:
    service = _FakeIntegrityService(
        object_views=(_source_view(1, status=KnowledgeSourceStatus.MISSING),)
    )
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert any("来源缺失" in warning for warning in result.warnings)


def test_source_integrity_legacy_unknown_is_partial() -> None:
    service = _FakeIntegrityService(
        object_views=(_source_view(1, status=KnowledgeSourceStatus.UNKNOWN),)
    )
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert any("来源状态未知" in warning for warning in result.warnings)


def test_source_integrity_single_source_by_source_stable_id() -> None:
    service = _FakeIntegrityService(single_view=_source_view(5))
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_SOURCE_STABLE_TYPE, 5)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["total_sources"] == 1  # type: ignore[index]
    assert service.calls == [("source_view", 5)]


def test_source_integrity_source_not_found() -> None:
    service = _FakeIntegrityService()
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_SOURCE_STABLE_TYPE, 999)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_source_integrity_invalid_input() -> None:
    adapter = InspectSourceIntegrityAdapter(_FakeIntegrityService(), kb_uuid=KB_UUID)

    result = adapter(
        _input("inspect_source_integrity", {"stable_id": "not-a-stable-id"}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_source_integrity_unknown_argument_fails_closed() -> None:
    adapter = InspectSourceIntegrityAdapter(_FakeIntegrityService(), kb_uuid=KB_UUID)

    result = adapter(
        _input("inspect_source_integrity", {"stable_id": "x", "refresh": True}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_source_integrity_service_failure() -> None:
    service = _FakeIntegrityService(object_error=RuntimeError("sqlite busy"))
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    result = adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE


def test_source_integrity_does_not_call_refresh_methods() -> None:
    service = _FakeIntegrityService(object_views=(_source_view(1),))
    adapter = InspectSourceIntegrityAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, KNOWLEDGE_OBJECT_STABLE_TYPE, 1)

    adapter(_input("inspect_source_integrity", {"stable_id": stable_id}), ToolContext())

    assert all(call[0] == "source_views" for call in service.calls)


# ---------------------------------------------------------------------------
# get_evidence mock tests
# ---------------------------------------------------------------------------


def test_get_evidence_confirmed_text_selection() -> None:
    service = _FakeEvidenceService(_evidence_item(1))
    adapter = GetEvidenceAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, EVIDENCE_STABLE_TYPE, 1)

    result = adapter(_input("get_evidence", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["confirmed"] is True  # type: ignore[index]
    assert result.data["evidence_type"] == "text_selection"  # type: ignore[index]
    stable_ids = [reference.stable_id for reference in result.references]
    assert stable_id in stable_ids
    assert f"{KB_UUID}:{PAGE_STABLE_TYPE}:1" in stable_ids


def test_get_evidence_confirmed_image_region() -> None:
    service = _FakeEvidenceService(
        _evidence_item(2, evidence_type=EvidenceType.IMAGE_REGION)
    )
    adapter = GetEvidenceAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, EVIDENCE_STABLE_TYPE, 2)

    result = adapter(_input("get_evidence", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["region_metadata"]["x0"] == 10  # type: ignore[index]
    assert result.data["region_metadata"]["image_width"] == 100  # type: ignore[index]


def test_get_evidence_unconfirmed_is_partial() -> None:
    service = _FakeEvidenceService(
        _evidence_item(
            3, confirmation_status=EvidenceConfirmationStatus.UNCONFIRMED
        )
    )
    adapter = GetEvidenceAdapter(service, kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, EVIDENCE_STABLE_TYPE, 3)

    result = adapter(_input("get_evidence", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.PARTIAL
    assert result.data["confirmed"] is False  # type: ignore[index]
    assert any("尚未人工确认" in warning for warning in result.warnings)


def test_get_evidence_missing_not_found() -> None:
    adapter = GetEvidenceAdapter(_FakeEvidenceService(), kb_uuid=KB_UUID)
    stable_id = build_stable_id(KB_UUID, EVIDENCE_STABLE_TYPE, 999)

    result = adapter(_input("get_evidence", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.NOT_FOUND


def test_get_evidence_invalid_identity() -> None:
    adapter = GetEvidenceAdapter(_FakeEvidenceService(), kb_uuid=KB_UUID)

    result = adapter(_input("get_evidence", {"stable_id": "bad"}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_evidence_unknown_argument_fails_closed() -> None:
    adapter = GetEvidenceAdapter(_FakeEvidenceService(), kb_uuid=KB_UUID)

    result = adapter(
        _input("get_evidence", {"stable_id": "x", "include_image": True}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INVALID_INPUT


def test_get_evidence_service_failure() -> None:
    adapter = GetEvidenceAdapter(
        _FakeEvidenceService(error=RuntimeError("sqlite busy")), kb_uuid=KB_UUID
    )
    stable_id = build_stable_id(KB_UUID, EVIDENCE_STABLE_TYPE, 1)

    result = adapter(_input("get_evidence", {"stable_id": stable_id}), ToolContext())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.INTERNAL_FAILURE


# ---------------------------------------------------------------------------
# registry / policy
# ---------------------------------------------------------------------------


def test_phase1_registry_exact_seven_tools() -> None:
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


def test_phase1c_definitions_are_read_only_and_policy_allowed() -> None:
    policy = Phase1ReadOnlyPolicy()
    for definition in (
        INSPECT_PROVENANCE_DEFINITION,
        INSPECT_SOURCE_INTEGRITY_DEFINITION,
        GET_EVIDENCE_DEFINITION,
    ):
        assert definition.side_effect is ToolSideEffect.READ_ONLY
        policy.validate(definition)


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
    source_views = objects.source_views(object_view.knowledge_object.id)
    source_link_id = source_views[0].source.id
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


def test_inspect_provenance_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["inspect_provenance"]
    stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
    )

    result = handler(
        _input("inspect_provenance", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.references[0].stable_id == stable_id
    assert any(
        anchor["anchor_type"] == "page"
        for anchor in result.data["source_anchors"]  # type: ignore[index]
    )


def test_inspect_source_integrity_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["inspect_source_integrity"]
    object_stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
    )
    source_stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_SOURCE_STABLE_TYPE, library.source_link_id
    )

    object_result = handler(
        _input("inspect_source_integrity", {"stable_id": object_stable_id}),
        ToolContext(),
    )
    source_result = handler(
        _input("inspect_source_integrity", {"stable_id": source_stable_id}),
        ToolContext(),
    )

    assert object_result.status is ToolResultStatus.SUCCESS
    assert object_result.data["total_sources"] == 1  # type: ignore[index]
    assert source_result.status is ToolResultStatus.SUCCESS
    assert source_result.data["total_sources"] == 1  # type: ignore[index]


def test_get_evidence_integration(library: SimpleNamespace) -> None:
    handler = library.handlers["get_evidence"]
    stable_id = build_stable_id(
        library.kb_uuid, EVIDENCE_STABLE_TYPE, library.evidence.id
    )

    result = handler(
        _input("get_evidence", {"stable_id": stable_id}),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["confirmed"] is True  # type: ignore[index]
    assert result.references[1].stable_id.startswith(
        f"{library.kb_uuid}:{PAGE_STABLE_TYPE}:"
    )


def test_integration_fingerprint_not_refreshed(library: SimpleNamespace) -> None:
    database = library.database
    with database._connection() as connection:  # noqa: SLF001
        before = connection.execute(
            "SELECT source_fingerprint, captured_at FROM knowledge_object_sources WHERE id = ?",
            (library.source_link_id,),
        ).fetchone()
    handler = library.handlers["inspect_source_integrity"]
    source_stable_id = build_stable_id(
        library.kb_uuid, KNOWLEDGE_SOURCE_STABLE_TYPE, library.source_link_id
    )
    handler(_input("inspect_source_integrity", {"stable_id": source_stable_id}), ToolContext())
    with database._connection() as connection:  # noqa: SLF001
        after = connection.execute(
            "SELECT source_fingerprint, captured_at FROM knowledge_object_sources WHERE id = ?",
            (library.source_link_id,),
        ).fetchone()
    assert before == after


def test_hidden_write_audit_all_seven_tools(library: SimpleNamespace) -> None:
    database_path = Path(library.database.database_path)
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    handlers = library.handlers
    handlers["page_search"](
        _input("page_search", {"query": "电机"}), ToolContext()
    )
    handlers["knowledge_search"](
        _input("knowledge_search", {"query": "PID"}), ToolContext()
    )
    handlers["get_knowledge_object"](
        _input(
            "get_knowledge_object",
            {
                "stable_id": build_stable_id(
                    library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
                )
            },
        ),
        ToolContext(),
    )
    handlers["get_knowledge_memory"](
        _input(
            "get_knowledge_memory",
            {
                "stable_id": build_stable_id(
                    library.kb_uuid,
                    KNOWLEDGE_MEMORY_STABLE_TYPE,
                    library.memory.id,
                )
            },
        ),
        ToolContext(),
    )
    handlers["inspect_provenance"](
        _input(
            "inspect_provenance",
            {
                "stable_id": build_stable_id(
                    library.kb_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, library.object_id
                )
            },
        ),
        ToolContext(),
    )
    handlers["inspect_source_integrity"](
        _input(
            "inspect_source_integrity",
            {
                "stable_id": build_stable_id(
                    library.kb_uuid,
                    KNOWLEDGE_SOURCE_STABLE_TYPE,
                    library.source_link_id,
                )
            },
        ),
        ToolContext(),
    )
    handlers["get_evidence"](
        _input(
            "get_evidence",
            {
                "stable_id": build_stable_id(
                    library.kb_uuid, EVIDENCE_STABLE_TYPE, library.evidence.id
                )
            },
        ),
        ToolContext(),
    )

    after = hashlib.sha256(database_path.read_bytes()).hexdigest()
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


def test_phase1c_adapter_modules_do_not_import_streamlit_pages_or_provider() -> None:
    forbidden_prefixes = ("src.ai.qwen_client", "src.ai.provider")
    for module_name in (
        "src.agent.tools.adapters.provenance",
        "src.agent.tools.adapters.source_integrity",
        "src.agent.tools.adapters.evidence",
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
        assert not any(
            item == prefix or item.startswith(prefix + ".")
            for item in imports
            for prefix in forbidden_prefixes
        ), f"{module_name} 导入了 provider 依赖"
        assert "urllib.request" not in imports
