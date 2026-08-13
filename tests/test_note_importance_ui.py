"""v0.3.1 note importance interaction UI tests (AppTest).

Covers the frozen Phase 3 contract: create/edit selectors, badge rendering
from display preferences, widget-state isolation and rebind/reframe/delete
regressions. Temporary databases and synthetic PNGs only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.note_ui as note_ui
import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import NoteImportance
from src.note_service import NoteService, NoteWriteError

READER = str(next((Path(__file__).parents[1] / "pages").glob("3_*.py")))
PAGE_TEXT = "液压系统 阀体 回路 压力"


def _build_reader(tmp_path: Path, monkeypatch) -> tuple[AppTest, Database, NoteService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    document = database.create_document(
        title="等级界面测试",
        filename="importance-ui.pdf",
        source_path=raw_dir / "importance-ui.pdf",
        sha256="4" * 64,
    )
    image_path = pages_dir / str(document.id) / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(image_path)
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text=PAGE_TEXT,
    )
    database.update_document_page_count(document.id, 1)
    service = DocumentService(database, raw_dir, pages_dir, tmp_path / "markdown")
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=database,
            raw_dir=raw_dir,
            pages_dir=pages_dir,
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )
    note_service = NoteService(database)
    app = AppTest.from_file(READER).run(timeout=25)
    return app, database, note_service, document.id


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _selectbox(app: AppTest, key: str):
    matches = [sb for sb in app.selectbox if sb.key == key]
    assert matches, f"找不到选择框 {key}；现有：{[sb.key for sb in app.selectbox][:14]}"
    return matches[0]


def _warnings(app: AppTest) -> list[str]:
    return [warning.value for warning in app.warning]


def _markdowns(app: AppTest) -> list[str]:
    return [markdown.value for markdown in app.markdown]


def _page1(database: Database, document_id: int):
    return database.get_page_by_number(document_id, 1)


def _seed_selection(note_service: NoteService, page_id: int, level: str = "normal") -> int:
    return note_service.create_text_selection_note(
        page_id, "阀体", "个人判断", user_excerpt="阀体摘录", importance=level
    ).note.id


def _drag(x1=85, y1=128, x2=340, y2=510, width=850, height=1275):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "width": width, "height": height, "unix_time": 1}


def _mock_component(monkeypatch, value):
    monkeypatch.setattr(
        note_ui, "streamlit_image_coordinates", lambda source, **kwargs: value
    )


# --- A. CREATE -------------------------------------------------------------


def test_create_document_note_default_and_levels(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    selector = _selectbox(app, f"note_create_document_{document_id}_imp")
    assert selector.value == NoteImportance.NORMAL

    app.text_area(key=f"note_create_document_{document_id}").input("重点文档").run()
    _selectbox(app, f"note_create_document_{document_id}_imp").set_value(
        NoteImportance.PRIMARY
    ).run()
    _button(app, f"note_create_document_{document_id}_save").click().run()
    notes = note_service.list_document_notes(document_id)
    assert notes[0].note.importance == "primary"
    # 创建成功后选择器复位为一般
    assert _selectbox(app, f"note_create_document_{document_id}_imp").value == (
        NoteImportance.NORMAL
    )


def test_create_page_note_secondary(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_create_page_{page.id}").input("次重点页").run()
    _selectbox(app, f"note_create_page_{page.id}_imp").set_value(
        NoteImportance.SECONDARY
    ).run()
    _button(app, f"note_create_page_{page.id}_save").click().run()
    notes = [n for n in note_service.list_page_notes(page.id)]
    assert notes[0].note.importance == "secondary"


def test_create_save_button_styles(tmp_path, monkeypatch) -> None:
    """UI-AMEND-02: 页面级保存按钮 primary；文档级保持 secondary 默认。"""
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    page_save = _button(app, f"note_create_page_{page.id}_save")
    document_save = _button(app, f"note_create_document_{document_id}_save")
    assert page_save.proto.type == "primary"
    assert document_save.proto.type == "secondary"


def test_create_text_selection_with_level_and_reset(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("阀体").run()
    app.text_area(key=f"note_text_create_personal_{page.id}").input("关键部位").run()
    _selectbox(app, f"note_text_create_imp_{page.id}").set_value(
        NoteImportance.PRIMARY
    ).run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    notes = note_service.list_page_notes(page.id)
    assert notes[0].note.importance == "primary"
    assert _selectbox(app, f"note_text_create_imp_{page.id}").value == NoteImportance.NORMAL


def test_create_image_region_with_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _mock_component(monkeypatch, _drag())
    app.run(timeout=25)
    _button(app, f"note_image_create_start_{page.id}").click().run()
    app.text_area(key=f"note_image_create_personal_{page.id}").input("重点区域").run()
    _selectbox(app, f"note_image_create_imp_{page.id}").set_value(
        NoteImportance.PRIMARY
    ).run()
    _button(app, f"note_image_create_save_{page.id}").click().run()
    notes = note_service.list_page_notes(page.id)
    assert notes[0].note.importance == "primary"


def test_create_failure_keeps_selection(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    monkeypatch.setattr(
        NoteService,
        "create_page_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("保存笔记失败")),
    )
    app.text_area(key=f"note_create_page_{page.id}").input("会失败的草稿").run()
    _selectbox(app, f"note_create_page_{page.id}_imp").set_value(
        NoteImportance.PRIMARY
    ).run()
    _button(app, f"note_create_page_{page.id}_save").click().run()
    assert any("保存失败" in error.value for error in app.error)
    assert _selectbox(app, f"note_create_page_{page.id}_imp").value == (
        NoteImportance.PRIMARY
    )
    assert app.text_area(key=f"note_create_page_{page.id}").value == "会失败的草稿"


# --- B. EDIT ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [("primary", NoteImportance.PRIMARY), ("secondary", NoteImportance.SECONDARY),
     ("normal", NoteImportance.NORMAL)],
)
def test_edit_selector_initialized_from_note(tmp_path, monkeypatch, level, expected) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "正文", importance=level).note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{note_id}").click().run()
    assert _selectbox(app, f"note_edit_imp_{note_id}").value == expected


def test_edit_text_only_preserves_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "原文", importance="primary").note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_edit_input_{note_id}").input("只改正文").run()
    _button(app, f"note_edit_save_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.personal_note == "只改正文"
    assert note.importance == "primary"


def test_edit_level_only_and_both(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "原文", importance="normal").note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{note_id}").click().run()
    # 无变化 → 无 dirty 提示
    assert not any("有未保存修改" in value for value in _warnings(app))
    _selectbox(app, f"note_edit_imp_{note_id}").set_value(NoteImportance.PRIMARY).run()
    assert any("有未保存修改" in value for value in _warnings(app))
    app.text_area(key=f"note_edit_input_{note_id}").input("正文也改").run()
    _button(app, f"note_edit_save_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.personal_note == "正文也改"
    assert note.importance == "primary"
    # 再改回一般
    _button(app, f"note_edit_open_{note_id}").click().run()
    _selectbox(app, f"note_edit_imp_{note_id}").set_value(NoteImportance.NORMAL).run()
    _button(app, f"note_edit_save_{note_id}").click().run()
    assert note_service.get_note(note_id).note.importance == "normal"


def test_edit_text_selection_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id, level="secondary")
    app.run(timeout=25)
    _button(app, f"note_text_edit_open_{note_id}").click().run()
    assert _selectbox(app, f"note_text_edit_imp_{note_id}").value == (
        NoteImportance.SECONDARY
    )
    _selectbox(app, f"note_text_edit_imp_{note_id}").set_value(
        NoteImportance.PRIMARY
    ).run()
    _button(app, f"note_text_edit_save_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.importance == "primary"
    assert note.source_excerpt_snapshot == "阀体"  # 锚点不受影响


def test_edit_image_region_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_image_region_note(
        page.id, 10, 20, 300, 400, "区域", importance="secondary"
    ).note.id
    app.run(timeout=25)
    _button(app, f"note_image_edit_open_{note_id}").click().run()
    assert _selectbox(app, f"note_image_edit_imp_{note_id}").value == (
        NoteImportance.SECONDARY
    )
    _selectbox(app, f"note_image_edit_imp_{note_id}").set_value(
        NoteImportance.NORMAL
    ).run()
    _button(app, f"note_image_edit_save_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.importance == "normal"
    assert (note.region_x0, note.region_x1) == (10, 300)


def test_edit_error_keeps_state(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "原文", importance="primary").note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{note_id}").click().run()
    _selectbox(app, f"note_edit_imp_{note_id}").set_value(NoteImportance.NORMAL).run()
    monkeypatch.setattr(
        NoteService,
        "update_page_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_edit_save_{note_id}").click().run()
    assert any("保存失败" in error.value for error in app.error)
    note = note_service.get_note(note_id).note
    assert note.importance == "primary"
    assert note.personal_note == "原文"


# --- C. BADGE --------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "label", "color"),
    [("primary", "重点", "#c0392b"), ("secondary", "次重点", "#2563eb"),
     ("normal", "一般", "#000000")],
)
def test_badge_label_and_default_color(tmp_path, monkeypatch, level, label, color) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_service.create_page_note(page.id, "正文", importance=level)
    app.run(timeout=25)
    assert any(
        label in value and color in value and ("#1a1a1a" in value or "#ffffff" in value)
        for value in _markdowns(app)
    )


def test_badge_follows_preference_colors(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_service.create_page_note(page.id, "正文", importance="primary")
    note_service.update_display_preferences("#ffffff", "#000000", "#dddddd")
    app.run(timeout=25)
    # 极亮背景 → 深色前景
    assert any(
        "重点" in value and "#ffffff" in value and "#1a1a1a" in value
        for value in _markdowns(app)
    )


def test_badge_dark_background_light_foreground(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_service.create_page_note(page.id, "正文", importance="secondary")
    note_service.update_display_preferences("#c0392b", "#000000", "#5a6570")
    app.run(timeout=25)
    # 极暗背景 → 浅色前景
    assert any(
        "次重点" in value and "#000000" in value and "color:#ffffff" in value
        for value in _markdowns(app)
    )


def test_badge_renders_with_missing_preference_row(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_service.create_page_note(page.id, "正文", importance="primary")
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("DELETE FROM note_display_preferences WHERE id = 1")
        connection.commit()
    app.run(timeout=25)
    assert any("重点" in value for value in _markdowns(app))
    assert not app.exception


def test_unknown_importance_shows_explicit_error_not_normal(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "正文", importance="normal").note.id
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE notes SET importance = 'magenta' WHERE id = ?", (note_id,))
        connection.commit()
    app.run(timeout=25)
    assert any("重要程度数据异常" in error.value for error in app.error)
    assert not any("magenta" in value and "一般" in value for value in _markdowns(app))


# --- D. STATE ---------------------------------------------------------------


def test_two_notes_edit_selectors_are_independent(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    first = note_service.create_page_note(page.id, "甲", importance="primary").note.id
    second = note_service.create_page_note(page.id, "乙", importance="normal").note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{first}").click().run()
    _button(app, f"note_edit_open_{second}").click().run()
    assert _selectbox(app, f"note_edit_imp_{first}").value == NoteImportance.PRIMARY
    assert _selectbox(app, f"note_edit_imp_{second}").value == NoteImportance.NORMAL


def test_delete_cleans_edit_selector_state(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "甲", importance="primary").note.id
    app.run(timeout=25)
    _button(app, f"note_edit_open_{note_id}").click().run()
    _selectbox(app, f"note_edit_imp_{note_id}").set_value(NoteImportance.SECONDARY).run()
    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    _button(app, f"note_delete_{note_id}").click().run()
    assert note_service.list_page_notes(page.id) == []
    assert f"note_edit_imp_{note_id}" not in {
        element.key for element in app.selectbox
    }


# --- E. REBIND regression ----------------------------------------------------


def test_rebind_preview_and_execute_preserve_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id, level="primary")
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert note_service.get_note(note_id).note.importance == "primary"
    app.checkbox(key=f"note_text_rebind_confirm_{note_id}").check().run()
    _button(app, f"note_text_rebind_apply_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.importance == "primary"
    assert note.source_excerpt_snapshot == "回路"


def test_stale_preview_still_discarded_with_level_ui_present(tmp_path, monkeypatch) -> None:
    """等级控件存在不改变 rebind 的 stale-preview 作废语义。"""
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id, level="secondary")
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("回路" in info.value for info in app.info)
    app.text_area(key=f"note_text_rebind_input_{note_id}").input("压力").run()
    assert not [
        button
        for button in app.button
        if button.key == f"note_text_rebind_apply_{note_id}"
    ]
    assert note_service.get_note(note_id).note.importance == "secondary"


# --- F. REFRAME regression ----------------------------------------------------


def test_reframe_preserves_level(tmp_path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_image_region_note(
        page.id, 10, 20, 300, 400, "区域", importance="secondary"
    ).note.id
    _mock_component(monkeypatch, _drag(100, 200, 500, 700))
    app.run(timeout=25)
    _button(app, f"note_image_rebind_start_{note_id}").click().run()
    app.checkbox(key=f"note_image_rebind_confirm_{note_id}").check().run()
    _button(app, f"note_image_rebind_apply_{note_id}").click().run()
    note = note_service.get_note(note_id).note
    assert note.importance == "secondary"
    assert (note.region_x0, note.region_y0) != (10, 20)


# --- G. DELETE regression ------------------------------------------------------


@pytest.mark.parametrize("level", ["primary", "secondary", "normal"])
def test_delete_semantics_identical_across_levels(tmp_path, monkeypatch, level) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = note_service.create_page_note(page.id, "正文", importance=level).note.id
    keeper_id = note_service.create_page_note(page.id, "保留", importance="normal").note.id
    app.run(timeout=25)

    delete_button = _button(app, f"note_delete_{note_id}")
    assert delete_button.disabled
    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    _button(app, f"note_delete_{note_id}").click().run()
    remaining = {view.note.id for view in note_service.list_page_notes(page.id)}
    assert remaining == {keeper_id}
