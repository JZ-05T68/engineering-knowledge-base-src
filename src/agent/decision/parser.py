"""Strict structured decision JSON parser (v0.6.0 Phase 2B).

Model output is treated as untrusted external input. This parser accepts only
one standard JSON top-level object with exactly the frozen fields
``kind`` / ``tool_name`` / ``arguments`` and no repair of any kind:

- no Markdown code fence stripping;
- no prose/JSON substring extraction;
- no duplicate-key overwrite;
- no NaN / Infinity;
- no extra fields (including ``reasoning`` / ``tool_calls``);
- no multiple tool requests.

The parser validates structure only. Tool existence and READ_ONLY policy stay
authoritative in the Phase 2A executor / Phase 1 Registry.
"""

from __future__ import annotations

import json

from src.agent.execution.contracts import AgentDecision, AgentDecisionKind

MAX_DECISION_OUTPUT_CHARS = 4096

_ALLOWED_FIELDS = frozenset({"kind", "tool_name", "arguments"})
_DECISION_KINDS = {
    "CALL_TOOL": AgentDecisionKind.CALL_TOOL,
    "call_tool": AgentDecisionKind.CALL_TOOL,
    "ANSWER_DIRECTLY": AgentDecisionKind.ANSWER_DIRECTLY,
    "answer_directly": AgentDecisionKind.ANSWER_DIRECTLY,
}

__all__ = ["DecisionParseError", "MAX_DECISION_OUTPUT_CHARS", "parse_decision"]


class DecisionParseError(ValueError):
    """Strict structured decision parsing failed.

    The message is intentionally generic and never contains the raw model
    output, API keys, paths or stack traces.
    """


def _reject_constant(value: str) -> None:
    raise ValueError(f"非标准 JSON 数值：{value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 键：{key}")
        result[key] = value
    return result


def parse_decision(raw: str) -> AgentDecision:
    """Parse exactly one strict structured decision object, or fail closed."""
    if not isinstance(raw, str):
        raise DecisionParseError("决策输出必须是文本。")
    stripped = raw.strip()
    if not stripped:
        raise DecisionParseError("决策输出为空。")
    if len(stripped) > MAX_DECISION_OUTPUT_CHARS:
        raise DecisionParseError(
            f"决策输出超过大小限制（{MAX_DECISION_OUTPUT_CHARS} 字符）。"
        )
    try:
        payload = json.loads(
            stripped,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        raise DecisionParseError("决策输出不是严格的标准 JSON 对象。") from exc

    if not isinstance(payload, dict):
        raise DecisionParseError("决策输出必须是单个顶层 JSON 对象。")

    extra_fields = sorted(set(payload) - _ALLOWED_FIELDS)
    if extra_fields:
        raise DecisionParseError(
            f"决策输出包含不允许的字段：{', '.join(extra_fields)}。"
        )
    missing_fields = sorted(_ALLOWED_FIELDS - set(payload))
    if missing_fields:
        raise DecisionParseError(
            f"决策输出缺少字段：{', '.join(missing_fields)}。"
        )

    kind_value = payload["kind"]
    if not isinstance(kind_value, str) or kind_value not in _DECISION_KINDS:
        raise DecisionParseError("决策输出包含未知的 kind。")
    kind = _DECISION_KINDS[kind_value]

    tool_name = payload["tool_name"]
    arguments = payload["arguments"]
    if kind is AgentDecisionKind.CALL_TOOL:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise DecisionParseError("CALL_TOOL 决策必须提供非空 tool_name。")
        if not isinstance(arguments, dict):
            raise DecisionParseError("CALL_TOOL 决策的 arguments 必须是对象。")
    else:
        if tool_name is not None:
            raise DecisionParseError("ANSWER_DIRECTLY 决策不允许携带 tool_name。")
        if not isinstance(arguments, dict) or arguments:
            raise DecisionParseError("ANSWER_DIRECTLY 决策的 arguments 必须是空对象。")

    try:
        return AgentDecision(kind=kind, tool_name=tool_name, arguments=arguments)
    except (TypeError, ValueError) as exc:
        raise DecisionParseError("决策对象不符合 AgentDecision 契约。") from exc
