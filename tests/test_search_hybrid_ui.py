"""Phase 11B UI cost-gate tests for the hybrid search mode.

Fully offline: the page's ``application_hybrid_search_service`` is stubbed with
a recording fake, so these tests prove the "one explicit hybrid search action →
at most one query embedding" invariant, filter/sort fallback, and default
keyword behaviour without any real Qwen call.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService


class _RecordingHybridService:
    """Records every hybrid invocation; returns a fixed empty outcome."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int = 20):
        self.calls.append((query, limit))
        # Minimal empty outcome; the page only needs ``.results`` and status.
        return type(
            "Outcome",
            (),
            {
                "results": (),
                "vector_status": "ok",
                "invalid_vector_candidates": 0,
            },
        )()


def _library(tmp_path: Path) -> Database:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="STM32入门",
        filename="stm32.pdf",
        source_path=tmp_path / "raw" / "stm32.pdf",
        sha256="a" * 64,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "1" / "page_0001.png",
        extracted_text="定时器预分频器和自动重装载寄存器的作用",
    )
    return database


def _ui_runtime(tmp_path: Path, monkeypatch, hybrid) -> Database:
    database = _library(tmp_path)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_hybrid_search_service", lambda: hybrid)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: _FakeBasketService(),
    )
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=database,
            raw_dir=tmp_path / "raw",
            pages_dir=tmp_path / "pages",
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )
    return database


class _FakeBasketService:
    def default_basket(self):
        return type("Basket", (), {"id": 1})()

    def list_items(self, basket_id=None):
        return []


def _search_page(tmp_path: Path):
    from pathlib import Path as _P

    search_path = next((_P(__file__).parents[1] / "pages").glob("4_*.py"))
    return search_path


def test_keyword_mode_never_calls_hybrid(tmp_path, monkeypatch) -> None:
    hybrid = _RecordingHybridService()
    _ui_runtime(tmp_path, monkeypatch, hybrid)
    app = AppTest.from_file(str(_search_page(tmp_path))).run(timeout=10)

    assert not app.exception
    # Default mode is keyword; submit a search without touching the mode radio.
    app.text_input(key="search_query_input").set_value("定时器")
    app.button(key="apply_search_filters").click().run(timeout=10)

    assert hybrid.calls == []


def test_hybrid_mode_with_clean_state_embeds_exactly_once(
    tmp_path, monkeypatch
) -> None:
    hybrid = _RecordingHybridService()
    _ui_runtime(tmp_path, monkeypatch, hybrid)
    app = AppTest.from_file(str(_search_page(tmp_path))).run(timeout=10)

    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=10)

    assert len(hybrid.calls) == 1


def test_hybrid_mode_with_filter_falls_back_to_keyword(
    tmp_path, monkeypatch
) -> None:
    hybrid = _RecordingHybridService()
    _ui_runtime(tmp_path, monkeypatch, hybrid)
    app = AppTest.from_file(str(_search_page(tmp_path))).run(timeout=10)

    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    # Activate a filter (仅看有笔记). The checkbox renders only after the
    # filter panel is expanded.
    app.toggle(key="search_filters_open").set_value(True).run(timeout=10)
    app.checkbox(key="search_has_note").set_value(True)
    app.button(key="apply_search_filters").click().run(timeout=10)

    assert hybrid.calls == []
