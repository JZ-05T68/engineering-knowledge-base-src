"""Knowledge Agent page tests plus the legacy loopback HTTP client contract.

The user-facing page is intentionally limited to real, locally imported
documents.  Frozen mock responses are injected only as deterministic display
fixtures; the page no longer exposes presets or a mock-mode switch.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.demo import MockDemoClient
from src.demo.contracts import DemoHTTPError
from src.demo.presets import PRESETS
from src.demo_ui import AgentMode, AskRecord
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_memory_service import KnowledgeMemoryService
from src.models import ContextFingerprintState, ContextItemType, parse_memory_citations

PAGE_PATH = str(next((Path(__file__).parents[1] / "pages").glob("0_*.py")))
HOME_PATH = str(Path(__file__).parents[1] / "app.py")

QUESTIONS = {preset.preset_id: preset.question for preset in PRESETS}
KB = "0e6b1de5-0000-4000-8000-000000000001"


@pytest.fixture
def app() -> AppTest:
    return AppTest.from_file(PAGE_PATH).run(timeout=30)


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"button {key} not found"
    return matches[0]


# ---------------------------------------------------------------------------
# Page: initial render
# ---------------------------------------------------------------------------


def test_initial_render_only_offers_real_document_questions(app: AppTest) -> None:
    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "问问知识 Agent" in markdown
    assert "使用我已读的资料" in markdown
    assert "问问你添加过的资料" in markdown
    assert "预置离线演示" not in markdown
    assert not [button for button in app.button if (button.key or "").startswith("preset_")]
    assert app.radio == []


def _app_with_preset_response(preset_id: str) -> AppTest:
    """Inject one frozen response to exercise answer/source presentation only."""

    client = MockDemoClient()
    question = QUESTIONS[preset_id]
    response = client.run_agent(question)
    metadata = {source_id: client.get_source(source_id) for source_id in response.citations}
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    app.session_state["agent_request_seq"] = 1
    app.session_state["agent_result"] = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question=question,
        response=response,
        source_metadata=metadata,
    )
    app.run(timeout=30)
    assert not app.exception
    return app


# ---------------------------------------------------------------------------
# Page: scenario A — grounded answer + source viewer
# ---------------------------------------------------------------------------


def test_scenario_a_grounded_answer_and_source_viewer() -> None:
    app = _app_with_preset_response("A")

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "已在资料中找到" in markdown
    assert "依据 2 条" in markdown
    assert "PID 调整时，比例项与积分项分别影响什么？" in markdown
    assert "预置演示数据" in markdown
    assert "**" not in markdown

    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert len(source_buttons) == 2
    assert all("《" in (button.label or "") for button in source_buttons)

    _button(app, "src_0_" + f"{KB}:page:1").click()
    app.run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "原始资料" in markdown
    assert "PID 控制器调试手册" in markdown
    assert "第 12 页" in markdown
    assert "演示预置状态" not in markdown  # scenario A sources carry no integrity state


def test_completed_answer_can_be_saved_only_by_clicking_memory_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "knowledge.db")
    memories = KnowledgeMemoryService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: memories
    )

    client = MockDemoClient()
    question = QUESTIONS["A"]
    response = client.run_agent(question)
    metadata = {source_id: client.get_source(source_id) for source_id in response.citations}
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    app.session_state["agent_request_seq"] = 1
    app.session_state["agent_result"] = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question=question,
        response=response,
        source_metadata=metadata,
    )
    app.run(timeout=30)

    assert memories.count() == 0
    save_button = _button(app, "save_agent_memory_1")
    assert save_button.label == "保存这次问答"
    captions = "\n".join(item.value for item in app.caption)
    assert "保存的是这个问题和这次回答的副本" in captions
    assert "不代表你的亲身经验" in captions
    save_button.click().run(timeout=30)
    assert not app.exception
    assert memories.count() == 1
    saved = memories.list()[0]
    assert saved.kind.value == "raw_qa"
    assert saved.creation_origin == "human_saved"
    assert saved.title == question
    assert saved.content.startswith(f"问题：{question}\n\nAgent 回答：")
    assert "Agent 回答" in saved.content
    assert any(
        "可在“我保存过的内容”中再次查看" in item.value
        for item in app.success
    )


def test_saved_answer_title_says_when_two_documents_were_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "knowledge.db")
    first = database.create_document(
        title="第一份资料",
        filename="first.pdf",
        source_path=tmp_path / "first.pdf",
        sha256="1" * 64,
        page_count=1,
    )
    second = database.create_document(
        title="第二份资料",
        filename="second.pdf",
        source_path=tmp_path / "second.pdf",
        sha256="2" * 64,
        page_count=1,
    )
    database.create_page(
        document_id=first.id,
        page_number=1,
        image_path=tmp_path / "first.png",
        extracted_text="第一份资料的内容",
    )
    database.create_page(
        document_id=second.id,
        page_number=1,
        image_path=tmp_path / "second.png",
        extracted_text="第二份资料的内容",
    )
    memories = KnowledgeMemoryService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: memories
    )

    client = MockDemoClient()
    question = QUESTIONS["A"]
    response = client.run_agent(question)
    metadata = {source_id: client.get_source(source_id) for source_id in response.citations}
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    app.session_state["agent_request_seq"] = 1
    app.session_state["agent_result"] = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question=question,
        response=response,
        source_metadata=metadata,
    )
    app.run(timeout=30)

    _button(app, "save_agent_memory_1").click().run(timeout=30)
    assert not app.exception
    saved = memories.list()[0]
    assert saved.title == "关于 2 份资料的讨论"
    assert saved.document_id == first.id
    # v0.7 Phase 1: the full citation snapshot keeps both sources.
    citations = parse_memory_citations(saved.citation_snapshot)
    assert len(citations) == 2
    assert {c.document_title for c in citations} == {"第一份资料", "第二份资料"}


# ---------------------------------------------------------------------------
# Page: scenario B — historical experience with limitation warning
# ---------------------------------------------------------------------------


def test_scenario_b_warning_and_memory_source() -> None:
    app = _app_with_preset_response("B")

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "已在资料中找到" in markdown
    assert "这份资料需要再检查" in markdown
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert len(source_buttons) == 1
    assert "编码器接线错误导致 PID 震荡的问题解决记录" in (source_buttons[0].label or "")
    source_buttons[0].click()
    app.run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "我保存过的内容" in markdown
    assert "编码器接线错误导致 PID 震荡的问题解决记录" in markdown


# ---------------------------------------------------------------------------
# Page: scenario C — integrity / trust
# ---------------------------------------------------------------------------


def test_scenario_c_integrity_viewer_state() -> None:
    app = _app_with_preset_response("C")

    assert not app.exception
    _button(app, "src_0_" + f"{KB}:knowledge_object:1").click()
    app.run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "来源发生变化" in markdown
    assert "演示预置状态" in markdown
    assert "不代表" in markdown  # changed = snapshot mismatch, explicitly disclaimed
    assert "不可信" not in markdown


# ---------------------------------------------------------------------------
# Page: empty and failed states
# ---------------------------------------------------------------------------


def test_empty_state_is_honest_without_demo_suggestions() -> None:
    app = _app_with_preset_response("EMPTY")

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "当前知识库中没有找到足够资料支持这个问题。" in markdown
    assert "资料里没有找到答案" in markdown
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert source_buttons == []
    assert not [button for button in app.button if (button.key or "").startswith("suggest_")]


def test_rehearsal_failed_state_is_safe() -> None:
    app = _app_with_preset_response("REHEARSAL_FAILED")

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "本次请求未完成" in markdown
    assert "知识库工具执行失败" in markdown
    for forbidden in ("Traceback", "Exception", '{"', "request_id"):
        assert forbidden not in markdown
    assert _button(app, "failure_retry")
    # business failure keeps the sources panel in a pending state, never the
    # "no supporting material" no-evidence copy
    assert "请求未完成，暂时没有来源可显示" in markdown
    assert "资料里没有找到答案" not in markdown


def test_rate_limited_state_renders_distinctly() -> None:
    app = _app_with_failure(failure_code="rate_limited")
    markdown = "\n".join(item.value for item in app.markdown)
    assert "请求过于频繁" in markdown
    assert "本次请求未完成" not in markdown.split("请求过于频繁")[0]


def test_backend_unavailable_state_does_not_offer_fake_answer_switch() -> None:
    app = _app_with_failure(failure_http_status=0)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "本机 Agent 服务当前不可用" in markdown
    assert _button(app, "failure_retry")
    assert not [button for button in app.button if button.key == "failure_switch_mock"]


def _app_with_failure(**failure: object) -> AppTest:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    app.session_state["agent_request_seq"] = 1
    app.session_state["agent_result"] = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question="任意问题",
        **failure,
    )
    app.run(timeout=30)
    assert not app.exception
    return app


# ---------------------------------------------------------------------------
# Home page entry point
# ---------------------------------------------------------------------------


def _stub_home_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    with_document: bool = False,
) -> None:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_dir / "knowledge.db")
    if with_document:
        database.create_document(
            title="首页测试资料",
            filename="home-test.pdf",
            source_path=tmp_path / "home-test.pdf",
            sha256="9" * 64,
        )
    monkeypatch.setattr(runtime, "application_settings", lambda: SimpleNamespace(
        data_dir="data", host="127.0.0.1", port=8501
    ))
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_startup_reconciliation", lambda: None
    )
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )


def test_home_page_offers_one_click_agent_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_home_runtime(monkeypatch, tmp_path, with_document=True)
    app = AppTest.from_file(HOME_PATH).run(timeout=30)
    assert not app.exception
    entries = [b for b in app.button if "问问 Agent" in (b.label or "")]
    assert entries


@pytest.mark.parametrize("query", ["  STM32 定时器  ", "电" * 510, "   "])
def test_home_search_handoff_preserves_query_without_calling_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    """The search form uses the existing local-search handoff, including empty input."""

    import streamlit as st

    _stub_home_runtime(monkeypatch, tmp_path, with_document=True)
    destinations: list[str] = []
    monkeypatch.setattr(st, "switch_page", destinations.append)
    app = AppTest.from_file(HOME_PATH).run(timeout=30)
    app.text_input(key="home_search_query").input(query)
    next(button for button in app.main.button if button.label == "搜索").click()
    app.run(timeout=30)
    assert not app.exception
    if query.strip():
        assert destinations == ["pages/4_检索资料.py"]
        assert app.session_state["pending_search_query_params"] == {"q": query.strip()[:500]}
    else:
        assert not destinations
        assert any("请输入你想找的内容" in item.value for item in app.info)
        assert "pending_search_query_params" not in app.session_state


# ---------------------------------------------------------------------------
# Mode 1 loopback client (frozen /v0.6 contract)
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    status_code = 200
    payload: dict[str, object] | str = {}

    def log_message(self, *args: object) -> None:  # silence test output
        return

    def _respond(self) -> None:
        body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._respond()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._respond()


@pytest.fixture
def loopback_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _run_agent_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_client_parses_frozen_agent_run_contract(loopback_server) -> None:
    from src.agent_client import HostedAgentClient

    _Handler.status_code = 200
    _Handler.payload = {
        "request_id": "req-1",
        "status": "completed",
        "answer": "答案【来源 #1】。",
        "grounded": True,
        "citations": [f"{KB}:page:1"],
        "warnings": ["来源存在限制，请核对引用资料。"],
        "error": None,
    }
    client = HostedAgentClient(_run_agent_url(loopback_server))
    response = client.run_agent("问题", correlation_id="demo.correlation-1")
    assert response.status == "completed"
    assert response.grounded is True
    assert response.citations == (f"{KB}:page:1",)


def test_client_normalizes_http_failures_to_closed_catalog(loopback_server) -> None:
    from src.agent_client import HostedAgentClient

    _Handler.status_code = 429
    _Handler.payload = {
        "request_id": "req-2",
        "status": "failed",
        "error": {"code": "rate_limited", "message": "请求过于频繁，请稍后再试。"},
    }
    client = HostedAgentClient(_run_agent_url(loopback_server))
    with pytest.raises(DemoHTTPError) as excinfo:
        client.run_agent("问题")
    assert excinfo.value.http_status == 429
    assert excinfo.value.failure.error.code == "rate_limited"

    _Handler.status_code = 503
    _Handler.payload = {"detail": "down"}
    with pytest.raises(DemoHTTPError) as excinfo:
        client.run_agent("问题")
    assert excinfo.value.failure.error.code == "runtime_unavailable"


def test_client_connection_refused_maps_to_backend_unavailable() -> None:
    from src.agent_client import HostedAgentClient

    # Port 1 on loopback: nothing listens there; refusal is immediate.
    client = HostedAgentClient("http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(DemoHTTPError) as excinfo:
        client.run_agent("问题")
    assert excinfo.value.http_status == 0
    assert excinfo.value.failure.error.code == "runtime_unavailable"


def test_client_rejects_non_loopback_base_url() -> None:
    from src.agent_client import HostedAgentClient

    for bad_url in ("http://192.168.1.10:8000", "https://example.com", "http://0.0.0.0:1"):
        with pytest.raises(ValueError):
            HostedAgentClient(bad_url)


def test_client_source_lookup_parses_metadata(loopback_server) -> None:
    from src.agent_client import HostedAgentClient

    _Handler.status_code = 200
    _Handler.payload = {
        "stable_id": f"{KB}:page:1",
        "type": "page",
        "title": "调试手册",
        "label": "第 1 页",
    }
    client = HostedAgentClient(_run_agent_url(loopback_server))
    source = client.get_source(f"{KB}:page:1")
    assert source.type is ContextItemType.PAGE
    assert source.title == "调试手册"


def test_demo_source_response_integrity_vocabulary() -> None:
    from src.demo.contracts import DemoSourceResponse

    source = DemoSourceResponse(
        stable_id=f"{KB}:knowledge_object:1",
        type=ContextItemType.KNOWLEDGE_OBJECT,
        title="伺服驱动调试指南",
        label="原理",
        integrity_state=ContextFingerprintState.CHANGED,
        demo_note="演示预置状态。",
    )
    assert source.integrity_state is ContextFingerprintState.CHANGED
