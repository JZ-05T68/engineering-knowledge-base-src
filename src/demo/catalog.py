"""Demo catalog assembly and fail-closed validation (v0.6.1).

``load_demo_catalog`` is the single entry point that turns the static fixture
definitions into a validated :class:`DemoCatalog`. Every invariant the mock
client and the frontend handoff rely on is checked here once, so violations
surface as loud construction errors instead of silent demo lies:

- source identities parse and match their declared type; display texts are
  path-free and survive ``safe_display_text`` unchanged;
- every citation resolves to a cataloged source and ``citations_detail``
  mirrors ``citations`` one-to-one with sequential display indexes;
- grounded answers carry at least one ``#N`` marker whose first appearance
  order matches ``citations``; non-grounded and failed answers carry none;
- warnings stay inside the closed real-HTTP projection (generic message);
- presets reference existing fixtures with matching expectations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.demo.contracts import DemoAgentRunResponse
from src.demo.fixtures import (
    GENERIC_WARNING,
    NO_EVIDENCE_MESSAGE,
    RESPONSES,
    SOURCES,
    DemoResponseFixture,
    DemoSourceFixture,
)
from src.demo.presets import PRESETS, DemoPreset
from src.source_metadata import InvalidSourceId, parse_source_id, safe_display_text

_CITATION_MARKER = re.compile(r"#([0-9]{1,3})")

_SCENARIO_FIXTURES = {
    "A_grounded": ("success_grounded_a", "success_grounded_a2"),
    "B_historical": ("partial_warning_b",),
    "C_integrity": ("integrity_warning_c",),
    "empty": ("empty_result",),
    "failed_safe": ("failed_safe",),
}


@dataclass(frozen=True, slots=True)
class DemoCatalog:
    """Validated, immutable demo dataset served by ``MockDemoClient``."""

    sources: tuple[DemoSourceFixture, ...]
    responses: tuple[DemoResponseFixture, ...]
    presets: tuple[DemoPreset, ...]

    def source(self, stable_id: str) -> DemoSourceFixture | None:
        """Return the source fixture for ``stable_id``, or ``None``."""
        for source in self.sources:
            if source.stable_id == stable_id:
                return source
        return None

    def response(self, key: str) -> DemoResponseFixture:
        """Return the response fixture ``key``; unknown keys fail closed."""
        for fixture in self.responses:
            if fixture.key == key:
                return fixture
        raise KeyError(f"未知的演示响应 fixture：{key}")

    def preset_for_question(self, normalized_text: str) -> DemoPreset | None:
        """Return the preset whose normalized question matches exactly."""
        for preset in self.presets:
            if _normalize(preset.question) == normalized_text:
                return preset
        return None


def load_demo_catalog() -> DemoCatalog:
    """Build and validate the deterministic demo catalog."""
    _validate_sources(SOURCES)
    _validate_responses(RESPONSES, SOURCES)
    _validate_presets(PRESETS, RESPONSES)
    return DemoCatalog(sources=SOURCES, responses=RESPONSES, presets=PRESETS)


def _normalize(text: str) -> str:
    return text.strip()


def _validate_sources(sources: tuple[DemoSourceFixture, ...]) -> None:
    seen: set[str] = set()
    for source in sources:
        if source.stable_id in seen:
            raise ValueError(f"演示来源重复：{source.stable_id}")
        seen.add(source.stable_id)
        try:
            _, kind, _ = parse_source_id(source.stable_id)
        except InvalidSourceId:
            raise ValueError(f"演示来源 stable_id 非法：{source.stable_id}") from None
        if kind != source.source_type.value:
            raise ValueError(f"演示来源类型与 stable_id 不一致：{source.stable_id}")
        if not source.anchor_label.strip():
            raise ValueError(f"演示来源缺少 anchor_label：{source.stable_id}")
        for text in (source.title, source.label):
            if text is not None and safe_display_text(text) != text:
                raise ValueError(f"演示来源展示文本不安全：{source.stable_id}")
        if source.demo_note is not None and source.integrity_state is None:
            raise ValueError(f"演示来源 demo_note 必须伴随 integrity_state：{source.stable_id}")


def _validate_responses(
    responses: tuple[DemoResponseFixture, ...],
    sources: tuple[DemoSourceFixture, ...],
) -> None:
    source_ids = {source.stable_id for source in sources}
    keys: set[str] = set()
    for fixture in responses:
        if fixture.key in keys:
            raise ValueError(f"演示响应 fixture 重复：{fixture.key}")
        keys.add(fixture.key)
        response = fixture.response
        _validate_response(fixture.key, response, source_ids)


def _validate_response(
    key: str,
    response: DemoAgentRunResponse,
    source_ids: set[str],
) -> None:
    failed = response.status == "failed"
    if failed != (response.error is not None):
        raise ValueError(f"演示响应 {key}：failed 状态必须与 error 同时出现")
    if len(response.citations_detail) != len(response.citations):
        raise ValueError(f"演示响应 {key}：citations_detail 与 citations 数量不一致")
    for index, (citation, detail) in enumerate(
        zip(response.citations, response.citations_detail, strict=True), start=1
    ):
        if detail.stable_id != citation or detail.display_index != index:
            raise ValueError(f"演示响应 {key}：citation 顺序或编号不一致")
        if detail.stable_id not in source_ids:
            raise ValueError(f"演示响应 {key}：引用了未知来源 {citation}")
    if response.status == "completed" and not response.grounded:
        if response.citations or response.citations_detail:
            raise ValueError(f"演示响应 {key}：无证据回答不得携带引用")
        if response.answer != NO_EVIDENCE_MESSAGE:
            raise ValueError(f"演示响应 {key}：无证据文案必须与真实链路一致")
    if response.status == "completed" and response.grounded:
        if not response.citations:
            raise ValueError(f"演示响应 {key}：grounded 回答必须携带引用")
        _validate_markers(key, response)
    if response.status == "failed" and (response.answer or response.citations):
        raise ValueError(f"演示响应 {key}：失败回答不得携带答案或引用")
    if response.warnings not in ((), (GENERIC_WARNING,)):
        raise ValueError(f"演示响应 {key}：warnings 必须为空或仅含真实投影的通用文案")


def _validate_markers(key: str, response: DemoAgentRunResponse) -> None:
    first_seen: list[int] = []
    for match in _CITATION_MARKER.finditer(response.answer):
        number = int(match.group(1))
        if number < 1 or number > len(response.citations):
            raise ValueError(f"演示响应 {key}：答案引用了不存在的来源编号 #{number}")
        if number not in first_seen:
            first_seen.append(number)
    if first_seen != list(range(1, len(response.citations) + 1)):
        raise ValueError(
            f"演示响应 {key}：正文引用编号的首现顺序必须与 citations 一一对应"
        )


def _validate_presets(
    presets: tuple[DemoPreset, ...],
    responses: tuple[DemoResponseFixture, ...],
) -> None:
    by_key = {fixture.key: fixture for fixture in responses}
    questions: set[str] = set()
    preset_ids: set[str] = set()
    role_counts = {"main": 0, "backup": 0, "rehearsal": 0}
    for preset in presets:
        if preset.preset_id in preset_ids:
            raise ValueError(f"演示题卡 preset_id 重复：{preset.preset_id}")
        preset_ids.add(preset.preset_id)
        normalized = _normalize(preset.question)
        if not normalized or normalized in questions:
            raise ValueError(f"演示题卡问题为空或重复：{preset.preset_id}")
        questions.add(normalized)
        role_counts[preset.role] += 1
        fixture = by_key.get(preset.expected_fixture)
        if fixture is None:
            raise ValueError(f"演示题卡引用了未知 fixture：{preset.expected_fixture}")
        if preset.scenario not in _SCENARIO_FIXTURES:
            raise ValueError(f"演示题卡场景非法：{preset.scenario}")
        if preset.expected_fixture not in _SCENARIO_FIXTURES[preset.scenario]:
            raise ValueError(f"演示题卡场景与 fixture 不一致：{preset.preset_id}")
        if preset.expected_cited_sources != tuple(fixture.response.citations):
            raise ValueError(f"演示题卡期望来源与 fixture 不一致：{preset.preset_id}")
        expected_warning = "generic_limitation" if fixture.response.warnings else "none"
        if preset.expected_warning_state != expected_warning:
            raise ValueError(f"演示题卡期望 warning 状态与 fixture 不一致：{preset.preset_id}")
    if role_counts["main"] != 3 or role_counts["backup"] != 2:
        raise ValueError(
            f"演示题卡必须为 3 主 + 2 备，当前：{role_counts['main']}/{role_counts['backup']}"
        )
    if role_counts["rehearsal"] < 1:
        raise ValueError("演示题卡必须包含至少 1 条排练预设")
