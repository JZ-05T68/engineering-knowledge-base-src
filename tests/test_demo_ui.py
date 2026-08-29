"""v0.6.1 demo UI presentation tests: view-model mapping without Streamlit.

Focused regression for ``src/demo_ui`` — the display-state matrix, citation
mapping (mock enrichment vs. real-API fallback), integrity semantics, safe
text presentation, preset mapping and stale-state helpers. No CSS/layout
assertions; the Streamlit page itself is covered separately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import demo_ui
from src.demo import GENERIC_WARNING, MockDemoClient
from src.demo.contracts import DemoSourceResponse
from src.demo.fixtures import NO_EVIDENCE_MESSAGE
from src.demo.presets import PRESETS
from src.demo_ui import (
    AgentMode,
    AnswerViewModel,
    AskRecord,
    DisplayOutcome,
    build_answer_view_model,
    build_failure_view_model,
    build_preset_chips,
    build_source_unavailable,
    build_source_view_model,
    classify_error_code,
    classify_outcome,
    classify_transport_failure,
    escape_text,
    failure_detail,
    is_mock_response,
    is_stale_result,
    next_request_sequence,
    short_source_id,
    source_type_label,
    split_answer_paragraph,
)
from src.hosted_api.contracts import AgentRunResponse
from src.models import ContextItemType

DEMO_UI_PATH = Path(demo_ui.__file__)
DEMO_QUESTIONS = {preset.preset_id: preset.question for preset in PRESETS}


@pytest.fixture(scope="module")
def client() -> MockDemoClient:
    return MockDemoClient()


def _answered(client: MockDemoClient, preset_id: str) -> AnswerViewModel:
    question = DEMO_QUESTIONS[preset_id]
    response = client.run_agent(question)
    metadata = {sid: client.get_source(sid) for sid in response.citations}
    return build_answer_view_model(question, response, metadata)


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------


def test_demo_ui_module_does_not_import_streamlit() -> None:
    source = DEMO_UI_PATH.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+streamlit", source, re.MULTILINE)


def test_presentation_steps_are_coarse_only() -> None:
    joined = " ".join(demo_ui.PRESENTATION_STEPS)
    for forbidden in ("推理", "思维链", "工具调用", "检索完成", "指纹核对完成"):
        assert forbidden not in joined


# ---------------------------------------------------------------------------
# Display-state mapping
# ---------------------------------------------------------------------------


def test_grounded_fixture_maps_to_grounded_outcome(client: MockDemoClient) -> None:
    view_model = _answered(client, "A")
    assert view_model.outcome is DisplayOutcome.ANSWER_GROUNDED
    assert [badge.text for badge in view_model.badges] == ["有依据回答"]
    assert view_model.citation_count == 2
    assert view_model.is_mock is True
    assert view_model.warnings == ()


def test_warning_fixture_maps_to_limited_outcome(client: MockDemoClient) -> None:
    view_model = _answered(client, "B")
    assert view_model.outcome is DisplayOutcome.ANSWER_WITH_WARNING
    assert [badge.text for badge in view_model.badges] == ["有依据回答", "来源存在限制"]
    assert GENERIC_WARNING in view_model.warnings


def test_empty_fixture_maps_to_no_evidence_outcome(client: MockDemoClient) -> None:
    view_model = _answered(client, "EMPTY")
    assert view_model.outcome is DisplayOutcome.NO_EVIDENCE
    assert view_model.citation_count == 0
    assert view_model.chips == ()
    assert view_model.hint
    assert view_model.answer_text == NO_EVIDENCE_MESSAGE


def test_failed_fixture_maps_to_failed_outcome(client: MockDemoClient) -> None:
    view_model = _answered(client, "REHEARSAL_FAILED")
    assert view_model.outcome is DisplayOutcome.FAILED
    assert view_model.failure_headline == "本次请求未完成"
    assert view_model.failure_detail
    assert view_model.paragraphs == ()
    for forbidden in ("Traceback", "Exception", "{", "tool"):
        assert forbidden not in (view_model.failure_detail or "")


def test_rate_limit_error_code_maps_to_rate_limited() -> None:
    assert classify_error_code("rate_limited") is DisplayOutcome.RATE_LIMITED
    assert classify_error_code("concurrency_limited") is DisplayOutcome.RATE_LIMITED
    assert classify_error_code("tool_failed") is DisplayOutcome.FAILED
    assert classify_error_code("runtime_unavailable") is DisplayOutcome.BACKEND_UNAVAILABLE
    assert classify_error_code("provider_unavailable") is DisplayOutcome.BACKEND_UNAVAILABLE


def test_transport_failure_status_mapping() -> None:
    assert classify_transport_failure(429) is DisplayOutcome.RATE_LIMITED
    assert classify_transport_failure(503) is DisplayOutcome.BACKEND_UNAVAILABLE
    assert classify_transport_failure(0) is DisplayOutcome.BACKEND_UNAVAILABLE
    assert classify_transport_failure(500) is DisplayOutcome.FAILED


def test_transport_failure_record_renders_unavailable_state() -> None:
    record = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question="问题",
        failure_http_status=0,
    )
    view_model = build_failure_view_model(record)
    assert view_model.outcome is DisplayOutcome.BACKEND_UNAVAILABLE
    assert "本机 Agent 服务当前不可用" in (view_model.failure_headline or "")
    assert "127.0.0.1" in (view_model.failure_detail or "")


def test_rate_limited_record_renders_rate_limit_state() -> None:
    record = AskRecord(
        sequence=1,
        mode=AgentMode.LOCAL_AGENT,
        question="问题",
        failure_code="rate_limited",
    )
    view_model = build_failure_view_model(record)
    assert view_model.outcome is DisplayOutcome.RATE_LIMITED
    assert "稍后再试" in (view_model.failure_headline or "")


def test_no_evidence_is_not_rendered_as_failure(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTIONS["EMPTY"])
    assert response.status == "completed"
    assert classify_outcome(response) is DisplayOutcome.NO_EVIDENCE


# ---------------------------------------------------------------------------
# Citation mapping
# ---------------------------------------------------------------------------


def test_mock_citations_detail_maps_display_index_and_anchor(
    client: MockDemoClient,
) -> None:
    view_model = _answered(client, "A")
    indexes = [chip.display_index for chip in view_model.chips]
    assert indexes == [1, 2]
    assert all(chip.anchor_label for chip in view_model.chips)
    assert all(chip.available for chip in view_model.chips)
    assert view_model.chips[0].title == "PID 控制器调试手册"
    assert view_model.chips[0].location == "第 12 页"
    assert view_model.chips[0].type_label == "页面资料"


def test_answer_text_preserves_mock_citation_markers(client: MockDemoClient) -> None:
    view_model = _answered(client, "A")
    assert "【来源 #1】" in view_model.answer_text
    assert "【来源 #2】" in view_model.answer_text


def test_real_api_response_falls_back_to_plain_verified_list() -> None:
    response = AgentRunResponse(
        request_id="real-1",
        status="completed",
        answer="结论一【来源 #1】。",
        grounded=True,
        citations=("0e6b1de5-0000-4000-8000-000000000001:page:1",),
        warnings=(),
        error=None,
    )
    assert is_mock_response(response) is False
    view_model = build_answer_view_model("问题", response, {})
    assert view_model.is_mock is False
    assert view_model.citation_count == 1
    chip = view_model.chips[0]
    assert chip.display_index is None
    assert chip.anchor_label is None
    assert chip.available is False
    assert chip.title == "页面资料 #1"
    # Mode 1 must not turn answer-body markers into clickable mappings.
    flattened = "".join(segment.text for segment in view_model.paragraphs[0])
    assert "【来源 #1】" in flattened


def test_real_metadata_enriches_mode1_chips_without_numbering() -> None:
    response = AgentRunResponse(
        request_id="real-2",
        status="completed",
        answer="结论。",
        grounded=True,
        citations=("0e6b1de5-0000-4000-8000-000000000001:page:1",),
        warnings=(),
        error=None,
    )
    metadata = {
        response.citations[0]: DemoSourceResponse(
            stable_id=response.citations[0],
            type=ContextItemType.PAGE,
            title="调试手册",
            label="第 3 页",
        )
    }
    chip = build_answer_view_model("问题", response, metadata).chips[0]
    assert chip.title == "调试手册"
    assert chip.location == "第 3 页"
    assert chip.display_index is None


def test_split_answer_paragraph_keeps_unknown_marker_literal() -> None:
    segments = split_answer_paragraph("前文【来源 #9】后文", render_markers=True, citation_count=2)
    flattened = "".join(segment.text for segment in segments)
    assert "【来源 #9】" in flattened


def test_split_answer_paragraph_disabled_keeps_text_whole() -> None:
    segments = split_answer_paragraph("前文【来源 #1】后文", render_markers=False, citation_count=1)
    assert len(segments) == 1
    assert segments[0].kind == "text"


# ---------------------------------------------------------------------------
# Source viewer semantics
# ---------------------------------------------------------------------------


def test_source_type_labels_use_product_language() -> None:
    assert source_type_label(ContextItemType.PAGE) == "页面资料"
    assert source_type_label(ContextItemType.KNOWLEDGE_OBJECT) == "知识对象"
    assert source_type_label(ContextItemType.KNOWLEDGE_MEMORY) == "知识记忆"
    assert source_type_label(ContextItemType.EVIDENCE) == "证据"
    assert source_type_label("page") == "页面资料"
    assert source_type_label("unknown_kind") == "unknown_kind"


def test_short_source_id_falls_back_without_leaking_uuid() -> None:
    assert short_source_id("0e6b1de5-0000-4000-8000-000000000001:page:7") == "页面资料 #7"
    assert "0e6b1de5" not in short_source_id("0e6b1de5-0000-4000-8000-000000000001:page:7")
    assert short_source_id("not-a-valid-id") == "来源详情暂不可用"


def test_source_viewer_renders_demo_integrity_state(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTIONS["C"])
    metadata = {sid: client.get_source(sid) for sid in response.citations}
    view_model = build_answer_view_model(DEMO_QUESTIONS["C"], response, metadata)
    source = build_source_view_model(view_model.chips[0], metadata[view_model.chips[0].stable_id])
    assert source.integrity is not None
    assert source.integrity.label == "来源发生变化"
    assert source.note is not None and "演示预置状态" in source.note
    # changed is a snapshot mismatch, explicitly disclaimed, never a moral claim.
    assert "不代表" in source.integrity.explanation
    assert "不可信" not in source.integrity.label
    assert source.integrity.tone == "warn"


def test_source_viewer_without_integrity_shows_mode1_note(
    client: MockDemoClient,
) -> None:
    response = client.run_agent(DEMO_QUESTIONS["A"])
    metadata = {sid: client.get_source(sid) for sid in response.citations}
    view_model = build_answer_view_model(DEMO_QUESTIONS["A"], response, metadata)
    source = build_source_view_model(view_model.chips[0], metadata[view_model.chips[0].stable_id])
    assert source.integrity is None
    assert source.integrity_note == demo_ui.LIVE_INTEGRITY_NOTE


def test_source_viewer_tolerates_null_title_and_label() -> None:
    stable_id = "0e6b1de5-0000-4000-8000-000000000001:page:1"
    response = AgentRunResponse(
        request_id="real-3",
        status="completed",
        answer="结论。",
        grounded=True,
        citations=(stable_id,),
        warnings=(),
        error=None,
    )
    # Mode 1 metadata with null title/label: chip falls back to the short
    # public id form and the viewer keeps the location fallback.
    metadata = {
        stable_id: DemoSourceResponse(
            stable_id=stable_id, type=ContextItemType.PAGE, title=None, label=None
        )
    }
    view_model = build_answer_view_model("问题", response, metadata)
    chip = view_model.chips[0]
    assert chip.title == "页面资料 #1"
    assert chip.location is None
    source = build_source_view_model(chip, metadata[stable_id])
    assert source.title == "页面资料 #1"
    assert source.location == demo_ui.LOCATION_FALLBACK


def test_source_unavailable_is_separate_from_answer_state(
    client: MockDemoClient,
) -> None:
    response = client.run_agent(DEMO_QUESTIONS["A"])
    view_model = build_answer_view_model(DEMO_QUESTIONS["A"], response, {})
    unavailable = build_source_unavailable(view_model.chips[0])
    assert unavailable.unavailable is True
    assert unavailable.note == demo_ui.SOURCE_UNAVAILABLE_NOTE
    assert unavailable.title == view_model.chips[0].title


# ---------------------------------------------------------------------------
# Presets, helpers and safety
# ---------------------------------------------------------------------------


def test_preset_chips_map_three_main_questions() -> None:
    chips = build_preset_chips()
    assert len(chips) == 6
    mains = [chip for chip in chips if chip.main]
    assert [chip.preset_id for chip in mains] == ["A", "B", "C"]
    backups = [chip for chip in chips if not chip.main]
    assert {chip.preset_id for chip in backups} == {"A2", "EMPTY", "REHEARSAL_FAILED"}


def test_preset_chips_use_human_scenario_titles() -> None:
    # judge-facing titles never leak fixture identifiers
    for chip in build_preset_chips():
        assert "_" not in chip.short_label
        assert chip.short_label not in ("success_grounded_a", "partial_warning_b")
        assert chip.question == DEMO_QUESTIONS[chip.preset_id]


def test_source_viewer_support_note_mock_dedup_or_anchor() -> None:
    client = MockDemoClient()
    # Scenario A anchor repeats title·page — deduplicated to the verified line.
    response = client.run_agent(DEMO_QUESTIONS["A"])
    metadata = {sid: client.get_source(sid) for sid in response.citations}
    view_model = build_answer_view_model(DEMO_QUESTIONS["A"], response, metadata)
    source = build_source_view_model(view_model.chips[0], metadata[view_model.chips[0].stable_id])
    assert source.support_note == demo_ui.VERIFIED_SUPPORT_NOTE
    # Scenario C anchor (完整性检查目标) is distinct and stays.
    response_c = client.run_agent(DEMO_QUESTIONS["C"])
    metadata_c = {sid: client.get_source(sid) for sid in response_c.citations}
    view_model_c = build_answer_view_model(DEMO_QUESTIONS["C"], response_c, metadata_c)
    source_c = build_source_view_model(
        view_model_c.chips[0], metadata_c[view_model_c.chips[0].stable_id]
    )
    assert source_c.support_note == "完整性检查目标"


def test_source_viewer_support_note_mode1_falls_back_to_verified_line() -> None:
    stable_id = "0e6b1de5-0000-4000-8000-000000000001:page:1"
    response = AgentRunResponse(
        request_id="real-4",
        status="completed",
        answer="结论。",
        grounded=True,
        citations=(stable_id,),
        warnings=(),
        error=None,
    )
    metadata = {
        stable_id: DemoSourceResponse(
            stable_id=stable_id,
            type=ContextItemType.PAGE,
            title="调试手册",
            label="第 3 页",
        )
    }
    view_model = build_answer_view_model("问题", response, metadata)
    source = build_source_view_model(view_model.chips[0], metadata[stable_id])
    assert source.support_note == demo_ui.VERIFIED_SUPPORT_NOTE


def test_mode_labels_identify_mock_mode_visibly() -> None:
    assert AgentMode.MOCK_DEMO.badge == "预置离线演示"
    assert "预置演示数据" in AgentMode.MOCK_DEMO.caption
    assert AgentMode.LOCAL_AGENT.badge == "本机 Agent"
    # judge-visible copy stays jargon-free
    for jargon in ("mock_demo", "/v0.6", "HTTP", "DTO", "loopback"):
        assert jargon not in AgentMode.MOCK_DEMO.caption
        assert jargon not in AgentMode.LOCAL_AGENT.caption
        assert jargon not in AgentMode.MOCK_DEMO.label
        assert jargon not in AgentMode.LOCAL_AGENT.label


def test_stale_state_helpers() -> None:
    assert next_request_sequence(0) == 1
    assert is_stale_result(1, 2) is True
    assert is_stale_result(2, 2) is False


def test_escape_text_neutralizes_html() -> None:
    escaped = escape_text("<img src=x onerror=alert(1)>")
    assert "<img" not in escaped
    assert "&lt;img" in escaped


def test_no_numeric_confidence_in_any_state_copy() -> None:
    for outcome in DisplayOutcome:
        text = failure_detail(outcome, "服务处理请求失败。")
        assert not re.search(r"\d{2}%|\d+/100", text)
