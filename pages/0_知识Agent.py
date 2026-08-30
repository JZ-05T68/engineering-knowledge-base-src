"""EKB Knowledge Agent Workspace — v0.6.1 competition demo surface.

Dedicated competition-facing page (not an admin page): question → grounded
answer → verified sources → source viewer → trust boundary, in one screen.

Frontend ownership notes for later polish:

- ``src/demo_ui.py`` owns every display mapping (states, badges, labels,
  citation chips, session-key contracts). Keep this page free of business
  mapping logic; it only orchestrates Streamlit widgets and renders the
  view models.
- ``src/agent_client.py`` owns the Mode 1 loopback transport; Mode 2 uses
  the frozen ``src/demo`` mock client. Never add provider/tool fields or a
  second response schema here.
- All custom HTML goes through ``demo_ui.escape_text``; answers never render
  DTO JSON, internal ids, paths or traces.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import streamlit as st

from src import demo_ui
from src.agent_client import DEFAULT_LOCAL_BASE_URL, HostedAgentClient
from src.demo import MockDemoClient
from src.demo.contracts import DemoHTTPError
from src.demo_ui import AgentMode, AskRecord, DisplayOutcome

LOGGER = logging.getLogger(__name__)

PAGE_VERSION_LINE = "v0.6.1 · Competition Demo Experience · 本地 · 离线 · 信任感知"

MODE_KEY = "agent_mode"
QUESTION_INPUT_KEY = "agent_question_input"
PENDING_KEY = "agent_pending_question"
SEQ_KEY = "agent_request_seq"
RESULT_KEY = "agent_result"
SELECTED_KEY = "agent_selected_source_id"
LIVE_BASE_KEY = "agent_base_url"
MOCK_CLIENT_KEY = "agent_mock_client"
LIVE_CLIENT_KEY = "agent_live_client"

_CSS = """
/* EKB Agent Workspace — page-local styles only (ekb-aw-* namespace). */
div[data-testid="stMainBlockContainer"] { padding-top: 0.9rem; }
@media (min-width: 1600px) { div[data-testid="stMainBlockContainer"] { max-width: 1760px; } }
/* Hide the Streamlit toolbar chrome (Deploy menu / status widget) on this
   competition surface only; sidebar navigation stays reachable. */
div[data-testid="stToolbar"] { display: none; }
/* The fixed Streamlit header bar is opaque white and, at this page's compact
   0.9rem top padding, would cover the brand kicker. Keep it transparent so
   the brand line shows while the sidebar toggle stays reachable. */
[data-testid="stHeader"] { background-color: transparent; }

/* --- header & brand --- */
.ekb-aw-header { display: flex; align-items: flex-end; justify-content: space-between;
    gap: 1.5rem; padding-bottom: 0.65rem; margin-bottom: 0.6rem;
    border-bottom: 1px solid #e2e8f0; }
.ekb-aw-brand { font-size: 12.5px; font-weight: 800; letter-spacing: 0.14em;
    color: #2563eb; text-transform: uppercase; }
.ekb-aw-title { font-size: 26px; font-weight: 800; color: #0f172a; margin: 2px 0 3px; }
.ekb-aw-sub { font-size: 13px; color: #64748b; }
.ekb-aw-mode-badge { display: inline-flex; align-items: center; gap: 7px;
    padding: 6px 14px; border-radius: 999px; font-size: 13px; font-weight: 800;
    white-space: nowrap; }
.ekb-aw-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.ekb-aw-mode-mock { background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; }
.ekb-aw-mode-mock .ekb-aw-dot { background: #d97706; }
.ekb-aw-mode-live { background: #ecfdf5; color: #166534; border: 1px solid #a7f3d0; }
.ekb-aw-mode-live .ekb-aw-dot { background: #16a34a; }

.ekb-aw-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px;
    padding: 16px 22px; margin-bottom: 12px; }
.ekb-aw-q-kicker { font-size: 12px; font-weight: 800; color: #2563eb;
    letter-spacing: 0.1em; margin-bottom: 3px; }
.ekb-aw-q-text { font-size: 18.5px; font-weight: 750; color: #0f172a;
    line-height: 1.55; margin-bottom: 8px; }
.ekb-aw-badges { display: flex; flex-wrap: wrap; gap: 8px; margin: 2px 0 10px;
    align-items: center; }
.ekb-aw-badge { display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 999px; font-size: 13px; font-weight: 750; }
.ekb-aw-badge-ok { background: #ecfdf5; color: #15803d; border: 1px solid #a7f3d0; }
.ekb-aw-badge-warn { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.ekb-aw-badge-empty { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }
.ekb-aw-badge-error { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.ekb-aw-badge-muted { background: #f8fafc; color: #64748b; border: 1px solid #e2e8f0; }
.ekb-aw-count { display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 12px; border-radius: 999px; font-size: 12.5px; font-weight: 700;
    background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }

.ekb-aw-answer { font-size: 16.5px; line-height: 1.9; color: #1e293b; }
.ekb-aw-answer p { margin: 0 0 10px; }
.ekb-aw-answer p:last-child { margin-bottom: 2px; }
.ekb-aw-cite { display: inline-flex; align-items: center; padding: 0 9px;
    margin: 0 2px; border-radius: 999px; background: #eff6ff; color: #1d4ed8;
    border: 1px solid #bfdbfe; font-size: 12.5px; font-weight: 800; line-height: 1.75; }

.ekb-aw-warn-banner { display: flex; gap: 8px; align-items: flex-start;
    background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
    border-radius: 10px; padding: 10px 14px; font-size: 14px; margin: 8px 0 4px;
    line-height: 1.6; }

.ekb-aw-empty { text-align: center; padding: 30px 26px; border-style: dashed; }
.ekb-aw-empty-icon { font-size: 28px; color: #94a3b8; margin-bottom: 6px; font-weight: 800; }
.ekb-aw-empty-title { font-size: 18px; font-weight: 800; color: #334155; margin-bottom: 8px; }
.ekb-aw-empty p { font-size: 14.5px; color: #475569; line-height: 1.8; margin: 0 0 6px; }
.ekb-aw-hint { color: #64748b; font-size: 13px !important; }

.ekb-aw-fail { border-left: 4px solid #b91c1c; }
.ekb-aw-fail-title { font-size: 17px; font-weight: 800; color: #b91c1c; margin-bottom: 6px; }
.ekb-aw-fail p { color: #475569; font-size: 14.5px; line-height: 1.75; margin: 0 0 6px; }

.ekb-aw-idle-title { font-size: 19px; font-weight: 800; color: #0f172a; margin-bottom: 6px; }
.ekb-aw-idle p { font-size: 14.5px; color: #475569; line-height: 1.7; margin: 0; }
.ekb-aw-idle ul { list-style: none; padding: 0; margin: 12px 0 0; }
.ekb-aw-idle li { padding: 8px 2px; font-size: 14.5px; color: #334155;
    border-top: 1px dashed #e2e8f0; line-height: 1.6; }
.ekb-aw-idle li:first-child { border-top: none; }

.ekb-aw-panel-head { display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 8px; }
.ekb-aw-panel-title { font-size: 15.5px; font-weight: 800; color: #0f172a; }
.ekb-aw-count-badge { font-size: 12.5px; font-weight: 700; color: #1d4ed8;
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 999px; padding: 3px 10px; }
.ekb-aw-selected-flag { font-size: 12.5px; font-weight: 800; color: #1d4ed8;
    margin: 10px 0 2px; }
.ekb-aw-panel-empty { text-align: center; color: #64748b; padding: 24px 18px;
    border: 1px dashed #cbd5e1; border-radius: 12px; font-size: 13.5px;
    line-height: 1.8; background: #f8fafc; }

.ekb-aw-viewer { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px;
    padding: 14px 18px; margin-top: 12px; }
.ekb-aw-viewer-kicker { font-size: 11.5px; font-weight: 800; color: #2563eb;
    letter-spacing: 0.1em; margin-bottom: 5px; }
.ekb-aw-viewer-title { font-size: 16.5px; font-weight: 800; color: #0f172a;
    line-height: 1.5; margin-bottom: 8px; }
.ekb-aw-chiprow { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.ekb-aw-chip { font-size: 12.5px; font-weight: 700; color: #475569; background: #ffffff;
    border: 1px solid #cbd5e1; border-radius: 999px; padding: 2px 10px; }
.ekb-aw-viewer-row { font-size: 13.5px; color: #475569; margin-bottom: 4px; line-height: 1.6; }
.ekb-aw-viewer-row b { color: #334155; }
.ekb-aw-support { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af;
    border-radius: 10px; padding: 9px 12px; font-size: 13.5px; line-height: 1.7;
    margin-top: 8px; }
.ekb-aw-support b { display: block; margin-bottom: 2px; }
.ekb-aw-integrity { border-radius: 10px; padding: 10px 13px; font-size: 13.5px;
    margin-top: 8px; line-height: 1.7; }
.ekb-aw-integrity-ok { background: #ecfdf5; border: 1px solid #a7f3d0; color: #166534; }
.ekb-aw-integrity-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
.ekb-aw-integrity-error { background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; }
.ekb-aw-integrity-muted { background: #f1f5f9; border: 1px solid #e2e8f0; color: #64748b; }
.ekb-aw-integrity b { display: block; margin-bottom: 2px; }
.ekb-aw-note { font-size: 12.5px; color: #64748b; margin-top: 8px; line-height: 1.7; }

.ekb-aw-section-label { font-size: 13px; font-weight: 700; color: #334155;
    margin: 8px 0 6px; }
.ekb-aw-preset-q { font-size: 12.5px; color: #64748b; line-height: 1.55;
    margin: 4px 2px 0; min-height: 2.6em; overflow: hidden; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
"""


# ---------------------------------------------------------------------------
# Session-state callbacks (run before the widgets re-instantiate)
# ---------------------------------------------------------------------------


def _clear_result_state() -> None:
    for key in demo_ui.STATE_KEYS_CLEARED_ON_MODE_SWITCH:
        st.session_state.pop(key, None)


def _on_mode_change() -> None:
    _clear_result_state()


def _on_base_url_change() -> None:
    st.session_state.pop(LIVE_CLIENT_KEY, None)
    _clear_result_state()


def _queue_question(question: str) -> None:
    """Queue one ask from a callback (widget keys still assignable there)."""

    text = question.strip()
    if not text:
        return
    st.session_state[QUESTION_INPUT_KEY] = text
    st.session_state[PENDING_KEY] = text
    for key in demo_ui.STATE_KEYS_CLEARED_ON_ASK:
        st.session_state.pop(key, None)


def _select_source(stable_id: str) -> None:
    st.session_state[SELECTED_KEY] = stable_id


def _switch_to_mock(question: str) -> None:
    st.session_state[MODE_KEY] = AgentMode.MOCK_DEMO
    _clear_result_state()
    _queue_question(question)


def _reset_demo() -> None:
    """Operator reset: clear ALL demo presentation state, nothing else.

    Clears session keys only (mode, result, selected source, request
    sequence, live base URL override). Never touches the database, files,
    fixtures or services.
    """

    for key in demo_ui.AGENT_SESSION_KEYS:
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Client seam
# ---------------------------------------------------------------------------


def _resolve_client(mode: AgentMode) -> Any:
    if mode is AgentMode.MOCK_DEMO:
        client = st.session_state.get(MOCK_CLIENT_KEY)
        if client is None:
            client = MockDemoClient()
            st.session_state[MOCK_CLIENT_KEY] = client
        return client
    base_url = (st.session_state.get(LIVE_BASE_KEY) or DEFAULT_LOCAL_BASE_URL).strip()
    cached = st.session_state.get(LIVE_CLIENT_KEY)
    if cached is not None and cached.base_url == base_url:
        return cached
    client = HostedAgentClient(base_url)
    st.session_state[LIVE_CLIENT_KEY] = client
    return client


# ---------------------------------------------------------------------------
# Ask execution (one place; coarse presentation states only)
# ---------------------------------------------------------------------------


def _execute_ask(question: str, mode: AgentMode, client: Any) -> None:
    """Run one ask synchronously and store a sequence-guarded record.

    The status labels are frontend presentation states only — the backend
    exposes no chain-of-thought, tool trace or step events, and none is
    claimed here. The pacing between labels is visual; mock responses
    themselves are instantaneous.
    """

    sequence = demo_ui.next_request_sequence(st.session_state.get(SEQ_KEY, 0))
    st.session_state[SEQ_KEY] = sequence
    steps = demo_ui.PRESENTATION_STEPS
    pace = demo_ui.PRESENTATION_PACE_SECONDS
    with st.status("正在处理问题…", expanded=True) as status:
        st.write(steps[0])
        time.sleep(pace)
        try:
            st.write(steps[1])
            time.sleep(pace)
            response = client.run_agent(question)
            st.write(steps[2])
            time.sleep(pace)
        except DemoHTTPError as error:
            failure = error.failure
            st.session_state[RESULT_KEY] = AskRecord(
                sequence=sequence,
                mode=mode,
                question=question,
                failure_code=failure.error.code,
                failure_http_status=error.http_status,
                failure_message=failure.error.message,
            )
            status.update(label="请求未能完成", state="error", expanded=False)
            return
        except Exception:
            # Unexpected local errors still render the safe failed state;
            # the traceback stays in the local log, never in the UI.
            LOGGER.exception("Agent 请求发生未预期错误")
            st.session_state[RESULT_KEY] = AskRecord(
                sequence=sequence,
                mode=mode,
                question=question,
                failure_code="internal_failure",
            )
            status.update(label="请求未能完成", state="error", expanded=False)
            return
        metadata: dict[str, Any] = {}
        unavailable: list[str] = []
        if response.citations:
            st.write(steps[3])
            for stable_id in response.citations:
                try:
                    metadata[stable_id] = client.get_source(stable_id)
                except DemoHTTPError:
                    LOGGER.warning("来源详情获取失败：%s", stable_id)
                    metadata[stable_id] = None
                    unavailable.append(stable_id)
        status.update(label="回答就绪", state="complete", expanded=False)
        st.session_state[RESULT_KEY] = AskRecord(
            sequence=sequence,
            mode=mode,
            question=question,
            response=response,
            source_metadata=metadata,
            source_unavailable=tuple(unavailable),
        )


# ---------------------------------------------------------------------------
# HTML fragments (all dynamic text goes through demo_ui.escape_text)
# ---------------------------------------------------------------------------


def _badges_html(view_model: Any) -> str:
    parts = [
        f"<span class='ekb-aw-badge ekb-aw-badge-{badge.tone}'>"
        f"<span aria-hidden='true'>{badge.icon}</span>{demo_ui.escape_text(badge.text)}</span>"
        for badge in view_model.badges
    ]
    if view_model.chips:
        parts.append(
            f"<span class='ekb-aw-count'>引用来源 {view_model.citation_count} 条</span>"
        )
    if view_model.is_mock:
        parts.append("<span class='ekb-aw-badge ekb-aw-badge-muted'>预置演示数据</span>")
    return f"<div class='ekb-aw-badges'>{''.join(parts)}</div>"


def _answer_paragraphs_html(view_model: Any) -> str:
    paragraphs: list[str] = []
    for segments in view_model.paragraphs:
        pieces: list[str] = []
        for segment in segments:
            if segment.kind == "citation" and segment.index is not None:
                pieces.append(
                    f"<span class='ekb-aw-cite'>来源 #{segment.index}</span>"
                )
            else:
                pieces.append(demo_ui.escape_text(segment.text))
        paragraphs.append(f"<p>{''.join(pieces)}</p>")
    return f"<div class='ekb-aw-answer'>{''.join(paragraphs)}</div>"


def _question_card_open_html(view_model: Any) -> str:
    return (
        "<div class='ekb-aw-card'>"
        "<div class='ekb-aw-q-kicker'>当前问题</div>"
        f"<div class='ekb-aw-q-text'>{demo_ui.escape_text(view_model.question)}</div>"
    )


def _render_idle_card() -> None:
    st.markdown(
        "<div class='ekb-aw-card' style='padding:22px 26px'>"
        "<div class='ekb-aw-idle-title'>向你的工程知识库提问</div>"
        "<p>知识 Agent 只基于知识库中已登记的资料回答，并给出可回源的引用。</p>"
        "<ul>"
        "<li><b>✓ 有依据才回答</b> —— 每条回答都标注是否有资料支持</li>"
        "<li><b>▣ 来源可核验</b> —— 点击引用即可查看来源标题、类型与位置</li>"
        "<li><b>⚠ 诚实边界</b> —— 无依据、来源变化、请求失败都会明示，不会编造</li>"
        "</ul></div>",
        unsafe_allow_html=True,
    )


def _render_grounded_answer(view_model: Any) -> None:
    html_parts = [_question_card_open_html(view_model), _badges_html(view_model)]
    if view_model.warnings:
        for warning in view_model.warnings:
            html_parts.append(
                "<div class='ekb-aw-warn-banner'><span aria-hidden='true'>⚠</span>"
                f"<span>{demo_ui.escape_text(warning)}</span></div>"
            )
    html_parts.append(_answer_paragraphs_html(view_model))
    html_parts.append("</div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)
    if view_model.chips:
        st.markdown(
            "<div class='ekb-aw-section-label'>引用来源 · 点击在右侧查看详情</div>",
            unsafe_allow_html=True,
        )
        selected_id = st.session_state.get(SELECTED_KEY)
        for row_start in range(0, len(view_model.chips), 3):
            row = view_model.chips[row_start : row_start + 3]
            columns = st.columns(3)
            for column, chip in zip(columns, row, strict=False):
                selected = selected_id == chip.stable_id
                prefix = f"#{chip.display_index}" if chip.display_index else "来源"
                column.button(
                    f"{'▸ ' if selected else ''}{prefix} · {chip.title}",
                    key=f"cite_{chip.stable_id}_{chip.display_index or 'n'}",
                    on_click=_select_source,
                    args=(chip.stable_id,),
                    use_container_width=True,
                    type="primary" if selected else "secondary",
                )
                location = chip.location or demo_ui.LOCATION_FALLBACK
                column.markdown(
                    "<div class='ekb-aw-preset-q'>"
                    f"{demo_ui.escape_text(f'{chip.type_label} · {location}')}</div>",
                    unsafe_allow_html=True,
                )


def _render_empty_state(view_model: Any, preset_chips: tuple[Any, ...]) -> None:
    st.markdown(
        _question_card_open_html(view_model)
        + _badges_html(view_model)
        + "<div class='ekb-aw-card ekb-aw-empty'>"
        + "<div class='ekb-aw-empty-icon' aria-hidden='true'>○</div>"
        + "<div class='ekb-aw-empty-title'>当前知识库中没有找到足够资料支持这个问题。</div>"
        + f"<p>{demo_ui.escape_text(view_model.answer_text)}</p>"
        + "<p class='ekb-aw-hint'>"
        + demo_ui.escape_text(view_model.hint or "")
        + "</p></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='ekb-aw-section-label'>换一条演示问题试试</div>",
        unsafe_allow_html=True,
    )
    columns = st.columns(3)
    for column, chip in zip(columns, (c for c in preset_chips if c.main), strict=True):
        column.button(
            f"{chip.preset_id} · {chip.short_label}",
            key=f"suggest_{chip.preset_id}",
            on_click=_queue_question,
            args=(chip.question,),
            use_container_width=True,
        )


def _render_failure_state(view_model: Any, mode: AgentMode, question: str) -> None:
    st.markdown(
        _question_card_open_html(view_model)
        + _badges_html(view_model)
        + "<div class='ekb-aw-fail' style='border-radius:10px;padding:12px 16px'>"
        + "<div class='ekb-aw-fail-title'>"
        + demo_ui.escape_text(view_model.failure_headline or "本次请求失败")
        + "</div><p>"
        + demo_ui.escape_text(view_model.failure_detail or "")
        + "</p></div></div>",
        unsafe_allow_html=True,
    )
    actions = st.columns([1, 1.6])
    actions[0].button(
        "重试",
        key="failure_retry",
        on_click=_queue_question,
        args=(question,),
        use_container_width=True,
    )
    if mode is AgentMode.LOCAL_AGENT:
        actions[1].button(
            "切换到预置离线演示",
            key="failure_switch_mock",
            on_click=_switch_to_mock,
            args=(question,),
            use_container_width=True,
        )


def _render_answer_area(preset_chips: tuple[Any, ...]) -> None:
    record = st.session_state.get(RESULT_KEY)
    sequence = st.session_state.get(SEQ_KEY, 0)
    if record is None or demo_ui.is_stale_result(record.sequence, sequence):
        _render_idle_card()
        return
    if record.transport_failed:
        view_model = demo_ui.build_failure_view_model(record)
    else:
        view_model = demo_ui.build_answer_view_model(
            record.question, record.response, record.source_metadata
        )
    if view_model.outcome is DisplayOutcome.NO_EVIDENCE:
        _render_empty_state(view_model, preset_chips)
    elif view_model.outcome in (
        DisplayOutcome.FAILED,
        DisplayOutcome.RATE_LIMITED,
        DisplayOutcome.BACKEND_UNAVAILABLE,
    ):
        _render_failure_state(view_model, record.mode, record.question)
    else:
        _render_grounded_answer(view_model)


# ---------------------------------------------------------------------------
# Right panel: verified sources + source viewer
# ---------------------------------------------------------------------------


def _viewer_html(source: Any) -> str:
    chip_row = [
        f"<span class='ekb-aw-chip'>{demo_ui.escape_text(source.type_label)}</span>",
        f"<span class='ekb-aw-chip'>{demo_ui.escape_text(source.location)}</span>",
    ]
    if source.citation_index is not None:
        chip_row.append(f"<span class='ekb-aw-chip'>引用 #{source.citation_index}</span>")
    support_html = ""
    if source.support_note and not source.unavailable:
        support_html = (
            "<div class='ekb-aw-support'><b>为什么与回答有关</b>"
            f"{demo_ui.escape_text(source.support_note)}</div>"
        )
    integrity_html = ""
    if source.integrity is not None:
        integrity_html = (
            f"<div class='ekb-aw-integrity ekb-aw-integrity-{source.integrity.tone}'>"
            f"<b>来源状态 · {demo_ui.escape_text(source.integrity.label)}</b>"
            f"{demo_ui.escape_text(source.integrity.explanation)}</div>"
        )
    elif source.integrity_note:
        integrity_html = (
            "<div class='ekb-aw-integrity ekb-aw-integrity-muted'>"
            f"<b>来源状态</b>{demo_ui.escape_text(source.integrity_note)}</div>"
        )
    note_html = ""
    if source.note:
        note_html = f"<div class='ekb-aw-note'>{demo_ui.escape_text(source.note)}</div>"
    return (
        "<div class='ekb-aw-viewer'>"
        "<div class='ekb-aw-viewer-kicker'>来源详情 · 已验证</div>"
        f"<div class='ekb-aw-viewer-title'>{demo_ui.escape_text(source.title)}</div>"
        f"<div class='ekb-aw-chiprow'>{''.join(chip_row)}</div>"
        f"{support_html}{integrity_html}{note_html}"
        "</div>"
    )


def _render_sources_panel() -> None:
    record = st.session_state.get(RESULT_KEY)
    sequence = st.session_state.get(SEQ_KEY, 0)
    fresh = record is not None and not demo_ui.is_stale_result(record.sequence, sequence)
    chips: tuple[Any, ...] = ()
    if fresh and not record.transport_failed:
        chips = demo_ui.build_citation_chips(record.response, record.source_metadata)
    st.markdown(
        "<div class='ekb-aw-panel-head'><span class='ekb-aw-panel-title'>已验证来源</span>"
        f"<span class='ekb-aw-count-badge'>{len(chips)} 条</span></div>",
        unsafe_allow_html=True,
    )
    if not chips:
        if not fresh:
            hint = (
                "运行一个演示问题后，这里会显示回答的每一条依据，点击即可查看详情。"
                "EKB 的关键结论可以回到来源。"
            )
        elif record is not None and (
            record.transport_failed or record.response.status == "failed"
        ):
            hint = "请求未完成，暂时没有来源可显示。"
        else:
            hint = "本次回答没有引用来源：知识库中未找到可支持资料，因此不展示任何引用。"
        st.markdown(
            f"<div class='ekb-aw-panel-empty'>{demo_ui.escape_text(hint)}</div>",
            unsafe_allow_html=True,
        )
        return
    unavailable = set(record.source_unavailable)
    for position, chip in enumerate(chips):
        selected = st.session_state.get(SELECTED_KEY) == chip.stable_id
        if selected:
            st.markdown(
                "<div class='ekb-aw-selected-flag'>正在查看 ▸</div>",
                unsafe_allow_html=True,
            )
        prefix = f"#{chip.display_index}" if chip.display_index else "来源"
        location = chip.location or demo_ui.LOCATION_FALLBACK
        st.button(
            f"{prefix} · {chip.title}",
            key=f"src_{position}_{chip.stable_id}",
            on_click=_select_source,
            args=(chip.stable_id,),
            use_container_width=True,
            type="primary" if selected else "secondary",
        )
        caption = f"{chip.type_label} · {location}"
        if chip.stable_id in unavailable:
            caption += " · 该来源暂时不可检查"
        st.caption(caption)
    selected_id = st.session_state.get(SELECTED_KEY)
    selected_chip = next((c for c in chips if c.stable_id == selected_id), None)
    if selected_chip is None:
        st.markdown(
            "<div class='ekb-aw-panel-empty'>点击上方一条来源，查看它为什么支持这条回答。</div>",
            unsafe_allow_html=True,
        )
        return
    metadata = record.source_metadata.get(selected_chip.stable_id)
    if metadata is None:
        viewer = demo_ui.build_source_unavailable(selected_chip)
    else:
        viewer = demo_ui.build_source_view_model(selected_chip, metadata)
    st.markdown(_viewer_html(viewer), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="知识 Agent · 工程知识库",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)

preset_chips = demo_ui.build_preset_chips()

mode = st.session_state.get(MODE_KEY)
if not isinstance(mode, AgentMode):
    mode = AgentMode.MOCK_DEMO

mode_class = "ekb-aw-mode-mock" if mode is AgentMode.MOCK_DEMO else "ekb-aw-mode-live"
st.markdown(
    "<div class='ekb-aw-header'><div>"
    "<div class='ekb-aw-brand'>EKB · Engineering Knowledge Base</div>"
    "<div class='ekb-aw-title'>知识 Agent 工作台</div>"
    f"<div class='ekb-aw-sub'>{demo_ui.escape_text(PAGE_VERSION_LINE)}</div>"
    "</div><div>"
    f"<span class='ekb-aw-mode-badge {mode_class}'>"
    "<span class='ekb-aw-dot' aria-hidden='true'></span>"
    f"{demo_ui.escape_text(mode.badge)}</span>"
    "</div></div>",
    unsafe_allow_html=True,
)

mode_row = st.columns([2.4, 1])
mode = mode_row[0].radio(
    "运行模式",
    options=list(AgentMode),
    format_func=lambda item: item.label,
    horizontal=True,
    key=MODE_KEY,
    on_change=_on_mode_change,
)
if mode is AgentMode.LOCAL_AGENT:
    with mode_row[1].expander("本机服务连接"):
        st.session_state.setdefault(LIVE_BASE_KEY, DEFAULT_LOCAL_BASE_URL)
        st.text_input(
            "服务地址（仅限 127.0.0.1）",
            key=LIVE_BASE_KEY,
            on_change=_on_base_url_change,
        )
        st.caption("默认 http://127.0.0.1:8000，仅限本机回环地址。")
st.caption(mode.caption)

with st.form("agent_ask_form", border=True):
    composer_columns = st.columns([5, 1])
    question_value = composer_columns[0].text_input(
        "你的工程问题",
        key=QUESTION_INPUT_KEY,
        placeholder="例如：PID 调整时，比例项与积分项分别影响什么？",
        label_visibility="collapsed",
    )
    submitted = composer_columns[1].form_submit_button(
        "提问", type="primary", use_container_width=True
    )
if submitted:
    text = (question_value or "").strip()
    if text:
        st.session_state[PENDING_KEY] = text
        for key in demo_ui.STATE_KEYS_CLEARED_ON_ASK:
            st.session_state.pop(key, None)
    else:
        st.info("请先输入一个工程问题，或点击下方演示题卡。")

st.markdown(
    "<div class='ekb-aw-section-label'>演示场景 · 一键提问</div>",
    unsafe_allow_html=True,
)
main_columns = st.columns(3)
for column, chip in zip(
    main_columns, (chip for chip in preset_chips if chip.main), strict=True
):
    column.button(
        f"{chip.preset_id} · {chip.short_label}",
        key=f"preset_{chip.preset_id}",
        on_click=_queue_question,
        args=(chip.question,),
        use_container_width=True,
    )
    column.markdown(
        f"<div class='ekb-aw-preset-q'>{demo_ui.escape_text(chip.question)}</div>",
        unsafe_allow_html=True,
    )
with st.expander("更多演示问题（备用与排练）"):
    secondary_columns = st.columns(3)
    for column, chip in zip(
        secondary_columns, (chip for chip in preset_chips if not chip.main), strict=True
    ):
        column.button(
            f"{chip.tag} · {chip.short_label}",
            key=f"preset_{chip.preset_id}",
            on_click=_queue_question,
            args=(chip.question,),
            use_container_width=True,
        )
        column.markdown(
            f"<div class='ekb-aw-preset-q'>{demo_ui.escape_text(chip.question)}</div>",
            unsafe_allow_html=True,
        )

body = st.columns([1.62, 1], gap="medium")
with body[0]:
    pending = st.session_state.pop(PENDING_KEY, None)
    if pending is not None:
        try:
            client = _resolve_client(mode)
        except ValueError as error:
            st.error(str(error))
            st.stop()
        _execute_ask(pending, mode, client)
    _render_answer_area(preset_chips)
with body[1]:
    _render_sources_panel()

# Compact operator area: backstage control, deliberately not a primary
# judge-facing button. Reset clears demo session state only.
with st.expander("演示操作"):
    st.caption(
        "重置演示只清除本页会话状态（结果、选中来源、请求序号与运行模式），"
        "不会修改数据库、文件或任何演示数据。"
    )
    st.button(
        "重置演示",
        key="demo_reset",
        on_click=_reset_demo,
        use_container_width=True,
    )
