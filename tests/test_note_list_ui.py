"""Standalone structured-notes list page tests (AppTest).

Fixtures use temporary databases and synthetic PNGs only. Production data and
port 8501 are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.models import NoteType
from src.note_service import NoteNotFoundError, NoteService, NoteWriteError

LIST_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("6_*.py")))


def _build_app(tmp_path: Path, monkeypatch, *, page_notes: int = 0):
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    documents = {}
    for index, sha in enumerate(("4" * 64, "5" * 64), start=1):
        document = database.create_document(
            title=f"文档{index}",
            filename=f"doc{index}.pdf",
            source_path=raw_dir / f"doc{index}.pdf",
            sha256=sha,
        )
        image_path = pages_dir / str(document.id) / "page_0001.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=1,
            image_path=image_path,
            extracted_text=f"第 {index} 页 阀体 回路",
        )
        database.update_document_page_count(document.id, 1)
        documents[index] = document
    service = NoteService(database)
    doc1, doc2 = documents[1], documents[2]
    page1 = database.get_page_by_number(doc1.id, 1)
    service.create_document_note(doc1.id, "甲文档笔记")
    service.create_page_note(page1.id, "甲页面笔记")
    service.create_text_selection_note(page1.id, "阀体", "甲选区笔记")
    service.create_image_region_note(page1.id, 10, 20, 300, 400, "甲区域笔记")
    service.create_document_note(doc2.id, "乙文档笔记")
    for index in range(page_notes):
        service.create_page_note(page1.id, f"分页笔记 {index + 1:02d}")
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    app = AppTest.from_file(LIST_PAGE).run(timeout=25)
    return app, database, service, doc1, doc2, page1


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _warnings(app: AppTest) -> list[str]:
    return [warning.value for warning in app.warning]


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]


def _markdowns(app: AppTest) -> list[str]:
    return [markdown.value for markdown in app.markdown]


def _select(app: AppTest, label: str):
    return next(sb for sb in app.selectbox if sb.label == label)


# --- A. page & filters --------------------------------------------------------


def test_page_renders_filters_and_cards(tmp_path: Path, monkeypatch) -> None:
    app, _, _, doc1, _, _ = _build_app(tmp_path, monkeypatch)
    assert not app.exception
    assert "结构化笔记" in app.title[0].value
    assert _select(app, "文档筛选").value == 0
    assert _select(app, "类型筛选").value == "全部类型"
    captions = _captions(app)
    assert any("文档级笔记" in value and f"文档{doc1.id}" in value for value in captions)
    assert any("第 1 页" in value for value in captions)
    assert any("共 5 条" in value for value in captions)
    markdowns = _markdowns(app)
    assert "甲文档笔记" in markdowns and "> 阀体" in markdowns


def test_empty_state(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    app = AppTest.from_file(LIST_PAGE).run(timeout=25)
    assert any("还没有结构化笔记" in info.value for info in app.info)


def test_document_and_type_filters(tmp_path: Path, monkeypatch) -> None:
    app, _, service, doc1, doc2, _ = _build_app(tmp_path, monkeypatch)
    _select(app, "文档筛选").set_value(doc2.id).run()
    captions = _captions(app)
    assert any("共 1 条" in value for value in captions)
    assert any(f"文档{doc2.id}" in value for value in captions)

    _select(app, "文档筛选").set_value(0).run()
    _select(app, "类型筛选").set_value(NoteType.TEXT_SELECTION).run()
    assert any("共 1 条" in value for value in _captions(app))
    assert any("文字选区笔记" in value for value in _captions(app))


def test_filter_change_resets_page(tmp_path: Path, monkeypatch) -> None:
    app, _, service, doc1, _, _ = _build_app(tmp_path, monkeypatch, page_notes=30)
    _button(app, "note_list_next").click().run()
    assert any("第 2 /" in value for value in _captions(app))
    _select(app, "类型筛选").set_value(NoteType.DOCUMENT).run()
    assert any("第 1 /" in value for value in _captions(app))


# --- B/C. cards ------------------------------------------------------------------


def test_card_contents_per_type(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    captions = _captions(app)
    markdowns = _markdowns(app)
    assert any("区域：(10, 20) - (300, 400)" in value for value in captions)
    assert any("创建时图像 800 × 1200" in value for value in captions)
    assert any("原文快照（只读）" in value for value in captions)
    assert any("来源有效" in value for value in captions)
    assert "甲选区笔记" in markdowns and "甲区域笔记" in markdowns
    # 文档级卡片不得伪造页码
    doc_caption = next(
        value for value in captions if "文档级笔记" in value and "甲" not in value
    )
    assert "第" not in doc_caption.split("创建于")[0].split("·")[-1]


# --- D. return to source -----------------------------------------------------------


def test_return_to_source_handoff(tmp_path: Path, monkeypatch) -> None:
    app, database, service, doc1, _, page1 = _build_app(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    note_id = next(
        item.note.id
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is NoteType.PAGE
    )
    _button(app, f"note_list_source_{note_id}").click().run()
    assert switched == ["pages/3_浏览资料.py"]
    params = app.session_state["pending_reader_query_params"]
    assert params == {"document": str(doc1.id), "page": "1", "from_search": "0"}


def test_document_note_returns_document_first_page(tmp_path: Path, monkeypatch) -> None:
    app, _, service, doc1, _, _ = _build_app(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    note_id = next(
        item.note.id
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is NoteType.DOCUMENT and item.document_id == doc1.id
    )
    _button(app, f"note_list_source_{note_id}").click().run()
    params = app.session_state["pending_reader_query_params"]
    assert params["document"] == str(doc1.id)
    assert params["page"] == "1"


# --- E. editing ---------------------------------------------------------------------


def _note_id(service: NoteService, note_type: NoteType) -> int:
    return next(
        item.note.id
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is note_type
    )


def test_edit_document_note(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.DOCUMENT)
    _button(app, f"note_list_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_list_edit_personal_{note_id}").input("甲文档笔记（修订）").run()
    assert any("有未保存修改" in value for value in _warnings(app))
    _button(app, f"note_list_edit_save_{note_id}").click().run()
    assert service.get_note(note_id).note.personal_note == "甲文档笔记（修订）"


def test_edit_text_selection_excerpt_and_personal(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.TEXT_SELECTION)
    before = service.get_note(note_id).note
    _button(app, f"note_list_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_list_edit_excerpt_{note_id}").input("修订摘录").run()
    app.text_area(key=f"note_list_edit_personal_{note_id}").input("修订笔记").run()
    _button(app, f"note_list_edit_save_{note_id}").click().run()
    updated = service.get_note(note_id).note
    assert updated.user_excerpt == "修订摘录"
    assert updated.personal_note == "修订笔记"
    assert updated.source_excerpt_snapshot == before.source_excerpt_snapshot


def test_edit_region_personal_only_and_no_rebind_controls(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.IMAGE_REGION)
    _button(app, f"note_list_edit_open_{note_id}").click().run()
    assert f"note_list_edit_excerpt_{note_id}" not in {
        element.key for element in app.text_area
    }
    app.text_area(key=f"note_list_edit_personal_{note_id}").input("区域修订").run()
    _button(app, f"note_list_edit_save_{note_id}").click().run()
    assert service.get_note(note_id).note.personal_note == "区域修订"
    # 列表页不出现重绑/重框控件
    button_labels = {button.label for button in app.button}
    assert not any("重新绑定" in label or "重新框选" in label for label in button_labels)


def test_edit_failure_keeps_draft(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.PAGE)
    _button(app, f"note_list_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_list_edit_personal_{note_id}").input("失败草稿").run()
    monkeypatch.setattr(
        NoteService,
        "update_page_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_list_edit_save_{note_id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert service.get_note(note_id).note.personal_note == "甲页面笔记"


def _errors(app: AppTest) -> list[str]:
    return [error.value for error in app.error]


# --- F. deletion ----------------------------------------------------------------------


def test_delete_with_confirmation_and_page_clamp(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch, page_notes=16)
    # 共 21 条：第 2 页仅 1 条；删除后应自动回到第 1 页
    _button(app, "note_list_next").click().run()
    assert any("第 2 / 2 页" in value for value in _captions(app))
    note_id = service.list_note_summaries(limit=20, offset=20)[0].note.id
    app.checkbox(key=f"note_list_delete_confirm_{note_id}").check().run()
    assert not _button(app, f"note_list_delete_{note_id}").disabled
    _button(app, f"note_list_delete_{note_id}").click().run()
    assert any("第 1 / 1 页" in value for value in _captions(app))
    assert service.count_notes() == 20
    with pytest.raises(NoteNotFoundError):
        service.get_note(note_id)


def test_delete_button_disabled_until_confirmed(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.IMAGE_REGION)
    assert _button(app, f"note_list_delete_{note_id}").disabled
    app.checkbox(key=f"note_list_delete_confirm_{note_id}").check().run()
    assert not _button(app, f"note_list_delete_{note_id}").disabled
    _button(app, f"note_list_delete_{note_id}").click().run()
    with pytest.raises(NoteNotFoundError):
        service.get_note(note_id)


# --- G. pagination ---------------------------------------------------------------------


def test_pagination_basics(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch, page_notes=45)
    # 共 50 条，每页 20 → 3 页
    assert any("第 1 / 3 页" in value and "共 50 条" in value for value in _captions(app))
    assert any("第 1–20 条" in value for value in _captions(app))
    _button(app, "note_list_next").click().run()
    assert any("第 2 / 3 页" in value and "第 21–40 条" in value for value in _captions(app))
    _button(app, "note_list_next").click().run()
    assert any("第 3 / 3 页" in value and "第 41–50 条" in value for value in _captions(app))
    assert _button(app, "note_list_next").disabled
    _select(app, "每页条数").set_value(50).run()
    assert any("第 1 / 1 页" in value for value in _captions(app))


# --- H. performance ---------------------------------------------------------------------


def test_queries_are_bounded(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0, "list": 0, "options": 0, "get_note": 0, "read_png": 0, "get_page": 0}
    original_count = NoteService.count_notes
    original_list = NoteService.list_note_summaries
    original_options = NoteService.list_note_document_options
    original_get = NoteService.get_note
    original_read = NoteService._read_page_image

    def counted(name, original):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return wrapper

    def counted_read(self, path):
        calls["read_png"] += 1
        return original_read(self, path)

    def counted_page(self, page_id):
        calls["get_page"] += 1
        return original_get_page(self, page_id)

    original_get_page = Database.get_page
    monkeypatch.setattr(NoteService, "count_notes", counted("count", original_count))
    monkeypatch.setattr(NoteService, "list_note_summaries", counted("list", original_list))
    monkeypatch.setattr(
        NoteService, "list_note_document_options", counted("options", original_options)
    )
    monkeypatch.setattr(NoteService, "get_note", counted("get_note", original_get))
    monkeypatch.setattr(NoteService, "_read_page_image", counted_read)
    monkeypatch.setattr(Database, "get_page", counted_page)

    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    for key in calls:
        calls[key] = 0
    app.run(timeout=25)
    assert not app.exception
    assert calls["count"] == 1
    assert calls["list"] == 1
    assert calls["options"] == 1
    assert calls["get_note"] == 0
    assert calls["read_png"] == 0
    assert calls["get_page"] == 0

    # 展开区域预览后按需读取
    note_id = _note_id(service, NoteType.IMAGE_REGION)
    _button(app, f"note_list_preview_show_btn_{note_id}").click().run()
    assert calls["read_png"] >= 1


def test_region_preview_shows_status_and_overlay(tmp_path: Path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    note_id = _note_id(service, NoteType.IMAGE_REGION)
    _button(app, f"note_list_preview_show_btn_{note_id}").click().run()
    assert any("图片来源有效" in value for value in _captions(app))
