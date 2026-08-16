"""Phase 11B-R1 lifecycle tests: the search page must emit exactly one hybrid
query embedding per explicit search action, and zero for any other UI event.

These tests use a counting fake provider at the ``application_hybrid_search_service``
boundary plus pure Streamlit AppTest reruns to pin the real rerun lifecycle:
mode switch, query typing, pagination, preview, URL/deep-link restore, and
non-search actions must all be cost-free.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.models import PageStatus, SearchResult


class CountingHybridService:
    """Counts every hybrid search; returns a fixed single-result outcome."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 20):
        self.calls.append(query)
        result = SearchResult(
            page_id=1,
            document_id=1,
            document_title="STM32入门",
            filename="stm32.pdf",
            page_number=1,
            image_path=Path("page_0001.png"),
            content="定时器预分频器和自动重装载寄存器的作用",
            snippet="",
            rank=0.0,
            status=PageStatus.REVIEWED,
            match_type="语义召回",
        )
        return type(
            "Outcome",
            (),
            {
                "results": (
                    type(
                        "Hit",
                        (),
                        {"result": result, "lexical_rank": 1, "vector_rank": 1},
                    )(),
                ),
                "vector_status": "ok",
                "invalid_vector_candidates": 0,
            },
        )()


class Basket:
    def default_basket(self):
        return type("B", (), {"id": 1})()

    def list_items(self, basket_id=None):
        return []


def _setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "d" / "database" / "knowledge.db")
    doc = db.create_document(
        title="STM32入门", filename="stm32.pdf",
        source_path=tmp_path / "d" / "stm32.pdf", sha256="a" * 64,
    )
    db.create_page(
        document_id=doc.id, page_number=1,
        image_path=tmp_path / "d" / "page_0001.png",
        extracted_text="定时器预分频器和自动重装载寄存器的作用",
    )
    hybrid = CountingHybridService()
    monkeypatch.setattr(runtime, "application_database", lambda: db)
    monkeypatch.setattr(runtime, "application_hybrid_search_service", lambda: hybrid)
    monkeypatch.setattr(runtime, "application_evidence_basket_service", lambda: Basket())
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=db, raw_dir=tmp_path / "d" / "r", pages_dir=tmp_path / "d" / "p",
            markdown_dir=tmp_path / "d" / "m", data_dir=tmp_path / "d",
        ),
    )
    page = next((Path(__file__).parents[1] / "pages").glob("4_*.py"))
    return hybrid, page


QUERY = "定时器预分频器和自动重装载寄存器的作用"


# --- §6 Case 1: initial load is free ---
def test_initial_load_free(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    AppTest.from_file(str(page)).run(timeout=15)
    assert hybrid.calls == []


# --- §6 Case 2: keyword query typing is free ---
def test_keyword_query_typing_free(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.text_input(key="search_query_input").set_value("DAC")
    app.run(timeout=15)
    assert hybrid.calls == []


# --- §6 Case 3: mode switch only is free (hard acceptance) ---
def test_mode_switch_only_free(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.radio(key="search_mode_value").set_value("hybrid")
    app.run(timeout=15)
    app.run(timeout=15)
    assert hybrid.calls == []


# --- §6 Case 4: hybrid query typing only is free ---
def test_hybrid_query_typing_free(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.radio(key="search_mode_value").set_value("hybrid")
    app.text_input(key="search_query_input").set_value(QUERY)
    app.run(timeout=15)
    assert hybrid.calls == []


# --- §6 Case 5: one explicit click = exactly one embedding ---
def test_one_explicit_click_embeds_once(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.text_input(key="search_query_input").set_value(QUERY)
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=15)
    assert hybrid.calls == [QUERY]


# --- §6 Case 6: post-submit reruns do not re-embed ---
def test_post_submit_rerun_still_one(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.text_input(key="search_query_input").set_value(QUERY)
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=15)
    app.run(timeout=15)
    app.run(timeout=15)
    assert hybrid.calls == [QUERY]


# --- §6 Case 9 / non-search interactions are free ---
def test_view_mode_change_free(tmp_path, monkeypatch) -> None:
    hybrid, page = _setup(tmp_path, monkeypatch)
    app = AppTest.from_file(str(page)).run(timeout=15)
    app.text_input(key="search_query_input").set_value(QUERY)
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=15)
    app.radio(key="search_view_mode").set_value("document")
    app.run(timeout=15)
    assert hybrid.calls == [QUERY]
