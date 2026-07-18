"""v0.0.7 result understanding, grouping, preview, and hit-navigation tests."""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

import pytest
import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import (
    PageStatus,
    SearchField,
    SearchFilters,
    SearchResult,
    SearchSort,
    SearchViewMode,
)
from src.search_navigation import (
    document_hit_results,
    group_search_results,
    locate_result,
    reader_query_params,
    state_for_result,
    unique_ordered_results,
)
from src.search_service import SearchService
from src.search_state import (
    SearchPageState,
    decode_return_state,
    encode_return_state,
    parse_search_state,
    search_state_query_params,
)
from src.text_utils import build_context_excerpts, highlight_html, literal_match_spans


def _create_document(
    database: Database, tmp_path: Path, *, title: str, filename: str, sha: str
):
    source_path = tmp_path / "raw" / filename
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"pdf")
    return database.create_document(
        title=title,
        filename=filename,
        source_path=source_path,
        sha256=sha * 64,
    )


def _library(tmp_path: Path) -> tuple[Database, dict[str, object]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    first = _create_document(
        database,
        tmp_path,
        title="自动控制原理",
        filename="control-manual [A]+.pdf",
        sha="a",
    )
    second = _create_document(
        database,
        tmp_path,
        title="液压系统",
        filename="hydraulic.pdf",
        sha="b",
    )
    pages = []
    for page_number in range(1, 13):
        image_path = tmp_path / "pages" / str(first.id) / f"page_{page_number:04d}.png"
        if page_number != 12:
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (12, 16), "white").save(image_path)
        extracted = (
            "控制器 <script>alert(1)</script> PID 第一处。"
            "中间文字" * 20
            + "控制 第二处。"
            if page_number == 1
            else f"控制系统正文，第 {page_number} 页，PID 参数。"
        )
        page = database.create_page(
            document_id=first.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=extracted,
            ocr_text="OCR 控制识别文本" if page_number == 2 else "",
            markdown_content="# 页面笔记\n控制整定说明" if page_number == 1 else "",
            status=PageStatus.REVIEWED if page_number <= 2 else PageStatus.PENDING,
        )
        pages.append(page)
    second_image = tmp_path / "pages" / str(second.id) / "page_0001.png"
    second_image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 16), "white").save(second_image)
    second_page = database.create_page(
        document_id=second.id,
        page_number=1,
        image_path=second_image,
        extracted_text="液压控制回路与控制阀。",
        status=PageStatus.DRAFT,
    )
    other_image = tmp_path / "pages" / str(second.id) / "page_0002.png"
    Image.new("RGB", (12, 16), "white").save(other_image)
    database.create_page(
        document_id=second.id,
        page_number=2,
        image_path=other_image,
        extracted_text="没有相关字样。",
    )
    database.update_document_page_count(first.id, 12)
    database.update_document_page_count(second.id, 2)
    tag = database.create_tag("控制 [A]+")
    project = database.create_project("控制工程")
    database.set_document_tags(first.id, [tag.id])
    database.set_document_projects(first.id, [project.id])
    return database, {
        "first": first,
        "second": second,
        "pages": pages,
        "second_page": second_page,
        "tag": tag,
        "project": project,
    }


@pytest.fixture
def ui_library(tmp_path: Path, monkeypatch):
    database, ids = _library(tmp_path)
    document_service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: document_service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    return database, ids


def test_view_state_defaults_round_trips_and_rejects_illegal_values() -> None:
    assert parse_search_state({}).view_mode is SearchViewMode.PAGE
    state = SearchPageState(
        query="控制 <script>",
        sort=SearchSort.DOCUMENT_PAGE,
        result_page=3,
        filters_open=True,
        view_mode=SearchViewMode.DOCUMENT,
        expanded_document_id=7,
        preview_page_id=19,
        focus_result=22,
    )
    params = search_state_query_params(state)
    assert parse_search_state(params) == state
    assert decode_return_state(encode_return_state(state)) == state
    assert params["view"] == "document"
    illegal = parse_search_state(
        {
            "view": "document; DROP TABLE pages",
            "expanded_document": "-2",
            "preview_page": "<script>",
            "focus_result": "999999999",
        }
    )
    assert illegal.view_mode is SearchViewMode.PAGE
    assert illegal.expanded_document_id is None
    assert illegal.preview_page_id is None
    assert illegal.focus_result is None


def test_document_groups_are_unique_counted_and_stably_ordered(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    service = SearchService(database, max_results=100)
    results = service.search("控制", limit=100)
    counts = service.document_counts("控制")
    groups = group_search_results([*results, results[0]], document_counts=counts)

    assert len(results) == 13
    assert [group.document_id for group in groups] == [
        ids["first"].id,
        ids["second"].id,
    ]
    assert [group.total_count for group in groups] == [12, 1]
    assert len({item.page_id for group in groups for item in group.results}) == 13
    assert [item.page_id for item in groups[0].results] == [
        item.page_id for item in results if item.document_id == ids["first"].id
    ]
    assert groups[0].best_result.rank == min(item.rank for item in groups[0].results)


def test_grouping_empty_and_abnormal_records_degrades_safely(tmp_path: Path) -> None:
    assert group_search_results([]) == ()
    abnormal = SearchResult(
        page_id=9,
        document_id=999,
        document_title="",
        filename="",
        page_number=4,
        image_path=tmp_path / "missing.png",
        content="控制",
        snippet="控制",
        rank=0.0,
        status=PageStatus.PENDING,
    )
    groups = group_search_results([abnormal, abnormal])
    assert len(groups) == 1
    assert groups[0].document_id == 999
    assert len(groups[0].results) == 1


def test_global_and_document_navigation_boundaries_and_state(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    results = SearchService(database, max_results=100).search("控制", limit=100)
    first = locate_result(results, results[0].page_id)
    middle = locate_result(results, results[5].page_id)
    last = locate_result(results, results[-1].page_id)
    assert first and first.previous is None and first.index == 1
    assert middle and middle.previous and middle.next and middle.index == 6
    assert last and last.next is None and last.index == len(results)
    assert locate_result(results, 999999) is None
    assert len(unique_ordered_results([results[0], results[0]])) == 1

    document_hits = document_hit_results(results, ids["first"].id)
    assert len(document_hits) == 12
    assert [item.page_number for item in document_hits] == list(range(1, 13))
    assert document_hit_results(results, 999) == ()
    page_state = state_for_result(
        SearchPageState("控制"), result_index=12, document_id=ids["first"].id
    )
    grouped_state = state_for_result(
        SearchPageState("控制", view_mode=SearchViewMode.DOCUMENT),
        result_index=12,
        document_id=ids["first"].id,
    )
    assert page_state.result_page == 2 and page_state.focus_result == 12
    assert grouped_state.expanded_document_id == ids["first"].id


def test_navigation_order_tracks_sort_and_filters(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    service = SearchService(database, max_results=100)
    document_order = service.search(
        "控制", limit=100, sort_by=SearchSort.DOCUMENT_PAGE
    )
    filtered = service.search(
        "控制",
        limit=100,
        filters=SearchFilters(document_ids=(ids["second"].id,)),
        sort_by=SearchSort.DOCUMENT_PAGE,
    )
    assert [(item.document_title, item.page_number) for item in document_order] == sorted(
        (item.document_title, item.page_number) for item in document_order
    )
    assert [item.page_id for item in filtered] == [ids["second_page"].id]
    assert locate_result(filtered, ids["second_page"].id).total == 1  # type: ignore[union-attr]


def test_multiple_snippets_sources_counts_and_html_safety(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    service = SearchService(database, max_results=100, snippet_length=90)
    result = next(
        item
        for item in service.search("控制", limit=100)
        if item.page_id == ids["pages"][0].id
    )
    assert 1 <= len(result.snippets) <= 3
    assert result.match_count >= 4
    assert {snippet.field for snippet in result.snippets} <= set(result.match_fields)
    assert SearchField.MARKDOWN in result.match_fields
    assert SearchField.EXTRACTED_TEXT in result.match_fields
    rendered = service.highlighted_snippet(result, "控制")
    assert "<mark>控制</mark>" in rendered
    assert html.unescape(rendered.replace("<mark>", "").replace("</mark>", "")) == result.snippet
    extracted_rendered = highlight_html(result.extracted_text, ("控制",))
    assert "<script>" not in extracted_rendered and "&lt;script&gt;" in extracted_rendered
    assert html.unescape(
        extracted_rendered.replace("<mark>", "").replace("</mark>", "")
    ) == result.extracted_text


@pytest.mark.parametrize(
    ("field", "query"),
    [
        (SearchField.DOCUMENT_TITLE, "自动控制原理"),
        (SearchField.FILENAME, "control"),
        (SearchField.EXTRACTED_TEXT, "参数"),
        (SearchField.OCR_TEXT, "识别文本"),
        (SearchField.MARKDOWN, "整定说明"),
    ],
)
def test_each_text_source_produces_a_labelled_snippet(
    tmp_path: Path, field: SearchField, query: str
) -> None:
    database, _ = _library(tmp_path)
    results = SearchService(database, max_results=100).search(
        query,
        limit=100,
        filters=SearchFilters(match_fields=(field,)),
    )
    assert results
    assert all(field in result.match_fields for result in results)
    assert any(snippet.field is field for result in results for snippet in result.snippets)


def test_excerpt_dedup_chinese_special_char_and_empty_fallback() -> None:
    text = "控制开始" + "甲" * 220 + "控制结束" + "乙" * 220 + "控制尾部"
    excerpts = build_context_excerpts(text, ("控制",), max_chars=80, max_excerpts=3)
    assert len(excerpts) == 3
    assert all("控制" in excerpt for excerpt in excerpts)
    assert literal_match_spans("控制控制", ("控制", "控")) == ((0, 2), (2, 4))
    assert build_context_excerpts("", ("控制",)) == ()
    special = highlight_html("<b>[A]+ 控制 & 数据</b>", ("[A]", "+", "控制"))
    assert special.count("<mark>") == 3
    assert "<b>" not in special and "&lt;b&gt;" in special


def test_document_count_query_is_one_aggregate_without_result_n_plus_one(
    tmp_path: Path, monkeypatch
) -> None:
    database, _ = _library(tmp_path)
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("src.database.sqlite3.connect", traced_connect)
    counts = SearchService(database).document_counts("控制")
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert sum(counts.values()) == 13
    assert len(selects) == 1


def test_search_ui_switches_group_view_restores_and_expands(ui_library) -> None:
    _, ids = ui_library
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path))
    app.query_params = {"q": "控制", "limit": "20"}
    app.run(timeout=15)
    assert not app.exception
    assert app.radio(key="search_view_mode").value == SearchViewMode.PAGE.value
    app.radio(key="search_view_mode").set_value(SearchViewMode.DOCUMENT.value).run()
    assert app.query_params["view"] == [SearchViewMode.DOCUMENT.value]
    assert any("命中 12 个页面" in item.value for item in app.caption)
    app.button(key=f"toggle_group_{ids['first'].id}").click().run()
    assert app.query_params["expanded_document"] == [str(ids["first"].id)]
    assert app.button(key=f"open_result_{ids['pages'][0].id}")

    restored = AppTest.from_file(str(search_path))
    restored.query_params = dict(app.query_params)
    restored.run(timeout=15)
    assert not restored.exception
    assert restored.radio(key="search_view_mode").value == SearchViewMode.DOCUMENT.value
    assert restored.button(key=f"toggle_group_{ids['first'].id}").label == "收起命中页"


def test_real_page_switch_pending_params_are_revalidated_on_target(ui_library) -> None:
    database, ids = ui_library
    result = SearchService(database, max_results=100).search("控制", limit=20)[0]
    state = SearchPageState(
        "控制",
        limit=20,
        view_mode=SearchViewMode.DOCUMENT,
        expanded_document_id=result.document_id,
        focus_result=1,
    )
    params = reader_query_params(result, state.query, return_state=state)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    reader = AppTest.from_file(str(reader_path))
    reader.session_state["pending_reader_query_params"] = params
    reader.run(timeout=15)
    assert not reader.exception
    assert reader.query_params["from_search"] == ["1"]
    assert reader.query_params["document"] == [str(result.document_id)]
    assert decode_return_state(reader.query_params["search_return"][0]) == state

    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    search = AppTest.from_file(str(search_path))
    search.session_state["pending_search_query_params"] = search_state_query_params(state)
    search.run(timeout=15)
    assert not search.exception
    assert search.query_params["view"] == [SearchViewMode.DOCUMENT.value]
    assert search.query_params["expanded_document"] == [str(result.document_id)]


def test_quick_preview_state_image_note_and_evidence_workflow(ui_library) -> None:
    database, ids = ui_library
    page = ids["pages"][0]
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path))
    app.query_params = {"q": "控制", "limit": "20"}
    app.run(timeout=15)
    app.button(key=f"toggle_preview_{page.id}").click().run()
    assert app.query_params["preview_page"] == [str(page.id)]
    assert app.image
    assert any("页面笔记 / Markdown 已存在" in item.value for item in app.caption)

    app.button(key=f"add_basket_result_{page.id}").click().run()
    item = EvidenceBasketService(database).list_items()[0]
    app.button(key=f"toggle_preview_{page.id}").click().run()
    app.button(key=f"toggle_preview_{page.id}").click().run()
    app.text_area(key=f"search_evidence_note_{item.id}").input("快速预览备注")
    app.button(key=f"save_search_evidence_note_{item.id}").click().run()
    assert EvidenceBasketService(database).list_items()[0].user_note == "快速预览备注"
    app.button(key=f"remove_search_evidence_{item.id}").click().run()
    assert EvidenceBasketService(database).list_items() == []
    assert "q" in app.query_params and app.query_params["q"] == ["控制"]


def test_quick_preview_missing_image_and_markdown_is_safe(ui_library) -> None:
    _, ids = ui_library
    missing_page = ids["pages"][-1]
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path))
    app.query_params = {"q": "控制", "limit": "20", "result_page": "2"}
    app.run(timeout=15)
    app.button(key=f"toggle_preview_{missing_page.id}").click().run()
    assert not app.exception
    assert any("页面图像缺失" in item.value for item in app.warning)
    assert any("尚无 Markdown" in item.value for item in app.caption)
    app.button(key=f"toggle_preview_{missing_page.id}").click().run()
    assert "preview_page" not in app.query_params


def test_reader_global_document_hit_and_adjacent_navigation(ui_library, monkeypatch) -> None:
    _, ids = ui_library
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    state = SearchPageState("控制", limit=20, view_mode=SearchViewMode.DOCUMENT)
    first_page = ids["pages"][0]
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {
        "document": str(ids["first"].id),
        "page": "1",
        "from_search": "1",
        "search_query": "控制",
        "search_return": encode_return_state(state),
    }
    app.run(timeout=15)
    assert not app.exception
    assert app.button(key="reader_global_previous_hit").disabled
    assert not app.button(key="reader_global_next_hit").disabled
    assert app.button(key="reader_document_previous_hit").disabled
    assert not app.button(key="reader_document_next_hit").disabled
    assert any("第 1 / 13 个结果" in item.value for item in app.caption)
    assert any("本文件完整命中 12 页" in item.value for item in app.caption)
    assert any("命中当前搜索" in item.value for item in app.caption)
    assert app.button(key=f"reader_open_hit_{first_page.id}").disabled

    app.button(key="reader_global_next_hit").click().run()
    assert app.query_params["page"] == ["2"]
    returned_state = decode_return_state(app.query_params["search_return"][0])
    assert returned_state.focus_result == 2
    assert returned_state.expanded_document_id == ids["first"].id


def test_reader_last_boundary_single_document_hit_and_absent_page(ui_library) -> None:
    _, ids = ui_library
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    state = SearchPageState(
        "控制",
        filters=SearchFilters(document_ids=(ids["second"].id,)),
        limit=20,
    )
    app = AppTest.from_file(str(reader_path))
    app.query_params = {
        "document": str(ids["second"].id),
        "page": "1",
        "from_search": "1",
        "search_query": "控制",
        "search_return": encode_return_state(state),
    }
    app.run(timeout=15)
    assert app.button(key="reader_global_previous_hit").disabled
    assert app.button(key="reader_global_next_hit").disabled
    assert app.button(key="reader_document_previous_hit").disabled
    assert app.button(key="reader_document_next_hit").disabled
    assert any("第 1 / 1 个结果" in item.value for item in app.caption)

    absent = AppTest.from_file(str(reader_path))
    absent.query_params = {
        "document": str(ids["second"].id),
        "page": "2",
        "from_search": "1",
        "search_query": "控制",
        "search_return": encode_return_state(state),
    }
    absent.run(timeout=15)
    assert not absent.exception
    assert any("当前页面不在重建后的搜索结果范围内" in item.value for item in absent.info)


def test_reader_many_document_hits_uses_bounded_batches(ui_library) -> None:
    _, ids = ui_library
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    state = SearchPageState("控制", limit=20)
    app = AppTest.from_file(str(reader_path))
    app.query_params = {
        "document": str(ids["first"].id),
        "page": "1",
        "from_search": "1",
        "search_query": "控制",
        "search_return": encode_return_state(state),
    }
    app.run(timeout=15)
    selector = app.selectbox(key=f"reader_hit_batch_{ids['first'].id}")
    assert len(selector.options) == 2
    hit_buttons = [
        button
        for button in app.button
        if button.key and button.key.startswith("reader_open_hit_")
    ]
    assert len(hit_buttons) == 10


def test_schema_v4_database_starts_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.db"
    first = Database(path)
    second = Database(path)
    with second._connection() as connection:  # noqa: SLF001 - integrity assertion
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert first.SCHEMA_VERSION == second.SCHEMA_VERSION == 4
    assert [row[0] for row in versions] == [1, 2, 3, 4]
    assert integrity == "ok" and foreign_keys == []
