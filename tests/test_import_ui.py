"""Focused tests for the optional post-import review action."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st

import src.runtime as runtime
from src.document_service import ImportResult, first_reviewable_import_page
from src.models import PageStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMPORT_PAGE = PROJECT_ROOT / "pages" / "1_导入资料.py"


class _Progress:
    def progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    def empty(self) -> None:
        return None


def _result(*statuses: PageStatus, duplicate: bool = False) -> ImportResult:
    pages = tuple(
        SimpleNamespace(
            id=index,
            status=status,
            processing_status="failed" if status is PageStatus.FAILED else "text_extracted",
        )
        for index, status in enumerate(statuses, start=1)
    )
    return SimpleNamespace(
        document=SimpleNamespace(id=7, title="隔离导入资料"),
        pages=pages,
        duplicate=duplicate,
        import_record=None,
    )


def _run_import_page(
    monkeypatch: pytest.MonkeyPatch, result: ImportResult
) -> tuple[
    list[tuple[str, dict[str, Any]]],
    dict[str, str],
    list[tuple[str, dict[str, Any]]],
    list[str],
    Any,
    list[int],
]:
    class Upload:
        name = "隔离资料.pdf"
        size = 1024

        @staticmethod
        def getvalue() -> bytes:
            return b"%PDF-isolated"

    class Service:
        @staticmethod
        def import_pdf(**kwargs: Any) -> ImportResult:
            callback = kwargs["progress_callback"]
            callback(1, max(len(result.pages), 1))
            return result

    buttons: list[tuple[str, dict[str, Any]]] = []
    query_params: dict[str, str] = {}
    switched: list[tuple[str, dict[str, Any]]] = []
    captions: list[str] = []
    remaining_import_clicks = [1]
    import_calls: list[int] = []

    def button(label: str, **kwargs: Any) -> bool:
        buttons.append((label, kwargs))
        if label == "导入 PDF" and remaining_import_clicks:
            remaining_import_clicks.pop()
            return True
        return False

    original_import = Service.import_pdf

    def import_pdf(**kwargs: Any) -> ImportResult:
        import_calls.append(1)
        return original_import(**kwargs)

    Service.import_pdf = staticmethod(import_pdf)

    monkeypatch.setattr(runtime, "application_document_service", lambda: Service())
    monkeypatch.setattr(st, "set_page_config", lambda **kwargs: None)
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "caption", lambda value, **kwargs: captions.append(str(value)))
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: Upload())
    monkeypatch.setattr(st, "text_input", lambda *args, **kwargs: "隔离导入资料")
    monkeypatch.setattr(st, "button", button)
    monkeypatch.setattr(st, "progress", lambda *args, **kwargs: _Progress())
    monkeypatch.setattr(st, "success", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "divider", lambda: None)
    monkeypatch.setattr(st, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "query_params", query_params)
    monkeypatch.setattr(
        st,
        "switch_page",
        lambda page, **kwargs: switched.append((str(page), kwargs)),
    )

    def run_page() -> None:
        runpy.run_path(str(IMPORT_PAGE), run_name="__main__")

    run_page()
    return buttons, query_params, switched, captions, run_page, import_calls


def _click_review(buttons: list[tuple[str, dict[str, Any]]]) -> None:
    review = next(kwargs for label, kwargs in buttons if label == "进入复核")
    callback = review["on_click"]
    callback(*review["args"])


def test_normal_import_offers_review_action_for_first_reviewable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(PageStatus.PENDING, PageStatus.PENDING)

    buttons, query_params, switched, _, rerun, import_calls = _run_import_page(
        monkeypatch, result
    )
    _click_review(buttons)
    assert query_params == {"import_review_page": "1"}
    assert switched == []
    rerun()

    assert first_reviewable_import_page(result).id == 1
    assert query_params == {}
    assert switched == [
        ("pages/5_待整理页面.py", {"query_params": {"page_id": "1"}})
    ]
    assert len(import_calls) == 1


def test_partially_failed_import_still_offers_review_for_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(PageStatus.REVIEWED, PageStatus.FAILED)

    buttons, query_params, switched, _, rerun, import_calls = _run_import_page(
        monkeypatch, result
    )
    _click_review(buttons)
    rerun()

    assert first_reviewable_import_page(result).id == 2
    assert query_params == {}
    assert switched == [
        ("pages/5_待整理页面.py", {"query_params": {"page_id": "2"}})
    ]
    assert len(import_calls) == 1


def test_complete_duplicate_import_never_offers_review_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(PageStatus.PENDING, duplicate=True)

    buttons, query_params, switched, _, _, import_calls = _run_import_page(
        monkeypatch, result
    )

    assert first_reviewable_import_page(result) is None
    assert "进入复核" not in [label for label, _ in buttons]
    assert query_params == {}
    assert switched == []
    assert len(import_calls) == 1


def test_import_without_reviewable_pages_explains_that_no_action_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(PageStatus.REVIEWED, PageStatus.SKIPPED)

    buttons, query_params, switched, captions, _, import_calls = _run_import_page(
        monkeypatch, result
    )

    assert first_reviewable_import_page(result) is None
    assert "进入复核" not in [label for label, _ in buttons]
    assert any("没有新增待复核页面" in caption for caption in captions)
    assert query_params == {}
    assert switched == []
    assert len(import_calls) == 1
