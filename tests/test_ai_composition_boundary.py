"""Structural regression tests for the production AI composition boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.experience_ui as experience_ui
import src.rag_answer_ui as rag_answer_ui
import src.runtime as runtime
from src.ai.provider import AIProductionCompositionError

REPOSITORY_ROOT = Path(__file__).parents[1]
APPROVED_QWEN_CONSTRUCTION_SITES = {
    Path("src/hosted/ai_runtime.py"),
    Path("src/runtime.py"),
}


def _qwen_constructor_sites() -> set[Path]:
    sites: set[Path] = set()
    for path in (REPOSITORY_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "QwenProvider"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "QwenProvider"
            )
            for node in ast.walk(tree)
        ):
            sites.add(path.relative_to(REPOSITORY_ROOT))
    return sites


def test_production_qwen_construction_is_restricted_to_approved_roots() -> None:
    assert _qwen_constructor_sites() == APPROVED_QWEN_CONSTRUCTION_SITES


@pytest.mark.parametrize(
    "resolver",
    [rag_answer_ui._resolve_provider, experience_ui._resolve_provider],
)
def test_local_rag_and_experience_composition_reject_raw_provider(
    monkeypatch: pytest.MonkeyPatch, resolver
) -> None:
    raw = Mock()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: raw)

    with pytest.raises(AIProductionCompositionError):
        resolver()

    raw.complete.assert_not_called()


@pytest.mark.parametrize(
    "resolver",
    [rag_answer_ui._resolve_provider, experience_ui._resolve_provider],
)
def test_local_ui_does_not_hide_production_composition_failure(
    monkeypatch: pytest.MonkeyPatch, resolver
) -> None:
    def invalid_factory():
        raise AIProductionCompositionError("test-only-invalid-composition")

    monkeypatch.setattr(runtime, "application_ai_provider", invalid_factory)

    with pytest.raises(AIProductionCompositionError):
        resolver()
