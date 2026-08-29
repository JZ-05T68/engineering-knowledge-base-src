"""v0.6.1 Agent Workspace page tests (Streamlit AppTest) + Mode 1 client.

Covers the competition page end to end in mock mode (presets A/B/C, empty,
failed, source viewer, mode-switch reset), plus the loopback Mode-1 client
against a local throwaway HTTP server (frozen-contract parsing and failure
normalization). No external network, no production DB, no real AI.
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
from src.demo.contracts import DemoHTTPError
from src.demo.presets import PRESETS
from src.evidence_basket_service import EvidenceBasketService
from src.models import ContextFingerprintState, ContextItemType

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


def test_initial_render_shows_workspace_and_presets(app: AppTest) -> None:
    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "知识 Agent 工作台" in markdown
    assert "EKB · Engineering Knowledge Base" in markdown
    assert "预置离线演示" in markdown
    for preset_id in ("A", "B", "C"):
        assert _button(app, f"preset_{preset_id}")


# ---------------------------------------------------------------------------
# Page: scenario A — grounded answer + source viewer
# ---------------------------------------------------------------------------


def test_scenario_a_grounded_answer_and_source_viewer() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_A").click()
    app.run(timeout=30)

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "有依据回答" in markdown
    assert "引用来源 2 条" in markdown
    assert "PID 调整时，比例项与积分项分别影响什么？" in markdown
    assert "预置演示数据" in markdown

    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert len(source_buttons) == 2

    _button(app, "src_0_" + f"{KB}:page:1").click()
    app.run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "来源详情" in markdown
    assert "PID 控制器调试手册" in markdown
    assert "第 12 页" in markdown
    assert "演示预置状态" not in markdown  # scenario A sources carry no integrity state


# ---------------------------------------------------------------------------
# Page: scenario B — historical experience with limitation warning
# ---------------------------------------------------------------------------


def test_scenario_b_warning_and_memory_source() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_B").click()
    app.run(timeout=30)

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "有依据回答" in markdown
    assert "来源存在限制" in markdown
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert len(source_buttons) == 1
    assert "编码器接线错误导致 PID 震荡的问题解决记录" in (source_buttons[0].label or "")
    source_buttons[0].click()
    app.run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "知识记忆" in markdown
    assert "编码器接线错误导致 PID 震荡的问题解决记录" in markdown


# ---------------------------------------------------------------------------
# Page: scenario C — integrity / trust
# ---------------------------------------------------------------------------


def test_scenario_c_integrity_viewer_state() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_C").click()
    app.run(timeout=30)

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


def test_empty_state_is_honest_with_suggestions() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_EMPTY").click()
    app.run(timeout=30)

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "当前知识库中没有找到足够资料支持这个问题。" in markdown
    assert "未找到可支持资料" in markdown
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert source_buttons == []
    for preset_id in ("A", "B", "C"):
        assert _button(app, f"suggest_{preset_id}")


def test_rehearsal_failed_state_is_safe() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_REHEARSAL_FAILED").click()
    app.run(timeout=30)

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
    assert "未找到可支持资料" not in markdown


def test_rate_limited_state_renders_distinctly() -> None:
    app = _app_with_failure(failure_code="rate_limited")
    markdown = "\n".join(item.value for item in app.markdown)
    assert "请求过于频繁" in markdown
    assert "本次请求未完成" not in markdown.split("请求过于频繁")[0]


def test_backend_unavailable_state_offers_explicit_mock_switch() -> None:
    app = _app_with_failure(failure_http_status=0)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "本机 Agent 服务当前不可用" in markdown
    assert _button(app, "failure_retry")
    assert _button(app, "failure_switch_mock")


def _app_with_failure(**failure: object) -> AppTest:
    from src.demo_ui import AgentMode, AskRecord

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
# Page: mode switch clears state
# ---------------------------------------------------------------------------


def test_mode_switch_clears_previous_result() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_A").click()
    app.run(timeout=30)
    assert _button(app, "src_0_" + f"{KB}:page:1")

    app.radio[0].set_value("本机 Agent 服务（需先启动）")
    app.run(timeout=30)

    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "向你的工程知识库提问" in markdown
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert source_buttons == []


# ---------------------------------------------------------------------------
# Repeated-run reliability (freeze rehearsal): transitions without state bleed
# ---------------------------------------------------------------------------


def _outcome_text(app: AppTest) -> str:
    return "\n".join(item.value for item in app.markdown)


def test_sequential_scenario_transitions_do_not_contaminate_state() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)

    # A -> B -> C -> A: each run shows only its own answer, badges and sources.
    _button(app, "preset_A").click()
    app.run(timeout=30)
    assert "引用来源 2 条" in _outcome_text(app)
    assert _button(app, "src_0_" + f"{KB}:page:1")

    _button(app, "preset_B").click()
    app.run(timeout=30)
    text = _outcome_text(app)
    assert "来源存在限制" in text
    assert "引用来源 1 条" in text
    assert "引用来源 2 条" not in text

    _button(app, "preset_C").click()
    app.run(timeout=30)
    _button(app, "src_0_" + f"{KB}:knowledge_object:1").click()
    app.run(timeout=30)
    assert "来源发生变化" in _outcome_text(app)

    _button(app, "preset_A").click()
    app.run(timeout=30)
    text = _outcome_text(app)
    assert "有依据回答" in text
    # C's integrity state must not leak into scenario A's sources
    assert "来源发生变化" not in text

    # A -> empty -> A -> failed -> A: recovery transitions
    _button(app, "preset_EMPTY").click()
    app.run(timeout=30)
    assert "当前知识库中没有找到足够资料支持这个问题。" in _outcome_text(app)

    _button(app, "preset_A").click()
    app.run(timeout=30)
    assert "有依据回答" in _outcome_text(app)

    _button(app, "preset_REHEARSAL_FAILED").click()
    app.run(timeout=30)
    assert "本次请求未完成" in _outcome_text(app)

    _button(app, "preset_A").click()
    app.run(timeout=30)
    text = _outcome_text(app)
    assert "有依据回答" in text
    assert "本次请求未完成" not in text
    assert not app.exception


def test_demo_reset_returns_page_to_clean_initial_state() -> None:
    app = AppTest.from_file(PAGE_PATH).run(timeout=30)
    _button(app, "preset_A").click()
    app.run(timeout=30)
    _button(app, "src_0_" + f"{KB}:page:1").click()
    app.run(timeout=30)
    assert "来源详情" in _outcome_text(app)

    _button(app, "demo_reset").click()
    app.run(timeout=30)

    assert not app.exception
    text = _outcome_text(app)
    assert "向你的工程知识库提问" in text
    assert "有依据回答" not in text
    assert "来源详情" not in text
    source_buttons = [b for b in app.button if (b.key or "").startswith("src_")]
    assert source_buttons == []
    # reset restores the default mock demo mode (radio value is the raw option)
    assert app.radio[0].value == "mock_demo"


# ---------------------------------------------------------------------------
# Home page entry point
# ---------------------------------------------------------------------------


def _stub_home_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_dir / "knowledge.db")
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
    _stub_home_runtime(monkeypatch, tmp_path)
    app = AppTest.from_file(HOME_PATH).run(timeout=30)
    assert not app.exception
    entries = [b for b in app.button if "进入知识 Agent" in (b.label or "")]
    assert entries


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
