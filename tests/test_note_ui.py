"""Reader-page structured-notes tab tests (AppTest).

Fixtures use temporary databases and synthetic PNGs only. Production data and
port 8501 are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.note_service import NoteNotFoundError, NoteService, NoteWriteError

READER = str(next((Path(__file__).parents[1] / "pages").glob("2_*.py")))


def _build_reader(tmp_path: Path, monkeypatch) -> tuple[AppTest, Database, NoteService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    document = database.create_document(
        title="笔记界面测试",
        filename="notes-ui.pdf",
        source_path=raw_dir / "notes-ui.pdf",
        sha256="6" * 64,
    )
    for page_number in (1, 2):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"第 {page_number} 页 阀体 回路",
        )
    database.update_document_page_count(document.id, 2)
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
    assert matches, f"找不到按钮 {key}；现有：{[b.key for b in app.button][:12]}"
    return matches[0]


def _button_label(app: AppTest, label: str):
    matches = [button for button in app.button if button.label == label]
    assert matches, f"找不到按钮 {label}"
    return matches[0]


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]


def _markdowns(app: AppTest) -> list[str]:
    return [markdown.value for markdown in app.markdown]


def _warnings(app: AppTest) -> list[str]:
    return [warning.value for warning in app.warning]


def _errors(app: AppTest) -> list[str]:
    return [error.value for error in app.error]


# --- A. tab structure & empty states ----------------------------------------


def test_tab_structure_and_empty_states(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    tab_labels = [tab.label for tab in app.tabs]
    assert ["整理稿-编辑", "整理稿-预览", "结构化笔记"] == tab_labels
    expander_labels = [expander.label for expander in app.expander]
    assert "文档级笔记" in expander_labels
    subheaders = [header.value for header in app.subheader]
    assert "本页笔记" in subheaders
    assert "这份文档还没有文档级笔记。" in _captions(app)
    assert "当前页面还没有页面级笔记。" in _captions(app)


# --- B. creation --------------------------------------------------------------


def test_create_document_note_success_clears_input(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    app.text_area(key=f"note_create_document_{document_id}").input("  整本手册很好  ").run()
    _button(app, f"note_create_document_{document_id}_save").click().run()
    assert not app.exception
    notes = NoteService(database).list_document_notes(document_id)
    assert [view.note.personal_note for view in notes] == ["整本手册很好"]
    assert app.text_area(key=f"note_create_document_{document_id}").value == ""
    assert any("文档级笔记已保存" in success.value for success in app.success)


def test_create_page_note_success(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    app.text_area(key=f"note_create_page_{page.id}").input("本页第一点").run()
    _button(app, f"note_create_page_{page.id}_save").click().run()
    assert [view.note.personal_note for view in note_service.list_page_notes(page.id)] == [
        "本页第一点"
    ]
    assert app.text_area(key=f"note_create_page_{page.id}").value == ""


def test_create_rejects_empty_input(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    app.text_area(key=f"note_create_document_{document_id}").input("   ").run()
    _button(app, f"note_create_document_{document_id}_save").click().run()
    assert "个人笔记不能为空" in _warnings(app)
    assert NoteService(database).list_document_notes(document_id) == []


def test_create_failure_keeps_input_and_shows_error(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise NoteWriteError("保存笔记失败，请重试")

    monkeypatch.setattr(NoteService, "create_document_note", fail)
    app.text_area(key=f"note_create_document_{document_id}").input("不要丢").run()
    _button(app, f"note_create_document_{document_id}_save").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert app.text_area(key=f"note_create_document_{document_id}").value == "不要丢"


def test_create_inputs_are_isolated(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    app.text_area(key=f"note_create_document_{document_id}").input("文档草稿").run()
    app.text_area(key=f"note_create_page_{page.id}").input("页面草稿").run()
    assert app.text_area(key=f"note_create_document_{document_id}").value == "文档草稿"
    assert app.text_area(key=f"note_create_page_{page.id}").value == "页面草稿"


# --- C. editing -----------------------------------------------------------------


def _seed_two_page_notes(note_service: NoteService, page_id: int) -> tuple[int, int]:
    first = note_service.create_page_note(page_id, "第一条").note.id
    second = note_service.create_page_note(page_id, "第二条").note.id
    return first, second


def test_edit_page_note_dirty_flag_and_save(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    note_id, _ = _seed_two_page_notes(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_edit_open_{note_id}").click().run()
    editor = app.text_area(key=f"note_edit_input_{note_id}")
    assert editor.value == "第一条"
    editor.input("第一条（修订）").run()
    assert any("有未保存修改" in value for value in _warnings(app))

    _button(app, f"note_edit_save_{note_id}").click().run()
    updated = note_service.get_note(note_id).note
    assert updated.personal_note == "第一条（修订）"
    assert f"note_edit_input_{note_id}" not in {
        element.key for element in app.text_area
    }


def test_edit_failure_keeps_draft(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    note_id, _ = _seed_two_page_notes(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_edit_input_{note_id}").input("失败草稿").run()
    monkeypatch.setattr(
        NoteService,
        "update_page_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_edit_save_{note_id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert note_service.get_note(note_id).note.personal_note == "第一条"


def test_edit_states_are_isolated_per_note(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    first, second = _seed_two_page_notes(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_edit_open_{first}").click().run()
    app.text_area(key=f"note_edit_input_{first}").input("只改第一条").run()
    assert app.text_area(key=f"note_edit_input_{first}").value == "只改第一条"
    assert note_service.get_note(second).note.personal_note == "第二条"
    assert f"note_edit_input_{second}" not in {element.key for element in app.text_area}


# --- D. deletion ------------------------------------------------------------------


def test_delete_requires_confirmation(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    note_id, _ = _seed_two_page_notes(note_service, page.id)
    app.run(timeout=25)

    delete_button = _button(app, f"note_delete_{note_id}")
    assert delete_button.disabled
    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    assert not _button(app, f"note_delete_{note_id}").disabled
    _button(app, f"note_delete_{note_id}").click().run()
    with pytest.raises(NoteNotFoundError):
        note_service.get_note(note_id)
    assert f"note_delete_confirm_{note_id}" not in {
        element.key for element in app.checkbox
    }


def test_delete_failure_shows_error_and_keeps_note(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    note_id, second = _seed_two_page_notes(note_service, page.id)
    app.run(timeout=25)

    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    monkeypatch.setattr(
        NoteService,
        "delete_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("删除笔记失败")),
    )
    _button(app, f"note_delete_{note_id}").click().run()
    assert any("删除失败" in value for value in _errors(app))
    assert note_service.get_note(note_id).note.personal_note == "第一条"
    assert note_service.get_note(second).note.personal_note == "第二条"


# --- E. type isolation & defensive rendering --------------------------------------


def test_anchor_types_render_readonly_and_sections_stay_separate(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    note_service.create_document_note(document_id, "文档级内容")
    note_service.create_page_note(page.id, "页面级内容")
    note_service.create_text_selection_note(page.id, "阀体", "选区内容")
    note_service.create_image_region_note(page.id, 10, 20, 300, 400, "区域内容")
    app.run(timeout=25)
    assert not app.exception

    captions = _captions(app)
    markdowns = _markdowns(app)
    assert any("文字选区笔记" in value for value in captions)
    assert any("图片区域笔记" in value for value in captions)
    assert any("区域：(10, 20) - (300, 400)" in value for value in captions)
    assert "页面级内容" in markdowns and "文档级内容" in markdowns
    # 可编辑入口 = 文档级 1 + 页面级 1 + 选区 1 + 区域 1（四类均已开放）
    edit_buttons = [b for b in app.button if b.label == "编辑"]
    delete_buttons = [b for b in app.button if b.label == "永久删除这条笔记"]
    assert len(edit_buttons) == 4
    assert len(delete_buttons) == 4


# --- F. regression: markdown flow & review status ---------------------------------


def test_markdown_flow_unaffected_and_note_save_keeps_page_state(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = database.get_page_by_number(document_id, 1)
    assert page.markdown_content == ""

    # 结构化笔记保存不改变 review_status 与 markdown_content
    app.text_area(key=f"note_create_page_{page.id}").input("结构化笔记").run()
    _button(app, f"note_create_page_{page.id}_save").click().run()
    after_note = database.get_page(page.id)
    assert after_note.status == page.status
    assert after_note.markdown_content == ""

    # 整理稿保存仍走原逻辑
    editor_key = f"markdown_editor_{page.id}_current"
    app.text_area(key=editor_key).input("# 整理稿内容").run()
    _button_label(app, "保存笔记").click().run()
    after_markdown = database.get_page(page.id)
    assert after_markdown.markdown_content == "# 整理稿内容"
    # 结构化笔记行不受影响
    notes = note_service.list_page_notes(page.id)
    assert [view.note.personal_note for view in notes] == ["结构化笔记"]


# --- G. query discipline -----------------------------------------------------------


def test_list_queries_are_bounded(tmp_path: Path, monkeypatch) -> None:
    calls = {"list_document_notes": 0, "list_page_notes": 0, "get_note": 0, "list_notes": 0}
    original_doc = NoteService.list_document_notes
    original_page = NoteService.list_page_notes
    original_get = NoteService.get_note
    original_list = NoteService.list_notes

    def counted(name, original):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        NoteService, "list_document_notes", counted("list_document_notes", original_doc)
    )
    monkeypatch.setattr(NoteService, "list_page_notes", counted("list_page_notes", original_page))
    monkeypatch.setattr(NoteService, "get_note", counted("get_note", original_get))
    monkeypatch.setattr(NoteService, "list_notes", counted("list_notes", original_list))

    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    assert calls["list_document_notes"] == 1
    assert calls["list_page_notes"] == 1
    assert calls["get_note"] == 0
    assert calls["list_notes"] == 0
