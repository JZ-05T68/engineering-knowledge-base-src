"""v0.0.6 fast filtering, URL state, contextual facets, and performance tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import PageStatus, SearchField, SearchFilters, SearchSort
from src.search_navigation import reader_query_params
from src.search_service import SearchService
from src.search_state import (
    SearchPageState,
    active_filter_labels,
    clear_search_filters,
    decode_return_state,
    encode_return_state,
    filter_named_options,
    parse_search_state,
    remove_search_filter,
    search_state_query_params,
)


def _document(database: Database, suffix: str, title: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pdf")
    return database.create_document(
        title=title,
        filename=path.name,
        source_path=path,
        sha256=suffix * 64,
    )


def _library(tmp_path: Path) -> tuple[Database, dict[str, int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    first_document = _document(
        database, "1", "自动控制原理", tmp_path / "raw" / "control [A]+.pdf"
    )
    second_document = _document(
        database, "2", "液压系统", tmp_path / "raw" / "hydraulic.pdf"
    )
    pages = []
    specifications = (
        (first_document, 1, "公共词 PID alpha", "PID 整定笔记", PageStatus.REVIEWED),
        (first_document, 2, "公共词 beta", "", PageStatus.PENDING),
        (second_document, 1, "公共词 gamma", "", PageStatus.DRAFT),
        (second_document, 2, "delta only", "", PageStatus.SKIPPED),
    )
    for document, page_number, text, note, status in specifications:
        image_path = tmp_path / "pages" / str(document.id) / f"page_{page_number}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        pages.append(
            database.create_page(
                document_id=document.id,
                page_number=page_number,
                image_path=image_path,
                extracted_text=text,
                markdown_content=note,
                status=status,
            )
        )
    database.update_document_page_count(first_document.id, 2)
    database.update_document_page_count(second_document.id, 2)
    tag_pid = database.create_tag("PID")
    tag_control = database.create_tag("控制 [A]+")
    database.set_document_tags(first_document.id, [tag_control.id])
    database.set_page_tags(pages[0].id, [tag_pid.id])
    database.set_page_tags(pages[2].id, [tag_control.id])
    project_control = database.create_project("自动控制项目")
    project_shared = database.create_project("公共项目")
    database.set_document_projects(first_document.id, [project_control.id])
    database.set_page_projects(pages[0].id, [project_shared.id])
    database.set_document_projects(second_document.id, [project_shared.id])
    basket_service = EvidenceBasketService(database)
    basket = basket_service.default_basket()
    basket_service.add_item(
        document_id=first_document.id,
        page_id=pages[0].id,
        evidence_text="公共词 PID alpha",
    )
    return database, {
        "first_document": first_document.id,
        "second_document": second_document.id,
        "first_page": pages[0].id,
        "second_page": pages[1].id,
        "third_page": pages[2].id,
        "tag_pid": tag_pid.id,
        "tag_control": tag_control.id,
        "project_control": project_control.id,
        "project_shared": project_shared.id,
        "basket": basket.id,
    }


def test_note_basket_and_combined_filters_share_the_normal_search_path(
    tmp_path: Path,
) -> None:
    database, ids = _library(tmp_path)
    service = SearchService(database)

    noted = service.search("公共词", filters=SearchFilters(has_note=True))
    in_basket = service.search(
        "公共词",
        filters=SearchFilters(evidence_basket_id=ids["basket"]),
    )
    combined = service.search(
        "alpha gamma",
        filters=SearchFilters(
            project_ids=(ids["project_control"], ids["project_shared"]),
            tag_ids=(ids["tag_control"], ids["tag_pid"]),
            statuses=(PageStatus.REVIEWED,),
            match_fields=(SearchField.EXTRACTED_TEXT,),
            has_note=True,
            evidence_basket_id=ids["basket"],
        ),
    )

    assert [result.page_id for result in noted] == [ids["first_page"]]
    assert [result.page_id for result in in_basket] == [ids["first_page"]]
    assert [result.page_id for result in combined] == [ids["first_page"]]


def test_contextual_facet_counts_predict_candidates_without_false_zeroes(
    tmp_path: Path,
) -> None:
    database, ids = _library(tmp_path)
    service = SearchService(database)
    counts = service.facet_counts(
        "公共词",
        filters=SearchFilters(
            document_ids=(ids["first_document"],),
            statuses=(PageStatus.REVIEWED,),
            tag_ids=(ids["tag_control"],),
        ),
    )

    assert counts.total == 1
    # Document and status counts ignore their own OR dimension.
    assert counts.documents[ids["first_document"]] == 1
    assert counts.documents[ids["second_document"]] == 0
    assert counts.statuses[PageStatus.PENDING] == 1
    # Tags retain current AND selections; adding PID leaves one page.
    assert counts.tags[ids["tag_pid"]] == 1

    project_counts = service.facet_counts(
        "公共词",
        filters=SearchFilters(project_ids=(ids["project_control"],)),
    )
    assert project_counts.projects[ids["project_shared"]] == 1


def test_facet_and_result_metadata_queries_are_constant_count(
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
    SearchService(database).facet_counts("公共词")
    facet_selects = [sql for sql in statements if sql.lstrip().upper().startswith("WITH")]
    assert len(facet_selects) == 1

    statements.clear()
    SearchService(database).search("公共词", limit=100)
    result_selects = [sql for sql in statements if sql.lstrip().upper().startswith("WITH")]
    assert len(result_selects) == 2  # main result query + one bulk metadata query


def test_url_state_round_trip_remove_clear_and_illegal_parameter_fallback() -> None:
    state = SearchPageState(
        query="液压 + % [A]",
        filters=SearchFilters(
            document_ids=(2, 1),
            project_ids=(3,),
            tag_ids=(4, 5),
            statuses=(PageStatus.REVIEWED,),
            match_fields=(SearchField.MARKDOWN,),
            has_note=True,
            evidence_basket_id=7,
        ),
        sort=SearchSort.UPDATED_DESC,
        limit=100,
        result_page=3,
        filters_open=True,
    )

    params = search_state_query_params(state)
    assert parse_search_state(params) == state
    assert decode_return_state(encode_return_state(state)) == state
    removed = remove_search_filter(state, "tag", 4)
    assert removed.filters.tag_ids == (5,) and removed.query == state.query
    kept = clear_search_filters(state, keep_query=True)
    cleared = clear_search_filters(state, keep_query=False)
    assert kept.query == state.query and kept.filters == SearchFilters()
    assert cleared.query == "" and cleared.sort is SearchSort.UPDATED_DESC

    illegal = parse_search_state(
        {
            "q": "安全",
            "documents": "1,broken,-2,3",
            "statuses": "reviewed,not-a-status",
            "fields": "markdown,<script>",
            "sort": "rank; DROP TABLE pages",
            "limit": "37",
            "result_page": "-9",
            "basket": "0 OR 1=1",
        }
    )
    assert illegal.filters.document_ids == (1, 3)
    assert illegal.filters.statuses == (PageStatus.REVIEWED,)
    assert illegal.filters.match_fields == (SearchField.MARKDOWN,)
    assert illegal.filters.evidence_basket_id is None
    assert illegal.sort is SearchSort.RELEVANCE
    assert illegal.limit == 40 and illegal.result_page == 1


def test_active_labels_and_unicode_option_finder_are_safe_and_reversible() -> None:
    state = SearchPageState(
        "PID",
        SearchFilters(document_ids=(1,), tag_ids=(2,), has_note=True),
    )
    labels = active_filter_labels(
        state,
        document_names={1: "自动控制 [A]+"},
        tag_names={2: "PID (整定)"},
    )

    assert [label.label for label in labels] == [
        "文档：自动控制 [A]+",
        "标签：PID (整定)",
        "其他：有笔记",
    ]
    options = {1: "自动控制 [A]+", 2: "液压系统", 3: "ＰＩＤ 整定"}
    assert filter_named_options(options, "[a]+ ") == (1,)
    assert filter_named_options(options, "pid") == (3,)
    assert filter_named_options(options, "不存在") == ()
    assert filter_named_options(options, "不存在", selected_ids=(2,)) == (2,)
    assert filter_named_options(options, "") == (1, 2, 3)


def test_reader_params_carry_complete_search_return_state(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    result = SearchService(database).search("公共词")[0]
    state = SearchPageState(
        "公共词",
        SearchFilters(
            project_ids=(ids["project_control"],),
            tag_ids=(ids["tag_control"],),
            statuses=(PageStatus.REVIEWED,),
        ),
        SearchSort.DOCUMENT_PAGE,
        result_page=2,
        filters_open=True,
    )

    params = reader_query_params(result, state.query, return_state=state)

    assert params["document"] == str(result.document_id)
    assert params["page"] == str(result.page_number)
    assert decode_return_state(params["search_return"]) == state


def _ui_runtime(tmp_path: Path, monkeypatch) -> tuple[Database, dict[str, int]]:
    database, ids = _library(tmp_path)
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    return database, ids


def test_search_ui_fast_find_active_removal_shortcuts_and_url_restore(
    tmp_path: Path, monkeypatch
) -> None:
    _, ids = _ui_runtime(tmp_path, monkeypatch)
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path)).run(timeout=10)

    assert not app.exception
    assert not app.toggle(key="search_filters_open").value
    assert {
        "仅看已复核",
        "仅看待复核",
        "仅看有笔记",
        "仅看当前证据篮",
        "最近查看",
        "最近修改",
    } <= {button.label for button in app.button}

    app.text_input(key="search_query_input").input("公共词").run()
    app.button(key="apply_search_filters").click().run()
    app.toggle(key="search_filters_open").set_value(True).run()
    app.text_input(key="search_document_finder").input("[A]+").run()
    assert not app.exception
    assert len(app.multiselect(key="search_document_ids").options) == 1
    app.text_input(key="search_document_finder").input("").run()
    assert len(app.multiselect(key="search_document_ids").options) == 2

    app.multiselect(key="search_tag_ids").select(ids["tag_control"]).run()
    app.multiselect(key="search_status_values").select(PageStatus.REVIEWED.value).run()
    app.selectbox(key="search_sort_value").select(SearchSort.DOCUMENT_PAGE.value).run()
    app.button(key="apply_search_filters").click().run()

    assert app.query_params["q"] == ["公共词"]
    assert app.query_params["tags"] == [str(ids["tag_control"])]
    assert app.query_params["statuses"] == [PageStatus.REVIEWED.value]
    assert app.query_params["sort"] == [SearchSort.DOCUMENT_PAGE.value]
    app.button(key=f"remove_filter_tag_{ids['tag_control']}").click().run()
    assert "tags" not in app.query_params
    assert app.session_state["knowledge_query"] == "公共词"

    app.button(key="search_shortcut_2").click().run()
    assert app.query_params["has_note"] == ["1"]
    app.button(key="search_shortcut_5").click().run()
    assert app.query_params["sort"] == [SearchSort.UPDATED_DESC.value]

    restored = AppTest.from_file(str(search_path))
    restored.query_params = dict(app.query_params)
    restored.run(timeout=10)
    assert not restored.exception
    assert restored.session_state["knowledge_query"] == "公共词"
    assert restored.session_state["search_has_note"]
    assert restored.session_state["search_sort_value"] == SearchSort.UPDATED_DESC.value


def test_search_ui_no_result_explains_scope_and_can_undo(
    tmp_path: Path, monkeypatch
) -> None:
    _ui_runtime(tmp_path, monkeypatch)
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path)).run(timeout=10)
    app.text_input(key="search_query_input").input("公共词").run()
    app.button(key="apply_search_filters").click().run()
    app.toggle(key="search_filters_open").set_value(True).run()
    app.multiselect(key="search_field_values").select(SearchField.FILENAME.value).run()
    app.button(key="apply_search_filters").click().run()

    assert any("当前搜索范围过窄" in item.value for item in app.info)
    assert not app.button(key="undo_empty_search").disabled
    app.button(key="undo_empty_search").click().run()
    assert app.session_state["knowledge_results"]
    assert app.session_state["search_field_values"] == []


def test_reader_ui_edits_evidence_and_restores_complete_search_url(
    tmp_path: Path, monkeypatch
) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    return_state = SearchPageState(
        "公共词",
        SearchFilters(
            project_ids=(ids["project_control"],),
            tag_ids=(ids["tag_control"],),
            statuses=(PageStatus.REVIEWED,),
            has_note=True,
        ),
        SearchSort.DOCUMENT_PAGE,
        result_page=2,
        filters_open=True,
    )
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {
        "document": str(ids["first_document"]),
        "page": "1",
        "from_search": "1",
        "search_query": return_state.query,
        "search_return": encode_return_state(return_state),
    }
    app.run(timeout=10)

    item = EvidenceBasketService(database).list_items()[0]
    app.text_area(key=f"reader_evidence_note_{item.id}").input("返回状态备注")
    app.button(key=f"reader_save_evidence_note_{item.id}").click().run()
    assert EvidenceBasketService(database).list_items()[0].user_note == "返回状态备注"
    assert "search_return" in app.query_params

    next(button for button in app.button if button.label == "返回检索结果").click().run()
    assert switched[-1] == "pages/3_检索资料.py"
    assert app.query_params == {
        key: [value] for key, value in search_state_query_params(return_state).items()
    }


def test_sort_filter_panel_and_pagination_keep_the_same_url_page(
    tmp_path: Path, monkeypatch
) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    document = database.get_document(ids["second_document"])
    assert document is not None
    for page_number in range(3, 13):
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / str(document.id) / f"page_{page_number}.png",
            extracted_text=f"公共词 extra {page_number}",
        )
    database.update_document_page_count(document.id, 12)
    search_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(search_path)).run(timeout=10)
    app.text_input(key="search_query_input").input("公共词").run()
    app.button(key="apply_search_filters").click().run()
    app.button(key="search_next_page").click().run()
    assert app.query_params["result_page"] == ["2"]

    app.toggle(key="search_filters_open").set_value(True).run()
    assert app.query_params["result_page"] == ["2"]
    app.selectbox(key="search_sort_value").select(SearchSort.DOCUMENT_PAGE.value).run()
    app.button(key="apply_search_filters").click().run()
    assert app.query_params["result_page"] == ["2"]
    assert app.query_params["sort"] == [SearchSort.DOCUMENT_PAGE.value]


def test_schema_v4_remains_idempotent_without_v006_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    database = Database(database_path)
    reopened = Database(database_path)
    with reopened._connection() as connection:  # noqa: SLF001 - integrity assertion
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert database.SCHEMA_VERSION == reopened.SCHEMA_VERSION == 4
    assert [row[0] for row in versions] == [1, 2, 3, 4]
    assert integrity == "ok" and foreign_keys == []
