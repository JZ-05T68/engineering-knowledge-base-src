"""End-to-end user PDF -> Qwen reading -> Agent answer -> original page tests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import fitz

from scripts.run_document_agent_e2e import _mentions_page
from src.agent import build_decision_prompt
from src.agent.local_document import LocalDocumentAgent
from src.agent.tools import ToolContext, ToolInput, ToolResultStatus
from src.agent.tools.bootstrap import build_phase1_handlers, build_phase1_registry
from src.agent_document_reader import AgentReadingStore, page_agent_source
from src.ai.provider import CompletionResult
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.knowledge_memory_service import KnowledgeMemoryService

MODEL = "qwen3.8-max"
QUESTION_TO_QUERY = {
    "EKB-S42 的额定转速是多少？": "额定转速 1379 rpm",
    "那次编码器问题最终怎么解决？大约用了多久？": "A/B 两相接反 42 分钟",
    "E17 表示什么？": "E17 编码器方向不一致",
    "散热风扇多久检查一次？": "散热风扇 每 500 小时",
    "这份资料里的唯一验证标记是什么？": "唯一验证标记 EKB-VERIFY",
    "EKB-S42 的电机轴承型号是什么？": "轴承型号",
}
QUESTION_TO_PAGE = {
    "EKB-S42 的额定转速是多少？": 2,
    "那次编码器问题最终怎么解决？大约用了多久？": 5,
    "E17 表示什么？": 6,
    "散热风扇多久检查一次？": 7,
    "这份资料里的唯一验证标记是什么？": 8,
    "EKB-S42 的电机轴承型号是什么？": 10,
}


def test_page_reference_validation_accepts_normal_chinese_spacing() -> None:
    assert _mentions_page("来源是第8页", 8)
    assert _mentions_page("来源是第 8 页", 8)
    assert not _mentions_page("来源是第 7 页", 8)


def test_decision_prompt_routes_imported_document_facts_to_page_search() -> None:
    prompt = build_decision_prompt(
        "这份 PDF 里的唯一验证标记是什么？",
        build_phase1_registry().list_definitions(),
    )

    assert "必须选择 page_search" in prompt
    assert "不能选择 ANSWER_DIRECTLY" in prompt
    assert "不要用它代替对导入页面的检索" in prompt


class _Qwen38FixtureProvider:
    """Deterministic provider exercising all three real completion boundaries."""

    is_configured = True

    def __init__(self) -> None:
        self.read_calls = 0
        self.decision_calls = 0
        self.answer_calls = 0

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        assert model == MODEL
        assert max_completion_tokens is not None
        if "原始页面文字(JSON 字符串)" in prompt:
            self.read_calls += 1
            page = int(re.search(r"原始页码：(\d+) /", prompt).group(1))  # type: ignore[union-attr]
            payload = {
                "summary": f"已理解第 {page} 页原始文字。",
                "keywords": [f"第{page}页"],
                "key_facts": ["事实只来自本页原文"],
            }
            return CompletionResult(
                text=json.dumps(payload, ensure_ascii=False), model=MODEL
            )
        if "EKB 单步只读 Agent 的结构化决策器" in prompt:
            self.decision_calls += 1
            question = _decision_question(prompt)
            return CompletionResult(
                text=json.dumps(
                    {
                        "kind": "CALL_TOOL",
                        "tool_name": "page_search",
                        "arguments": {"query": QUESTION_TO_QUERY[question]},
                    },
                    ensure_ascii=False,
                ),
                model=MODEL,
            )
        self.answer_calls += 1
        question = next(question for question in QUESTION_TO_QUERY if question in prompt)
        page = QUESTION_TO_PAGE[question]
        citation = _citation_for_page(prompt, page)
        answers = {
            2: "EKB-S42 的额定转速是 1379 rpm",
            5: "根因是编码器 A/B 两相接反，交换 A/B 相后恢复，共用时 42 分钟",
            6: "E17 表示编码器方向不一致",
            7: (
                "根据用户人工校对，散热风扇应每 350 小时检查一次"
                if "【用户人工校对或补充】" in prompt and "350 小时" in prompt
                else "散热风扇应每 500 小时检查一次"
            ),
            8: "唯一验证标记是 EKB-VERIFY-9F27K",
            10: "资料中没有提供 EKB-S42 的电机轴承型号，不能猜测",
        }
        return CompletionResult(
            text=f"{answers[page]}，来源是原 PDF 第 {page} 页【来源 #{citation}】。",
            model=MODEL,
        )


def _decision_question(prompt: str) -> str:
    block = prompt.split("[USER_REQUEST]\n", 1)[1].split("\n[END_USER_REQUEST]", 1)[0]
    return json.loads(block)


def _citation_for_page(prompt: str, page_number: int) -> int:
    blocks = re.findall(r"## \[(\d+)\] ([\s\S]*?)(?=\n## \[|\Z)", prompt)
    for number, block in blocks:
        if f"第 {page_number} 页" in block:
            return int(number)
    raise AssertionError(f"page {page_number} source missing from prompt")


def _build_pdf(path: Path) -> None:
    page_text = {
        1: "EKB 演示资料。",
        2: "设备 EKB-S42 的额定转速为 1379 rpm。",
        3: "PID 比例项影响即时响应，积分项用于消除静差。",
        4: "编码器 A/B 相接反会造成反馈方向错误和震荡。",
        5: "排障确认编码器 A/B 两相接反，交换后恢复；总耗时 42 分钟。",
        6: "报警 E17 表示编码器方向不一致。",
        7: "散热风扇每 500 小时检查一次。",
        8: "唯一验证标记是 EKB-VERIFY-9F27K。",
        9: "资料从 R1 更新到 R2。",
        10: "本资料没有提供 EKB-S42 的电机轴承型号。",
    }
    document = fitz.open()
    for page_number in range(1, 11):
        page = document.new_page()
        page.insert_text((72, 72), page_text[page_number], fontname="china-s", fontsize=12)
    document.save(path)
    document.close()


def _build_system(tmp_path: Path):
    database = Database(tmp_path / "data" / "database" / "knowledge.db")
    pdf = tmp_path / "EKB-test.pdf"
    _build_pdf(pdf)
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
    )
    imported = service.import_pdf(pdf.read_bytes(), pdf.name, title="EKB 测试资料")
    provider = _Qwen38FixtureProvider()
    readings = AgentReadingStore(tmp_path / "data" / "agent-readings")
    agent = LocalDocumentAgent(
        database=database,
        provider=provider,
        readings=readings,
        model=MODEL,
    )
    return database, imported, provider, readings, agent


def test_user_pdf_is_read_page_by_page_and_answered_with_original_pages(
    tmp_path: Path,
) -> None:
    database, imported, provider, readings, agent = _build_system(tmp_path)
    original_text = {
        page.id: page_agent_source(page)[0] for page in imported.pages
    }
    progress: list[tuple[int, int]] = []

    report = agent.read_document(
        imported.document.id,
        progress_callback=lambda current, total: progress.append((current, total)),
    )

    assert report.state.completed
    assert report.total_pages == report.newly_read_pages == 10
    assert report.reused_pages == 0
    assert provider.read_calls == 10
    assert progress == [(page, 10) for page in range(1, 11)]
    for page in database.list_pages(imported.document.id):
        reading = readings.page_reading(page.id)
        assert reading is not None
        assert reading.page_number == page.page_number
        assert reading.model == MODEL
        assert reading.source_text_sha256 == hashlib.sha256(
            original_text[page.id].encode("utf-8")
        ).hexdigest()
        assert page_agent_source(database.get_page(page.id))[0] == original_text[page.id]  # type: ignore[arg-type]

    expected_fragments = {
        "EKB-S42 的额定转速是多少？": "1379 rpm",
        "那次编码器问题最终怎么解决？大约用了多久？": "42 分钟",
        "E17 表示什么？": "编码器方向不一致",
        "散热风扇多久检查一次？": "每 500 小时",
        "这份资料里的唯一验证标记是什么？": "EKB-VERIFY-9F27K",
        "EKB-S42 的电机轴承型号是什么？": "没有提供",
    }
    for question, expected in expected_fragments.items():
        response = agent.ask(question)
        page_number = QUESTION_TO_PAGE[question]
        assert response.status == "completed", (question, response)
        assert response.grounded is True
        assert response.model == MODEL
        assert expected in response.answer
        assert f"第 {page_number} 页" in response.answer
        cited_pages = [
            database.get_page(int(stable_id.rsplit(":", 1)[1])).page_number  # type: ignore[union-attr]
            for stable_id in response.citations
        ]
        assert page_number in cited_pages

    assert provider.decision_calls == 6
    assert provider.answer_calls == 6


def test_reader_resume_is_free_and_agent_context_keeps_original_text(tmp_path: Path) -> None:
    database, imported, provider, readings, agent = _build_system(tmp_path)
    agent.read_document(imported.document.id)
    page_8 = database.get_page_by_number(imported.document.id, 8)
    assert page_8 is not None
    first_read_calls = provider.read_calls

    second = agent.read_document(imported.document.id)

    assert second.newly_read_pages == 0
    assert second.reused_pages == 10
    assert provider.read_calls == first_read_calls
    handler = build_phase1_handlers(
        database,
        page_readings=readings,
        require_agent_read=True,
    )["page_search"]
    result = handler(
        ToolInput(
            tool_name="page_search",
            arguments={"query": "9F27K"},
        ),
        ToolContext(),
    )
    assert result.status is ToolResultStatus.SUCCESS
    row = next(item for item in result.data["results"] if item["page_number"] == 8)  # type: ignore[index]
    assert "EKB-VERIFY-9F27K" in row["content"]
    assert row["content"] == page_agent_source(page_8)[0]
    assert row["reading_summary"] != row["content"]


def test_unread_uploaded_pages_are_not_available_to_local_agent(tmp_path: Path) -> None:
    _, _, provider, _, agent = _build_system(tmp_path)

    response = agent.ask("这份资料里的唯一验证标记是什么？")

    assert response.status == "completed"
    assert response.grounded is False
    assert "没有在当前知识库中找到" in response.answer
    assert provider.decision_calls == 1
    assert provider.answer_calls == 0


def test_changed_original_page_invalidates_old_agent_reading(tmp_path: Path) -> None:
    database, imported, _, readings, agent = _build_system(tmp_path)
    agent.read_document(imported.document.id)
    page_8 = database.get_page_by_number(imported.document.id, 8)
    assert page_8 is not None
    database.update_page(
        page_8.id,
        extracted_text=page_8.extracted_text + "\n来源已重新提取。",
    )
    handler = build_phase1_handlers(
        database,
        page_readings=readings,
        require_agent_read=True,
    )["page_search"]

    result = handler(
        ToolInput(
            tool_name="page_search",
            arguments={"query": "9F27K"},
        ),
        ToolContext(),
    )

    assert result.status is ToolResultStatus.EMPTY
    assert result.data["results"] == []  # type: ignore[index]
    assert "尚未让 Agent 读完" in result.warnings[0]


def test_manual_page_correction_is_labelled_and_requires_rereading(tmp_path: Path) -> None:
    database, imported, _, readings, agent = _build_system(tmp_path)
    agent.read_document(imported.document.id)
    page = database.get_page_by_number(imported.document.id, 2)
    assert page is not None

    database.update_page(page.id, markdown_content="人工确认：额定转速应按铭牌记录。")
    updated = database.get_page(page.id)
    assert updated is not None
    combined, kind = page_agent_source(updated)

    assert "【原始页面文字】" in combined
    assert "【用户人工校对或补充】" in combined
    assert "1379 rpm" in combined
    assert "人工确认" in combined
    assert kind == "pdf_text+manual"
    assert not readings.is_page_ready(
        page.id, hashlib.sha256(combined.encode("utf-8")).hexdigest()
    )
    reread = agent.read_document(imported.document.id)
    assert reread.newly_read_pages == 1
    assert reread.reused_pages == 9
    assert readings.is_page_ready(
        page.id, hashlib.sha256(combined.encode("utf-8")).hexdigest()
    )


def test_manual_correction_replaces_old_answer_only_after_agent_rereads(
    tmp_path: Path,
) -> None:
    database, imported, _, readings, agent = _build_system(tmp_path)
    agent.read_document(imported.document.id)
    page = database.get_page_by_number(imported.document.id, 7)
    assert page is not None
    database.update_page(
        page.id,
        markdown_content="人工校对：散热风扇应每 350 小时检查一次。",
    )

    before_reread = agent.ask("散热风扇多久检查一次？")
    assert before_reread.grounded is False
    assert "没有在当前知识库中找到" in before_reread.answer

    reread = agent.read_document(imported.document.id)
    assert reread.newly_read_pages == 1
    assert reread.reused_pages == 9
    after_reread = agent.ask("散热风扇多久检查一次？")
    assert after_reread.status == "completed"
    assert after_reread.grounded is True
    assert "350 小时" in after_reread.answer
    assert "500 小时" not in after_reread.answer
    cited_pages = [
        database.get_page(int(stable_id.rsplit(":", 1)[1])).page_number  # type: ignore[union-attr]
        for stable_id in after_reread.citations
    ]
    assert cited_pages == [7]
    assert readings.page_reading(page.id) is not None


def test_deleted_document_is_not_answered_from_saved_question_copy(tmp_path: Path) -> None:
    database, imported, provider, readings, agent = _build_system(tmp_path)
    agent.read_document(imported.document.id)
    page_8 = database.get_page_by_number(imported.document.id, 8)
    assert page_8 is not None
    answered = agent.ask("这份资料里的唯一验证标记是什么？")
    memory_service = KnowledgeMemoryService(database)
    memory = memory_service.create_entry(
        kind="experience",
        title="关于 EKB 测试资料的讨论",
        content=f"问题：唯一验证标记是什么？\n\nAgent 回答：\n{answered.answer}",
        document_id=imported.document.id,
        page_id=page_8.id,
    )
    source_bytes = Path(imported.document.source_path).read_bytes()
    old_document_id = imported.document.id
    old_page_ids = [page.id for page in imported.pages]
    assert readings.document_state(old_document_id) is not None
    assert all(readings.page_reading(page_id) is not None for page_id in old_page_ids)

    deletion = DocumentDeletionService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
        data_dir=tmp_path / "data",
        agent_readings_dir=tmp_path / "data" / "agent-readings",
    )
    deletion.delete_document(
        old_document_id, expected_title=imported.document.title
    )

    saved_copy = memory_service.get(memory.id)
    assert saved_copy is not None
    assert saved_copy.document_id is None
    assert saved_copy.page_id is None
    assert "EKB-VERIFY-9F27K" in saved_copy.content
    assert readings.document_state(old_document_id) is None
    assert all(readings.page_reading(page_id) is None for page_id in old_page_ids)
    answer_calls_before = provider.answer_calls
    after_delete = agent.ask("这份资料里的唯一验证标记是什么？")
    assert after_delete.grounded is False
    assert "没有在当前知识库中找到" in after_delete.answer
    assert provider.answer_calls == answer_calls_before

    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
    )
    reimported = service.import_pdf(
        source_bytes,
        imported.document.filename,
        title=imported.document.title,
    )
    assert reimported.duplicate is False
    assert reimported.document.id != old_document_id
    before_reread = agent.ask("这份资料里的唯一验证标记是什么？")
    assert before_reread.grounded is False
    reread = agent.read_document(reimported.document.id)
    assert reread.newly_read_pages == 10
    after_reread = agent.ask("这份资料里的唯一验证标记是什么？")
    assert after_reread.grounded is True
    assert "EKB-VERIFY-9F27K" in after_reread.answer
    assert any(
        database.get_page(int(stable_id.rsplit(":", 1)[1])).page_number == 8  # type: ignore[union-attr]
        for stable_id in after_reread.citations
    )


def test_same_filename_with_different_pdf_bytes_is_a_new_document(tmp_path: Path) -> None:
    database, imported, _, _, _ = _build_system(tmp_path)
    original_bytes = Path(imported.document.source_path).read_bytes()
    changed_pdf = fitz.open(stream=original_bytes, filetype="pdf")
    changed_pdf[7].insert_text(
        (72, 110), "EKB-VERIFY-CHANGED", fontname="china-s", fontsize=12
    )
    changed_bytes = changed_pdf.tobytes()
    changed_pdf.close()
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
    )

    second = service.import_pdf(
        changed_bytes,
        imported.document.filename,
        title=imported.document.title,
    )

    assert second.duplicate is False
    assert second.document.id != imported.document.id
    assert second.document.sha256 != imported.document.sha256
    assert second.document.source_path != imported.document.source_path
    assert Path(second.document.source_path).read_bytes() == changed_bytes
