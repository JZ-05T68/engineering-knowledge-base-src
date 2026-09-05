"""v0.7.1 Experience Recall engineering tests.

Covers the recall-semantics surface the agent depends on: search results carry
the authority boundary and applicability conditions of memory entries, the
knowledge_read tool exposes confirmation and provenance fields, and the
decision prompt encodes the recall rules (positive recall phrasing, negative
suppression, version/condition awareness, raw-QA authority).
"""

from __future__ import annotations

from pathlib import Path

from src.agent.tools.adapters.knowledge_read import KnowledgeMemoryAdapter
from src.agent.tools.adapters.knowledge_search import KnowledgeSearchAdapter
from src.agent.tools.contracts import ToolContext, ToolInput
from src.database import Database
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_search_service import KnowledgeSearchService


def _database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _document_and_page(
    database: Database, *, title: str, sha256: str, text: str
) -> tuple[int, int]:
    document = database.create_document(
        title=title,
        filename=f"{sha256[:8]}.pdf",
        source_path=f"data/raw/{sha256[:8]}.pdf",
        sha256=sha256,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=f"data/pages/{document.id}/page_0001.png",
        extracted_text=text,
    )
    return document.id, page.id


def _experience(
    service: KnowledgeMemoryService, raw_qa_id: int | None = None
):
    return service.promote_raw_qa_to_experience(
        raw_qa_id,
        title="电机闭环方向故障排查",
        content=(
            "遇到的问题：电机闭环运行方向异常。\n\n"
            "处理方式：交换 A/B 相后恢复正常。"
        ),
        root_cause="A/B 相接反",
        lesson="方向异常先查相序。",
        outcome="设备恢复正常运行。",
        context_conditions="仅适用于 DemoSuite 3.2 固件。",
        root_cause_confirmed=True,
    )


def _adapter(database: Database) -> KnowledgeSearchAdapter:
    return KnowledgeSearchAdapter(KnowledgeSearchService(database))


def _memory_adapter(database: Database) -> KnowledgeMemoryAdapter:
    return KnowledgeMemoryAdapter(
        KnowledgeMemoryService(database),
        kb_uuid=database.get_knowledge_base_uuid(),
    )


def test_search_result_carries_recall_semantics(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="电机手册", sha256="a" * 64, text="闭环控制说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = service.create_raw_qa_entry(
        question="电机闭环方向异常怎么处理？",
        answer="交换 A/B 相。",
        cited_page_ids=(page_id,),
    ).entry
    experience = _experience(service, raw_qa.id)

    results = database.search_knowledge("电机 闭环 方向")
    memories = [
        item
        for item in results
        if item.result_type.value == "knowledge_memory"
        and item.id == experience.id
    ]
    assert memories, "experience 应可被知识检索命中"
    found = memories[0]
    assert found.kind == "experience"
    assert found.creation_origin == "agent_assisted"
    assert found.outcome == "设备恢复正常运行。"
    assert found.context_conditions == "仅适用于 DemoSuite 3.2 固件。"
    assert found.root_cause_confirmed is True


def test_search_adapter_exposes_recall_fields(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="电机手册", sha256="b" * 64, text="闭环控制说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = service.create_raw_qa_entry(
        question="电机闭环方向异常怎么处理？",
        answer="交换 A/B 相。",
        cited_page_ids=(page_id,),
    ).entry
    _experience(service, raw_qa.id)

    adapter = _adapter(database)
    result = adapter(
        ToolInput(tool_name="knowledge_search", arguments={"query": "电机 闭环 方向"}),
        ToolContext(run_id="test-run"),
    )
    assert result.status.value == "success"
    experiences = [
        item
        for item in result.data["results"]
        if item["kind"] == "experience"
    ]
    assert experiences, "experience 结果必须出现在 tool 输出里"
    payload = experiences[0]
    assert payload["creation_origin"] == "agent_assisted"
    assert payload["context_conditions"] == "仅适用于 DemoSuite 3.2 固件。"
    assert payload["root_cause_confirmed"] is True
    assert payload["outcome"] == "设备恢复正常运行。"
    assert payload["presentation_note"] is not None
    assert "你之前整理过一条经验" in payload["presentation_note"]


def test_knowledge_read_exposes_confirmation_and_source(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="电机手册", sha256="c" * 64, text="闭环控制说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = service.create_raw_qa_entry(
        question="电机闭环方向异常怎么处理？",
        answer="交换 A/B 相。",
        cited_page_ids=(page_id,),
    ).entry
    experience = _experience(service, raw_qa.id)

    adapter = _memory_adapter(database)
    from src.models import KNOWLEDGE_MEMORY_STABLE_TYPE, build_stable_id

    result = adapter(
        ToolInput(
            tool_name="get_knowledge_memory",
            arguments={
                "stable_id": build_stable_id(
                    database.get_knowledge_base_uuid(),
                    KNOWLEDGE_MEMORY_STABLE_TYPE,
                    experience.id,
                )
            }
        ),
        ToolContext(run_id="test-run"),
    )
    assert result.status.value == "success"
    assert result.data["root_cause_confirmed"] is True
    assert result.data["source_entry_id"] == raw_qa.id
    assert result.data["creation_origin"] == "agent_assisted"
    assert result.data["context_conditions"] == "仅适用于 DemoSuite 3.2 固件。"


def test_decision_prompt_encodes_recall_rules() -> None:
    from src.agent.decision import prompt as decision_prompt
    from src.agent.tools.adapters.knowledge_read import (
        GET_KNOWLEDGE_MEMORY_DEFINITION,
    )
    from src.agent.tools.adapters.knowledge_search import (
        KNOWLEDGE_SEARCH_DEFINITION,
    )

    built = decision_prompt.build_decision_prompt(
        "测试问题",
        (KNOWLEDGE_SEARCH_DEFINITION, GET_KNOWLEDGE_MEMORY_DEFINITION),
    )
    assert "Experience Recall 规则" in built
    assert "你之前整理过一条经验" in built
    assert "root_cause_confirmed" in built
    assert "适用条件" in built
    assert "词面相似但主题无关" in built
    assert "现已不可用" in built
    assert "没有找到你整理过的相关经验" in built
    assert "不得因此宣称" in built


def test_mapper_prepends_recall_note_to_memory_content(tmp_path: Path) -> None:
    from src.agent.response.tool_context import ToolResultContextMapper
    from src.agent.tools.contracts import ToolReference, ToolResult, ToolResultStatus
    from src.knowledge_context_packager import KnowledgeContextPackager

    database_dir = tmp_path / "db"
    database_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_dir / "knowledge.db")
    kb_uuid = database.get_knowledge_base_uuid()
    experience_id = 4
    stable_id = (
        f"{kb_uuid}:knowledge_memory:{experience_id}"
    )
    row = {
        "result_type": "knowledge_memory",
        "id": experience_id,
        "stable_id": stable_id,
        "title": "经验条目",
        "snippet": "处理方式：对调 U/V 两相。",
        "kind": "experience",
        "kind_label": "经验",
        "root_cause_confirmed": False,
        "source_anchors": [],
    }
    result = ToolResult(
        status=ToolResultStatus.SUCCESS,
        data={
            "query": "测试",
            "total": 1,
            "results": [row],
        },
        references=[
            ToolReference(stable_id=stable_id, anchor_label="经验条目")
        ],
    )
    package = ToolResultContextMapper().build(
        result,
        question="测试问题",
        packager=KnowledgeContextPackager(kb_uuid=kb_uuid, app_version="test"),
    )
    memory_items = [
        item for item in package.items if item.stable_id == stable_id
    ]
    assert memory_items, "memory item must be projected"
    assert "【表述要求】" in memory_items[0].content
    assert "你之前整理过一条经验" in memory_items[0].content
    assert "未经你确认" in memory_items[0].content
