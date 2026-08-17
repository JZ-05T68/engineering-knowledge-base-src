"""Phase 11B UI cost-gate tests for the hybrid search mode.

Fully offline: the page's ``application_hybrid_search_service`` is stubbed with
a recording fake, so these tests prove the "one explicit hybrid search action →
at most one query embedding" invariant, filter/sort fallback, and default
keyword behaviour without any real Qwen call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.ai.hybrid_search import VectorPathStatus
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.models import PageStatus, SearchResult


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


class _ConfigurableHybridService:
    """Returns a one-hit outcome with a configurable vector path status."""

    def __init__(self, vector_status: VectorPathStatus = VectorPathStatus.OK) -> None:
        self.calls: list[str] = []
        self.vector_status = vector_status

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
            status=PageStatus.PENDING,
        )
        vector_rank = (
            1
            if self.vector_status in (VectorPathStatus.OK, VectorPathStatus.EMPTY)
            else None
        )
        hit = type(
            "Hit",
            (),
            {"result": result, "lexical_rank": 1, "vector_rank": vector_rank},
        )()
        return type(
            "Outcome",
            (),
            {
                "results": (hit,),
                "vector_status": self.vector_status,
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


# --- D-05: UX honesty — per-event fallback notice and outcome-driven heading ---

FALLBACK_NOTICE = "当前筛选或排序条件不支持 AI 混合检索，本次搜索已使用关键词检索。"
KEYWORD_HEADING = "搜索结果（共 1 个页面）"
HYBRID_HEADING = "混合检索结果（已载入 1 条）"


def _subheader_values(app: AppTest) -> list[str]:
    return [item.value for item in app.subheader]


def _caption_values(app: AppTest) -> list[str]:
    return [item.value for item in app.caption]


def _start_app(tmp_path: Path, monkeypatch, hybrid) -> AppTest:
    _ui_runtime(tmp_path, monkeypatch, hybrid)
    app = AppTest.from_file(str(_search_page(tmp_path))).run(timeout=15)
    assert not app.exception
    return app


def test_gate_fallback_with_filter_shows_notice_and_keyword_heading(
    tmp_path, monkeypatch
) -> None:
    hybrid = _ConfigurableHybridService()
    app = _start_app(tmp_path, monkeypatch, hybrid)

    # Expand the filter panel first; activating it re-syncs the query widget.
    app.toggle(key="search_filters_open").set_value(True).run(timeout=15)
    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    # Filter on the page's own status so the keyword fallback still has a hit.
    app.multiselect(key="search_status_values").select(PageStatus.PENDING.value)
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert hybrid.calls == []
    assert FALLBACK_NOTICE in _caption_values(app)
    assert KEYWORD_HEADING in _subheader_values(app)
    assert not any("混合检索结果" in value for value in _subheader_values(app))


def test_gate_fallback_with_sort_shows_notice_and_keyword_heading(
    tmp_path, monkeypatch
) -> None:
    hybrid = _ConfigurableHybridService()
    app = _start_app(tmp_path, monkeypatch, hybrid)

    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    app.selectbox(key="search_sort_value").set_value("document_page")
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert hybrid.calls == []
    assert FALLBACK_NOTICE in _caption_values(app)
    assert KEYWORD_HEADING in _subheader_values(app)
    assert not any("混合检索结果" in value for value in _subheader_values(app))


def test_hybrid_vector_ok_shows_hybrid_heading(tmp_path, monkeypatch) -> None:
    hybrid = _ConfigurableHybridService(vector_status=VectorPathStatus.OK)
    app = _start_app(tmp_path, monkeypatch, hybrid)

    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert hybrid.calls == ["定时器"]
    assert HYBRID_HEADING in _subheader_values(app)
    assert FALLBACK_NOTICE not in _caption_values(app)


@pytest.mark.parametrize(
    ("vector_status", "expected_note"),
    [
        (VectorPathStatus.DISABLED, "AI 混合检索未启用，当前使用关键词检索"),
        (VectorPathStatus.FAILED, "语义检索暂时失败，已保留关键词结果"),
    ],
)
def test_degraded_hybrid_shows_keyword_heading_and_degradation_note(
    tmp_path, monkeypatch, vector_status: VectorPathStatus, expected_note: str
) -> None:
    hybrid = _ConfigurableHybridService(vector_status=vector_status)
    app = _start_app(tmp_path, monkeypatch, hybrid)

    app.text_input(key="search_query_input").set_value("定时器")
    app.radio(key="search_mode_value").set_value("hybrid")
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert hybrid.calls == ["定时器"]
    assert expected_note in _caption_values(app)
    assert KEYWORD_HEADING in _subheader_values(app)
    assert not any("混合检索结果" in value for value in _subheader_values(app))


def test_pure_keyword_search_has_no_fallback_notice(tmp_path, monkeypatch) -> None:
    hybrid = _ConfigurableHybridService()
    app = _start_app(tmp_path, monkeypatch, hybrid)

    app.text_input(key="search_query_input").set_value("定时器")
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert hybrid.calls == []
    assert FALLBACK_NOTICE not in _caption_values(app)
    assert KEYWORD_HEADING in _subheader_values(app)
    assert not any("混合检索结果" in value for value in _subheader_values(app))
