"""Presentation view-models for the v0.6.1 competition Agent workspace.

This module is the pure, Streamlit-free mapping layer behind the dedicated
demo page (``pages/0_知识Agent.py``). It turns frozen demo/public DTOs into
safe display models:

- display-state mapping for the full UI state matrix (grounded, warning,
  no-evidence, failed, rate-limited, backend-unavailable);
- source-type, integrity and badge labels in product language (never raw
  enum values, never binary moral claims);
- citation chips: mock ``citations_detail`` enrichment with an explicit
  ``#N`` mapping, real-API fallback to a plain verified-source list without
  guessing an answer-body mapping;
- null-tolerant title/label fallbacks and HTML escaping helpers;
- preset-chip mapping and mode/state session-key helpers.

Rendering side effects stay in the page; everything here is data mapping
that focused tests can cover without Streamlit.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.demo.contracts import DEMO_MODE
from src.demo.presets import PRESETS, DemoPreset
from src.hosted_api.contracts import SourceResponse
from src.models import ContextFingerprintState, ContextItemType
from src.source_metadata import InvalidSourceId, parse_source_id, safe_display_text

__all__ = [
    "AGENT_SESSION_KEYS",
    "AgentMode",
    "AnswerSegment",
    "AnswerViewModel",
    "AskRecord",
    "Badge",
    "CitationChip",
    "DisplayOutcome",
    "IntegrityBadge",
    "PRESENTATION_STEPS",
    "PresetChip",
    "SourceViewModel",
    "build_answer_view_model",
    "build_failure_view_model",
    "build_preset_chips",
    "build_source_unavailable",
    "build_source_view_model",
    "classify_error_code",
    "classify_outcome",
    "classify_transport_failure",
    "escape_text",
    "failure_headline",
    "is_mock_response",
    "is_stale_result",
    "next_request_sequence",
    "short_source_id",
    "source_type_label",
]

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class AgentMode(StrEnum):
    """The two competition-demo transports. Mode 1 is a loopback seam only."""

    MOCK_DEMO = "mock_demo"
    LOCAL_AGENT = "local_agent"

    @property
    def label(self) -> str:
        return {
            self.MOCK_DEMO: "预置离线演示（推荐）",
            self.LOCAL_AGENT: "本机 Agent 服务（需先启动）",
        }[self]

    @property
    def badge(self) -> str:
        return {
            self.MOCK_DEMO: "预置离线演示",
            self.LOCAL_AGENT: "本机 Agent",
        }[self]

    @property
    def caption(self) -> str:
        return {
            self.MOCK_DEMO: (
                "回答由预置演示数据生成，不是实时模型输出；无需联网，无需 API Key。"
            ),
            self.LOCAL_AGENT: (
                "连接本机知识服务回答问题；服务未启动时会明确提示，"
                "可随时切回预置离线演示。"
            ),
        }[self]


def is_mock_response(response: Any) -> bool:
    """True only for ``mode="mock_demo"`` responses (field absent in Mode 1)."""

    return getattr(response, "mode", None) == DEMO_MODE


# ---------------------------------------------------------------------------
# Display outcomes (UI state matrix)
# ---------------------------------------------------------------------------


class DisplayOutcome(StrEnum):
    """Closed set of presentation states; no numeric confidence anywhere."""

    ANSWER_GROUNDED = "answer_grounded"
    ANSWER_WITH_WARNING = "answer_with_warning"
    NO_EVIDENCE = "no_evidence"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True, slots=True)
class Badge:
    """One trust badge: icon + text + tone; states never rely on color alone."""

    icon: str
    text: str
    tone: str  # "ok" | "warn" | "empty" | "error" | "muted"


_GROUNDED_BADGE = Badge(icon="✓", text="有依据回答", tone="ok")
_LIMITED_BADGE = Badge(icon="⚠", text="来源存在限制", tone="warn")
_NO_EVIDENCE_BADGE = Badge(icon="○", text="未找到可支持资料", tone="empty")
_FAILED_BADGE = Badge(icon="×", text="本次请求未完成", tone="error")
_RATE_LIMIT_BADGE = Badge(icon="⏳", text="请求过于频繁", tone="warn")
_UNAVAILABLE_BADGE = Badge(icon="⏻", text="本机服务不可用", tone="error")

_OUTCOME_BADGES: dict[DisplayOutcome, tuple[Badge, ...]] = {
    DisplayOutcome.ANSWER_GROUNDED: (_GROUNDED_BADGE,),
    DisplayOutcome.ANSWER_WITH_WARNING: (_GROUNDED_BADGE, _LIMITED_BADGE),
    DisplayOutcome.NO_EVIDENCE: (_NO_EVIDENCE_BADGE,),
    DisplayOutcome.FAILED: (_FAILED_BADGE,),
    DisplayOutcome.RATE_LIMITED: (_RATE_LIMIT_BADGE,),
    DisplayOutcome.BACKEND_UNAVAILABLE: (_UNAVAILABLE_BADGE,),
}

_RATE_LIMIT_CODES = frozenset({"rate_limited", "concurrency_limited"})
_BACKEND_CODES = frozenset({"runtime_unavailable", "provider_unavailable"})


def classify_error_code(code: str | None) -> DisplayOutcome:
    """Map a closed-catalog error code to a presentation state."""

    if code in _RATE_LIMIT_CODES:
        return DisplayOutcome.RATE_LIMITED
    if code in _BACKEND_CODES:
        return DisplayOutcome.BACKEND_UNAVAILABLE
    return DisplayOutcome.FAILED


def classify_transport_failure(http_status: int) -> DisplayOutcome:
    """Map a transport-level HTTP status to a presentation state.

    ``0`` means a connection-level failure (refused/timeout), which is a
    backend-unavailable state, visually distinct from "no evidence".
    """

    if http_status == 429:
        return DisplayOutcome.RATE_LIMITED
    if http_status in (0, 503):
        return DisplayOutcome.BACKEND_UNAVAILABLE
    return DisplayOutcome.FAILED


def classify_outcome(response: Any) -> DisplayOutcome:
    """Map one agent response onto the closed presentation state set.

    ``200 + completed + grounded=false`` is the honest no-evidence state,
    never a system failure; ``200 + status=failed`` is a business failure.
    """

    status = getattr(response, "status", None)
    if status == "failed":
        error = getattr(response, "error", None)
        return classify_error_code(getattr(error, "code", None))
    if not getattr(response, "grounded", False):
        return DisplayOutcome.NO_EVIDENCE
    if getattr(response, "warnings", None):
        return DisplayOutcome.ANSWER_WITH_WARNING
    return DisplayOutcome.ANSWER_GROUNDED


def failure_headline(outcome: DisplayOutcome) -> str:
    return {
        DisplayOutcome.FAILED: "本次请求未完成",
        DisplayOutcome.RATE_LIMITED: "请求过于频繁，请稍后再试",
        DisplayOutcome.BACKEND_UNAVAILABLE: "本机 Agent 服务当前不可用",
    }.get(outcome, "本次请求未完成")


def failure_detail(outcome: DisplayOutcome, error_message: str | None = None) -> str:
    """Closed failure copy; the only error text shown is the public catalog."""

    if outcome is DisplayOutcome.RATE_LIMITED:
        return "短时间内请求过多。请稍等片刻后重试，或切换到预置离线演示继续演示。"
    if outcome is DisplayOutcome.BACKEND_UNAVAILABLE:
        return (
            "本机 Agent 服务未启动或暂时无法连接（127.0.0.1）。"
            "这不是知识库本身的问题。可稍后重试，或显式切换到预置离线演示。"
        )
    detail = error_message or "服务处理请求失败。"
    return f"{detail} 请重试，或切换到预置离线演示继续演示。"


# ---------------------------------------------------------------------------
# Source presentation
# ---------------------------------------------------------------------------

SOURCE_TYPE_LABELS: dict[ContextItemType, str] = {
    ContextItemType.PAGE: "页面资料",
    ContextItemType.KNOWLEDGE_OBJECT: "知识对象",
    ContextItemType.KNOWLEDGE_MEMORY: "知识记忆",
    ContextItemType.EVIDENCE: "证据",
}


def source_type_label(value: Any) -> str:
    """Product-language label for a source type (tolerates raw strings)."""

    try:
        return SOURCE_TYPE_LABELS.get(ContextItemType(value), str(value))
    except ValueError:
        return str(value)


@dataclass(frozen=True, slots=True)
class IntegrityBadge:
    """Demo integrity semantics; ``changed`` is a snapshot mismatch, not fraud."""

    label: str
    tone: str
    explanation: str


INTEGRITY_BADGES: dict[ContextFingerprintState, IntegrityBadge] = {
    ContextFingerprintState.VALID: IntegrityBadge(
        label="来源一致",
        tone="ok",
        explanation="来源内容与登记时的快照一致。",
    ),
    ContextFingerprintState.CHANGED: IntegrityBadge(
        label="来源发生变化",
        tone="warn",
        explanation=(
            "来源内容与登记时的快照不一致。这不代表造假或被篡改，"
            "只表示应回到原文重新核对后再采信。"
        ),
    ),
    ContextFingerprintState.MISSING: IntegrityBadge(
        label="来源不可用",
        tone="error",
        explanation="登记的来源当前无法读取，本条结论暂不应依赖它。",
    ),
    ContextFingerprintState.UNKNOWN: IntegrityBadge(
        label="暂无法确认",
        tone="muted",
        explanation="现有信息不足以确认来源状态，建议手动核对原文。",
    ),
    ContextFingerprintState.NOT_APPLICABLE: IntegrityBadge(
        label="不适用",
        tone="muted",
        explanation="该来源类型不适用完整性核对。",
    ),
}

SOURCE_UNAVAILABLE_NOTE = "该来源暂时不可检查（来源详情获取失败），不影响已给出的答案。"
LOCATION_FALLBACK = "位置信息未提供"
LIVE_INTEGRITY_NOTE = "当前模式未提供该来源的完整性状态，仅展示来源元数据。"
VERIFIED_SUPPORT_NOTE = "本条回答的已验证来源之一，支撑回答中的对应结论。"


def short_source_id(stable_id: str) -> str:
    """Short public fallback for a null title: type + local number, no UUID."""

    try:
        _, kind, local_id = parse_source_id(stable_id)
    except InvalidSourceId:
        return "来源详情暂不可用"
    try:
        type_text = SOURCE_TYPE_LABELS[ContextItemType(kind)]
    except ValueError:
        type_text = "来源"
    return f"{type_text} #{local_id}"


@dataclass(frozen=True, slots=True)
class CitationChip:
    """One verified-source chip; ``display_index`` is None in Mode 1."""

    stable_id: str
    display_index: int | None
    title: str
    location: str | None
    type_label: str
    anchor_label: str | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class SourceViewModel:
    """Safe public/demo fields only: no paths, IDs, SQL or provider payloads."""

    stable_id: str
    title: str
    type_label: str
    location: str
    citation_index: int | None
    support_note: str | None = None
    anchor_note: str | None = None
    integrity: IntegrityBadge | None = None
    integrity_note: str | None = None
    note: str | None = None
    unavailable: bool = False


def _display_title(source: SourceResponse | None) -> str | None:
    if source is None:
        return None
    return safe_display_text(getattr(source, "title", None))


def build_citation_chips(
    response: Any,
    source_metadata: dict[str, SourceResponse | None],
) -> tuple[CitationChip, ...]:
    """Build the verified-source chip list.

    Mode 2 (``citations_detail`` present): explicit ``#N`` mapping and anchor
    labels from the validated mock contract. Mode 1 (field absent): the plain
    ``citations`` list in array order, enriched by fetched source metadata,
    with ``display_index=None`` so the UI never implies a body ``#N`` mapping.
    """

    details = getattr(response, "citations_detail", None) or ()
    chips: list[CitationChip] = []
    if details:
        for detail in details:
            meta = source_metadata.get(detail.stable_id)
            title = _display_title(meta) or detail.title or short_source_id(detail.stable_id)
            label = getattr(meta, "label", None) or detail.label
            chips.append(
                CitationChip(
                    stable_id=detail.stable_id,
                    display_index=detail.display_index,
                    title=title,
                    location=safe_display_text(label),
                    type_label=source_type_label(detail.source_type),
                    anchor_label=detail.anchor_label,
                    available=meta is not None,
                )
            )
        return tuple(chips)
    for stable_id in getattr(response, "citations", ()) or ():
        meta = source_metadata.get(stable_id)
        chips.append(
            CitationChip(
                stable_id=stable_id,
                display_index=None,
                title=_display_title(meta) or short_source_id(stable_id),
                location=safe_display_text(getattr(meta, "label", None)),
                type_label=source_type_label(getattr(meta, "type", "来源")),
                available=meta is not None,
            )
        )
    return tuple(chips)


def build_source_view_model(
    chip: CitationChip,
    source: SourceResponse | None,
) -> SourceViewModel:
    """Project one selected source into viewer fields (metadata only)."""

    integrity: IntegrityBadge | None = None
    integrity_note: str | None = None
    note: str | None = None
    if source is None:
        return SourceViewModel(
            stable_id=chip.stable_id,
            title=chip.title,
            type_label=chip.type_label,
            location=chip.location or LOCATION_FALLBACK,
            citation_index=chip.display_index,
            unavailable=True,
            note=SOURCE_UNAVAILABLE_NOTE,
        )
    state = getattr(source, "integrity_state", None)
    if state is not None:
        integrity = INTEGRITY_BADGES.get(ContextFingerprintState(state))
        note = safe_display_text(getattr(source, "demo_note", None))
    else:
        integrity_note = LIVE_INTEGRITY_NOTE
    anchor = chip.anchor_label
    # An anchor that merely repeats "title · location" adds nothing the chip
    # row does not already show; only a distinct anchor explains the support.
    label_text = (safe_display_text(source.label) or "").strip()
    title_text = (source.title or "").strip()
    parts = [text for text in (title_text, label_text) if text]
    repeated = {text for text in (title_text, " · ".join(parts)) if text}
    if anchor and title_text and anchor.strip() in repeated:
        anchor = None
    return SourceViewModel(
        stable_id=chip.stable_id,
        title=_display_title(source) or chip.title,
        type_label=source_type_label(source.type),
        location=safe_display_text(source.label) or chip.location or LOCATION_FALLBACK,
        citation_index=chip.display_index,
        support_note=anchor or VERIFIED_SUPPORT_NOTE,
        anchor_note=chip.anchor_label,
        integrity=integrity,
        integrity_note=integrity_note,
        note=note,
    )


def build_source_unavailable(chip: CitationChip) -> SourceViewModel:
    """Separate per-source lookup failure from the answer state."""

    return SourceViewModel(
        stable_id=chip.stable_id,
        title=chip.title,
        type_label=chip.type_label,
        location=chip.location or LOCATION_FALLBACK,
        citation_index=chip.display_index,
        unavailable=True,
        note=SOURCE_UNAVAILABLE_NOTE,
    )


# ---------------------------------------------------------------------------
# Answer presentation
# ---------------------------------------------------------------------------

_CITATION_MARKER = re.compile(r"【来源\s*#([0-9]{1,3})】")


@dataclass(frozen=True, slots=True)
class AnswerSegment:
    """One inline piece of an answer paragraph: plain text or a #N chip."""

    kind: str  # "text" | "citation"
    text: str = ""
    index: int | None = None


def split_answer_paragraph(
    paragraph: str, *, render_markers: bool, citation_count: int
) -> tuple[AnswerSegment, ...]:
    """Split one paragraph into text/citation segments.

    ``render_markers`` is True only when the contract explicitly provides a
    ``#N`` mapping (Mode 2 ``citations_detail``). Mode 1 keeps the marker as
    literal text: no heuristic answer-text parsing is allowed there. Marker
    numbers outside the known citation set stay literal text as well.
    """

    if not render_markers:
        return (AnswerSegment(kind="text", text=paragraph),)
    segments: list[AnswerSegment] = []
    cursor = 0
    for match in _CITATION_MARKER.finditer(paragraph):
        number = int(match.group(1))
        if number < 1 or number > citation_count:
            continue
        if match.start() > cursor:
            segments.append(AnswerSegment(kind="text", text=paragraph[cursor : match.start()]))
        segments.append(AnswerSegment(kind="citation", index=number))
        cursor = match.end()
    if cursor < len(paragraph):
        segments.append(AnswerSegment(kind="text", text=paragraph[cursor :]))
    return tuple(segments) or (AnswerSegment(kind="text", text=paragraph),)


NO_EVIDENCE_HINT = (
    "EKB 不会在缺少依据时编造答案。可以换一条演示问题，"
    "或先在管理页面导入相关资料。"
)


@dataclass(frozen=True, slots=True)
class AnswerViewModel:
    """Everything the answer card renders, pre-mapped and display-safe."""

    question: str
    outcome: DisplayOutcome
    badges: tuple[Badge, ...]
    paragraphs: tuple[tuple[AnswerSegment, ...], ...] = ()
    chips: tuple[CitationChip, ...] = ()
    warnings: tuple[str, ...] = ()
    citation_count: int = 0
    is_mock: bool = False
    failure_headline: str | None = None
    failure_detail: str | None = None
    hint: str | None = None

    @property
    def answer_text(self) -> str:
        """Plain reassembled answer text (chips back to marker form)."""

        lines: list[str] = []
        for segments in self.paragraphs:
            pieces: list[str] = []
            for segment in segments:
                if segment.kind == "citation" and segment.index is not None:
                    pieces.append(f"【来源 #{segment.index}】")
                else:
                    pieces.append(segment.text)
            lines.append("".join(pieces))
        return "\n\n".join(lines)


def build_answer_view_model(
    question: str,
    response: Any,
    source_metadata: dict[str, SourceResponse | None],
) -> AnswerViewModel:
    """Map one completed/failed response to the answer-card view model."""

    outcome = classify_outcome(response)
    chips = build_citation_chips(response, source_metadata)
    mock = is_mock_response(response)
    paragraphs: tuple[tuple[AnswerSegment, ...], ...] = ()
    hint: str | None = None
    headline: str | None = None
    detail: str | None = None
    answer_text = getattr(response, "answer", "") or ""
    if outcome is DisplayOutcome.NO_EVIDENCE:
        hint = NO_EVIDENCE_HINT
    if outcome is DisplayOutcome.FAILED:
        error = getattr(response, "error", None)
        headline = failure_headline(outcome)
        detail = failure_detail(outcome, getattr(error, "message", None))
    elif answer_text:
        render_markers = mock and bool(getattr(response, "citations_detail", None))
        paragraphs = tuple(
            split_answer_paragraph(
                part, render_markers=render_markers, citation_count=len(chips)
            )
            for part in answer_text.split("\n\n")
        )
    return AnswerViewModel(
        question=question,
        outcome=outcome,
        badges=_OUTCOME_BADGES[outcome],
        paragraphs=paragraphs,
        chips=chips,
        warnings=tuple(getattr(response, "warnings", ()) or ()),
        citation_count=len(chips),
        is_mock=mock,
        failure_headline=headline,
        failure_detail=detail,
        hint=hint,
    )


@dataclass(frozen=True, slots=True)
class AskRecord:
    """One submitted ask kept in session state (stale-guarded by sequence)."""

    sequence: int
    mode: AgentMode
    question: str
    response: Any = None
    failure_code: str | None = None
    failure_http_status: int | None = None
    failure_message: str | None = None
    source_metadata: dict[str, SourceResponse | None] = field(default_factory=dict)
    source_unavailable: tuple[str, ...] = ()

    @property
    def transport_failed(self) -> bool:
        return self.response is None

    def failure_outcome(self) -> DisplayOutcome:
        """Presentation state for a failed record (transport or business)."""

        if self.transport_failed and self.failure_code is not None:
            return classify_error_code(self.failure_code)
        if self.transport_failed and self.failure_http_status is not None:
            return classify_transport_failure(self.failure_http_status)
        return classify_outcome(self.response)


def build_failure_view_model(record: AskRecord) -> AnswerViewModel:
    """Map a failed record to the same answer-card shape (safe copy only)."""

    outcome = record.failure_outcome()
    return AnswerViewModel(
        question=record.question,
        outcome=outcome,
        badges=_OUTCOME_BADGES[outcome],
        failure_headline=failure_headline(outcome),
        failure_detail=failure_detail(outcome, record.failure_message),
        is_mock=record.mode is AgentMode.MOCK_DEMO,
    )


# ---------------------------------------------------------------------------
# Preset chips
# ---------------------------------------------------------------------------

_PRESET_SHORT_LABELS: dict[str, str] = {
    "A": "参数影响",
    "B": "历史经验",
    "C": "来源可信度",
    "A2": "变频器注意事项",
    "EMPTY": "超出知识范围",
    "REHEARSAL_FAILED": "失败演练",
}

_PRESET_TAGS: dict[str, str] = {
    "main": "主问题",
    "backup": "备用",
    "rehearsal": "排练",
}


@dataclass(frozen=True, slots=True)
class PresetChip:
    """One preset question card: role, short label and full question."""

    preset_id: str
    role: str
    scenario: str
    question: str
    short_label: str
    tag: str
    main: bool


def build_preset_chips(
    presets: tuple[DemoPreset, ...] = PRESETS,
) -> tuple[PresetChip, ...]:
    chips: list[PresetChip] = []
    for preset in presets:
        chips.append(
            PresetChip(
                preset_id=preset.preset_id,
                role=preset.role,
                scenario=preset.scenario,
                question=preset.question,
                short_label=_PRESET_SHORT_LABELS.get(preset.preset_id, preset.preset_id),
                tag=_PRESET_TAGS.get(preset.role, preset.role),
                main=preset.role == "main",
            )
        )
    return tuple(chips)


# ---------------------------------------------------------------------------
# Presentation states and session helpers
# ---------------------------------------------------------------------------

# Coarse, frontend-only presentation labels shown while one ask runs. They are
# NOT chain-of-thought, tool traces or backend-reported events; the backend
# exposes none of those, and none is claimed here.
PRESENTATION_STEPS: tuple[str, ...] = (
    "正在提交问题",
    "正在等待知识服务",
    "正在整理有依据的回答",
    "正在加载已验证来源",
)

# Seconds of purely visual pacing between presentation labels so the demo
# reads naturally; mock responses themselves are instantaneous.
PRESENTATION_PACE_SECONDS = 0.35

AGENT_SESSION_KEYS: tuple[str, ...] = (
    "agent_mode",
    "agent_request_seq",
    "agent_pending_question",
    "agent_submitted_question",
    "agent_result",
    "agent_selected_source_id",
    "agent_base_url",
)

# Keys cleared when the operator switches mode or asks a new question, so a
# stale result or selected source can never leak into the new state.
STATE_KEYS_CLEARED_ON_ASK: tuple[str, ...] = (
    "agent_selected_source_id",
)
STATE_KEYS_CLEARED_ON_MODE_SWITCH: tuple[str, ...] = (
    "agent_pending_question",
    "agent_submitted_question",
    "agent_result",
    "agent_selected_source_id",
)


def next_request_sequence(current: int) -> int:
    """Monotonic request generation, guarding against stale results."""

    return current + 1


def is_stale_result(result_sequence: int, request_sequence: int) -> bool:
    return result_sequence != request_sequence


def escape_text(value: str) -> str:
    """HTML-escape any text before it enters an unsafe_allow_html block."""

    return html.escape(value, quote=True)
