"""UI tests for the cross-document knowledge aggregation page (AppTest).

Fixtures use temporary databases and synthetic files only. Production data
and port 8501 are never touched. Deletion goes through
DocumentDeletionService, never raw SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.aggregation_service import AggregationService
from src.aggregation_ui import build_source_params
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.evidence_basket_service import EvidenceBasketService
from src.models import NoteImportance, NoteType
from src.note_service import NoteService

AGGREGATION_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("8_*.py")))


def _create_document(
    database: Database,
    raw_dir: Path,
    pages_dir: Path,
    *,
    title: str,
    sha_letter: str,
    page_count: int = 1,
):
    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=page_count,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 50)
    pages = []
    for number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        pages.append(
            database.create_page(
                document_id=document.id,
                page_number=number,
                image_path=image_path,
                extracted_text=f"第 {number} 页 阀体 回路 {title}",
            )
        )
    database.update_document_page_count(document.id, page_count)
    return document, pages


def _build_app(tmp_path: Path, monkeypatch, *, with_content: bool = True):
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    context = {}
    if with_content:
        alpha, alpha_pages = _create_document(
            database, raw_dir, pages_dir, title="甲文档", sha_letter="a", page_count=2
        )
        beta, beta_pages = _create_document(
            database, raw_dir, pages_dir, title="乙文档", sha_letter="b"
        )
        tag = database.create_tag("泵")
        project = database.create_project("主项目")
        database.set_document_tags(alpha.id, [tag.id])
        database.set_document_projects(alpha.id, [project.id])
        database.set_document_projects(beta.id, [project.id])
        notes = NoteService(database)
        notes.create_document_note(alpha.id, "甲文档级笔记", importance="primary")
        notes.create_page_note(alpha_pages[0].id, "甲页面笔记", importance="secondary")
        notes.create_text_selection_note(alpha_pages[0].id, "阀体", "甲选区笔记")
        notes.create_page_note(beta_pages[0].id, "乙页面笔记", importance="primary")
        EvidenceBasketService(database).add_item(
            document_id=alpha.id,
            page_id=alpha_pages[0].id,
            evidence_text="阀体",
            user_note="关键参数出处",
        )
        context = {
            "database": database,
            "alpha": alpha,
            "alpha_pages": alpha_pages,
            "beta": beta,
            "beta_pages": beta_pages,
            "tag": tag,
            "project": project,
        }
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    app = AppTest.from_file(AGGREGATION_PAGE).run(timeout=30)
    return app, database, data_dir, raw_dir, pages_dir, markdown_dir, context


def _markdown_values(app: AppTest) -> list[str]:
    return [item.value for item in app.markdown]


def _caption_values(app: AppTest) -> list[str]:
    return [item.value for item in app.caption]


# --- loading and empty states ---------------------------------------------------


def test_page_loads_with_project_axis(tmp_path: Path, monkeypatch) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    assert not app.exception
    radios = [radio for radio in app.radio if radio.key == "agg_axis"]
    assert radios and list(radios[0].options) == ["项目", "标签"]
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["涉及文档"] == "2"
    assert metrics["笔记"] == "4"
    assert metrics["证据"] == "1"
    assert metrics["总条目"] == "5"


def test_no_projects_shows_empty_hint(tmp_path: Path, monkeypatch) -> None:
    database_dir = tmp_path / "data"
    (database_dir / "database").mkdir(parents=True)
    database = Database(database_dir / "database" / "knowledge.db")
    database.create_tag("孤立标签")
    monkeypatch.setattr(runtime, "application_database", lambda: database)

    app = AppTest.from_file(AGGREGATION_PAGE).run(timeout=30)

    assert not app.exception
    assert any("还没有项目" in info.value for info in app.info)


def test_no_tags_shows_empty_hint(tmp_path: Path, monkeypatch) -> None:
    database_dir = tmp_path / "data"
    (database_dir / "database").mkdir(parents=True)
    database = Database(database_dir / "database" / "knowledge.db")
    database.create_project("孤立项目")
    monkeypatch.setattr(runtime, "application_database", lambda: database)

    app = AppTest.from_file(AGGREGATION_PAGE).run(timeout=30)
    app.radio(key="agg_axis").set_value("标签").run()

    assert not app.exception
    assert any("还没有标签" in info.value for info in app.info)


def test_empty_axis_aggregation_shows_hint(tmp_path: Path, monkeypatch) -> None:
    database_dir = tmp_path / "data"
    (database_dir / "database").mkdir(parents=True)
    database = Database(database_dir / "database" / "knowledge.db")
    database.create_project("主项目")
    lonely = database.create_project("空项目")
    monkeypatch.setattr(runtime, "application_database", lambda: database)

    app = AppTest.from_file(AGGREGATION_PAGE).run(timeout=30)
    app.selectbox(key="agg_project_id").set_value(lonely.id).run()

    assert not app.exception
    assert any("目前还没有可聚合的笔记或证据" in info.value for info in app.info)


# --- axis results and item rendering ---------------------------------------------


def test_project_aggregation_renders_notes_and_evidence(tmp_path: Path, monkeypatch) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    markdown = _markdown_values(app)
    captions = _caption_values(app)

    assert any("甲文档" in value and "文档级" in value for value in markdown)
    assert any("甲文档" in value and "第 1 页" in value for value in markdown)
    assert any("结构化笔记 · 文档级" in value for value in markdown)
    assert any("结构化笔记 · 页面级" in value for value in markdown)
    assert any("结构化笔记 · 文字选区" in value for value in markdown)
    assert any("证据（来自证据篮）" in value for value in markdown)
    assert any("批注：关键参数出处" in value for value in captions)
    assert any("标签：泵" in value for value in captions)
    assert any("项目：主项目" in value for value in captions)
    assert any(value == "甲文档级笔记" for value in markdown)
    assert any(value == "阀体" for value in markdown)


def test_tag_aggregation_via_radio(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _, _, _, context = _build_app(tmp_path, monkeypatch)
    app.radio(key="agg_axis").set_value("标签").run()
    app.selectbox(key="agg_tag_id").set_value(context["tag"].id).run()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["总条目"] == "4"  # 甲文档的 3 条笔记 + 1 条证据
    assert not any(value == "乙页面笔记" for value in _markdown_values(app))


def test_importance_filter_hides_evidence_with_explanation(
    tmp_path: Path, monkeypatch
) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    app.selectbox(key="agg_importance").set_value(NoteImportance.PRIMARY).run()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["笔记"] == "2"
    assert metrics["证据"] == "0"
    assert any(
        "证据条目没有重要性等级" in value for value in _caption_values(app)
    )
    # Evidence never wears a fabricated importance badge.
    assert not any(
        "证据（来自证据篮）" in value and "重点" in value
        for value in _markdown_values(app)
    )


def test_note_type_filter(tmp_path: Path, monkeypatch) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    app.selectbox(key="agg_note_type").set_value(NoteType.TEXT_SELECTION).run()

    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["总条目"] == "1"
    assert any(value == "甲选区笔记" for value in _markdown_values(app))


def test_empty_filter_result_shows_hint(tmp_path: Path, monkeypatch) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    app.selectbox(key="agg_note_type").set_value(NoteType.IMAGE_REGION).run()

    assert any("放宽筛选条件" in info.value for info in app.info)


# --- pagination and state ---------------------------------------------------------


def _add_many_notes(database: Database, context) -> None:
    notes = NoteService(database)
    for index in range(26):
        notes.create_page_note(
            context["alpha_pages"][index % 2].id, f"批量笔记{index:02d}"
        )


def test_pagination_flow(tmp_path: Path, monkeypatch) -> None:
    app, database, *_rest, context = _build_app(tmp_path, monkeypatch)
    _add_many_notes(database, context)
    app.run(timeout=30)

    assert any("第 1 / 2 页" in value for value in _markdown_values(app))
    app.button(key="agg_next").click().run()
    assert any("第 2 / 2 页" in value for value in _markdown_values(app))
    app.button(key="agg_prev").click().run()
    assert any("第 1 / 2 页" in value for value in _markdown_values(app))


def test_filter_change_resets_to_first_page(tmp_path: Path, monkeypatch) -> None:
    app, database, *_rest, context = _build_app(tmp_path, monkeypatch)
    _add_many_notes(database, context)
    app.run(timeout=30)
    app.button(key="agg_next").click().run()
    assert app.session_state["agg_page"] == 2

    app.selectbox(key="agg_importance").set_value(NoteImportance.SECONDARY).run()

    assert app.session_state["agg_page"] == 1


def test_stale_axis_id_in_url_falls_back_safely(tmp_path: Path, monkeypatch) -> None:
    app, database, *_ = _build_app(tmp_path, monkeypatch)
    stale = AppTest.from_file(AGGREGATION_PAGE)
    stale.query_params["agg_axis"] = "project"
    stale.query_params["agg_id"] = "99999"
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    stale.run(timeout=30)

    assert not stale.exception
    assert any("已不存在" in info.value for info in stale.info)


# --- navigation --------------------------------------------------------------------


def test_open_source_navigation_params(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _, _, _, context = _build_app(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))

    app.button(key="agg_open_note_2").click().run()  # 甲页面笔记（第 1 页）

    assert switched == ["pages/3_浏览资料.py"]
    assert app.query_params.get("document") == [str(context["alpha"].id)]
    assert app.query_params.get("page") == ["1"]
    assert app.query_params.get("from_search") == ["0"]


def test_document_level_note_opens_page_one(tmp_path: Path, monkeypatch) -> None:
    _, _, _, _, _, _, context = _build_app(tmp_path, monkeypatch)
    item = next(
        entry
        for entry in _aggregation_items(context)
        if entry.note_type is NoteType.DOCUMENT
    )
    params = build_source_params(item)
    assert params["document"] == str(context["alpha"].id)
    assert params["page"] == "1"


def _aggregation_items(context):
    return AggregationService(context["database"]).aggregate_by_project(
        context["project"].id
    ).items


def test_evidence_navigation_buttons(tmp_path: Path, monkeypatch) -> None:
    app, database, _, _, _, _, context = _build_app(tmp_path, monkeypatch)
    evidence_item = next(
        entry for entry in _aggregation_items(context) if entry.basket_id is not None
    )
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))

    app.button(key=f"agg_basket_{evidence_item.source_id}").click().run()
    assert switched == ["pages/7_证据篮.py"]

    params = build_source_params(evidence_item)
    assert params["document"] == str(context["alpha"].id)
    assert params["page"] == "1"


# --- read-only and freshness guarantees -------------------------------------------


def test_page_has_no_editing_entries(tmp_path: Path, monkeypatch) -> None:
    app, *_ = _build_app(tmp_path, monkeypatch)
    button_labels = [button.label for button in app.button]
    for forbidden in ("编辑", "删除", "修改", "移出", "加入"):
        assert not any(forbidden in label for label in button_labels)
    text_input_labels = [field.label for field in app.text_input]
    assert not any("笔记" in label for label in text_input_labels)


def test_deleted_document_disappears_without_stale_items(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, data_dir, raw_dir, pages_dir, markdown_dir, context = _build_app(
        tmp_path, monkeypatch
    )
    assert any(value == "甲文档级笔记" for value in _markdown_values(app))

    DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
    ).delete_document(context["alpha"].id, expected_title=context["alpha"].title)
    app.run(timeout=30)

    assert not app.exception
    markdown = _markdown_values(app)
    assert not any(value == "甲文档级笔记" for value in markdown)
    assert any(value == "乙页面笔记" for value in markdown)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["总条目"] == "1"


def test_page_interactions_never_write_to_database(tmp_path: Path, monkeypatch) -> None:
    app, database, _, _, _, _, context = _build_app(tmp_path, monkeypatch)

    def snapshot() -> tuple:
        with sqlite3.connect(database.database_path) as connection:
            tables = sorted(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            )
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("notes", "evidence_items", "documents", "pages")
            )
        return tuple(tables), counts

    before = snapshot()
    app.radio(key="agg_axis").set_value("标签").run()
    app.selectbox(key="agg_tag_id").set_value(context["tag"].id).run()
    app.selectbox(key="agg_importance").set_value(NoteImportance.PRIMARY).run()
    app.run(timeout=30)
    assert snapshot() == before
