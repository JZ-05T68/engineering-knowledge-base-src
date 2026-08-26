"""Model-backed structured decision integration (v0.6.0 Phase 2B).

This package adapts the Phase 2A ``DecisionProvider`` boundary to the existing
vendor-neutral AI Provider in ``src.ai.provider``. It contains only:

- a deterministic decision prompt builder over the frozen Phase 1 Tool
  Registry;
- a strict, fail-closed structured JSON parser for model output;
- :class:`ModelDecisionProvider`, the DecisionProvider implementation that
  calls the existing audited/budgeted provider exactly once per decision.

It deliberately does not create a second AI Provider abstraction, does not
retry, does not execute Tools, and does not implement the Final Answer Stage.
"""

from src.agent.decision.parser import MAX_DECISION_OUTPUT_CHARS, DecisionParseError, parse_decision
from src.agent.decision.prompt import build_decision_prompt, build_tool_catalog
from src.agent.decision.provider import (
    DEFAULT_DECISION_MAX_OUTPUT_TOKENS,
    DEFAULT_DECISION_SOURCE_FEATURE,
    ModelDecisionProvider,
)

__all__ = [
    "DEFAULT_DECISION_MAX_OUTPUT_TOKENS",
    "DEFAULT_DECISION_SOURCE_FEATURE",
    "DecisionParseError",
    "MAX_DECISION_OUTPUT_CHARS",
    "ModelDecisionProvider",
    "build_decision_prompt",
    "build_tool_catalog",
    "parse_decision",
]
