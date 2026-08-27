"""Focused tests for the Phase 2C ToolResult → context projection.

All tests are offline and construct ToolResults directly. No real AI provider,
network, or production database is used.
"""

from __future__ import annotations

import pytest

from src.agent.response import ToolResultContextMapper
from src.agent.tools import (
    ToolMetadata,
    ToolReference,
    ToolResult,
    ToolResultStatus,
)
from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackager,
)

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _page_ref(local_id: int = 1) -> ToolReference:
    return ToolReference(
        stable_id=f"{KB_UUID}:page:{local_id}",
        anchor_label=f"页面 {local_id}",
    )


def _ko_ref(local_id: int = 1) -> ToolReference:
    return ToolReference(
        stable_id=f"{KB_UUID}:knowledge_object:{local_id}",
        anchor_label=f"知识对象 {local_id}",
    )


def _packager() -> KnowledgeContextPackager:
    return KnowledgeContextPackager(kb_uuid=KB_UUID, app_version="test")


def _mapper() -> ToolResultContextMapper:
    return ToolResultContextMapper()


def _search_result_data(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"query": "x", "limit": 20, "total": len(rows), "results": rows}


def test_page_search_results_projected_in_reference_order() -> None:
    references = (_page_ref(1), _page_ref(2))
    data = _search_result_data(
        [
            {"id": 1, "document_title": "文档A", "snippet": "电机控制"},
            {"id": 2, "document_title": "文档B", "snippet": "PCB 设计"},
        ]
    )
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=references,
        metadata=ToolMetadata(tool_name="page_search"),
    )

    package = _mapper().build(tool_result, question="问题", packager=_packager())

    assert [item.stable_id for item in package.items] == [
        f"{KB_UUID}:page:1",
        f"{KB_UUID}:page:2",
    ]
    assert package.items[0].content == "电机控制"
    assert package.items[0].title == "文档A"
    assert package.citations[0][0] == f"{KB_UUID}:page:1"


def test_knowledge_search_results_projected_with_stable_ids() -> None:
    ko = _ko_ref(3)
    memory_ref = ToolReference(
        stable_id=f"{KB_UUID}:knowledge_memory:4", anchor_label="记忆 4"
    )
    data = _search_result_data(
        [
            {
                "stable_id": ko.stable_id,
                "title": "PID 整定",
                "snippet": "比例积分微分",
                "result_type": "knowledge_object",
            },
            {
                "stable_id": memory_ref.stable_id,
                "title": "编码器异常",
                "snippet": "编码器中断配置错误",
                "result_type": "knowledge_memory",
            },
        ]
    )
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(ko, memory_ref),
        metadata=ToolMetadata(tool_name="knowledge_search"),
    )

    package = _mapper().build(tool_result, question="问题", packager=_packager())

    assert [item.stable_id for item in package.items] == [
        ko.stable_id,
        memory_ref.stable_id,
    ]
    assert package.items[1].type.value == "knowledge_memory"


def test_single_knowledge_object_uses_content() -> None:
    reference = _ko_ref(1)
    data = {
        "stable_id": reference.stable_id,
        "title": "PID 整定",
        "content": "比例积分微分参数整定经验",
        "status": "active",
        "status_label": "现行",
    }
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(reference,),
        metadata=ToolMetadata(tool_name="get_knowledge_object"),
    )

    package = _mapper().build(tool_result, question="问题", packager=_packager())

    assert len(package.items) == 1
    assert package.items[0].content == "比例积分微分参数整定经验"
    assert package.items[0].source_anchors


def test_evidence_projected_from_evidence_text() -> None:
    reference = ToolReference(
        stable_id=f"{KB_UUID}:evidence:7", anchor_label="证据 7"
    )
    data = {
        "stable_id": reference.stable_id,
        "id": 7,
        "document_title": "手册",
        "evidence_text": "PHASE2C_OK 对应验证值",
    }
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(reference,),
        metadata=ToolMetadata(tool_name="get_evidence"),
    )

    package = _mapper().build(tool_result, question="问题", packager=_packager())

    assert package.items[0].content == "PHASE2C_OK 对应验证值"
    assert package.items[0].type.value == "evidence"


def test_provenance_uses_structured_summary_when_no_content() -> None:
    reference = _ko_ref(5)
    data = {
        "stable_id": reference.stable_id,
        "subject_type": "knowledge_object",
        "title": "PID 整定",
        "status": "active",
        "revision_ref": "第 2 版",
        "source_anchors": [
            {
                "anchor_type": "page",
                "anchor_id": 1,
                "anchor_label": "页面 1",
                "fingerprint_state": "valid",
            }
        ],
        "relation_refs": [],
    }
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(reference,),
        metadata=ToolMetadata(tool_name="inspect_provenance"),
    )

    package = _mapper().build(tool_result, question="问题", packager=_packager())

    assert package.items[0].content
    assert "PID 整定" in package.items[0].content
    assert "页面 1" in package.items[0].content


def test_unsupported_stable_type_raises_no_evidence() -> None:
    reference = ToolReference(
        stable_id=f"{KB_UUID}:knowledge_source:1", anchor_label="来源 1"
    )
    data = {
        "stable_id": reference.stable_id,
        "subject_type": "knowledge_source",
        "aggregate_state": "valid",
        "sources": [],
    }
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(reference,),
        metadata=ToolMetadata(tool_name="inspect_source_integrity"),
    )

    with pytest.raises(KnowledgeContextError, match="空上下文"):
        _mapper().build(tool_result, question="问题", packager=_packager())


def test_mismatched_results_and_references_fails_closed() -> None:
    data = _search_result_data([{"id": 1, "snippet": "x"}])
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=(_page_ref(1), _page_ref(2)),
        metadata=ToolMetadata(tool_name="page_search"),
    )

    with pytest.raises(KnowledgeContextError, match="数量不一致"):
        _mapper().build(tool_result, question="问题", packager=_packager())


def test_mapping_is_deterministic() -> None:
    references = (_page_ref(1), _page_ref(2))
    data = _search_result_data(
        [
            {"id": 1, "document_title": "A", "snippet": "一"},
            {"id": 2, "document_title": "B", "snippet": "二"},
        ]
    )
    tool_result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data=data,
        references=references,
        metadata=ToolMetadata(tool_name="page_search"),
    )

    first = _mapper().build(tool_result, question="q", packager=_packager())
    second = _mapper().build(tool_result, question="q", packager=_packager())

    assert [item.stable_id for item in first.items] == [
        item.stable_id for item in second.items
    ]
    assert first.citations == second.citations


def test_empty_results_produce_no_evidence_error() -> None:
    tool_result = ToolResult(
        status=ToolResultStatus.EMPTY,
        data={"query": "x", "limit": 20, "total": 0, "results": []},
        references=(),
        metadata=ToolMetadata(tool_name="page_search"),
    )

    with pytest.raises(KnowledgeContextError, match="空上下文"):
        _mapper().build(tool_result, question="问题", packager=_packager())
