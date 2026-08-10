"""Reader text-selection notes interaction tests (AppTest).

Fixtures use temporary databases and synthetic PNGs only. Production data and
port 8501 are never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import NoteSourceStatus
from src.note_service import NoteNotFoundError, NoteService, NoteWriteError

READER = str(next((Path(__file__).parents[1] / "pages").glob("2_*.py")))
PAGE_TEXT = "液压系统 阀体 回路 压力"


def _build_reader(tmp_path: Path, monkeypatch) -> tuple[AppTest, Database, NoteService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    document = database.create_document(
        title="选区界面测试",
        filename="selection-ui.pdf",
        source_path=raw_dir / "selection-ui.pdf",
        sha256="9" * 64,
    )
    for page_number in (1, 2):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=PAGE_TEXT if page_number == 1 else "另一页内容",
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
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _warnings(app: AppTest) -> list[str]:
    return [warning.value for warning in app.warning]


def _errors(app: AppTest) -> list[str]:
    return [error.value for error in app.error]


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]


def _set_page_text(database: Database, page_id: int, extracted: str, ocr: str = "") -> None:
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET extracted_text = ?, ocr_text = ? WHERE id = ?",
            (extracted, ocr, page_id),
        )
        connection.commit()


def _page1(database: Database, document_id: int):
    return database.get_page_by_number(document_id, 1)


# --- A. source display -------------------------------------------------------


def test_source_display_pdf_text_priority(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    assert "来源：PDF 文本层" in _captions(app)
    code_values = [code.value for code in app.code]
    assert any(PAGE_TEXT in value for value in code_values)


def test_source_display_ocr_fallback(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _set_page_text(database, page.id, "", "仅 OCR 内容")
    app.run(timeout=25)
    assert "来源：OCR 初稿" in _captions(app)


def test_create_disabled_without_text_source(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _set_page_text(database, page.id, "", "")
    app.run(timeout=25)
    assert any("没有可用文字来源" in value for value in _warnings(app))
    assert f"note_text_create_excerpt_{page.id}" not in {
        element.key for element in app.text_area
    }


# --- B. creation --------------------------------------------------------------


def test_create_selection_note_success_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("阀体").run()
    app.text_area(key=f"note_text_create_personal_{page.id}").input("关注这里").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert not app.exception
    views = note_service.list_page_notes(page.id)
    assert len(views) == 1
    note = views[0].note
    assert note.source_excerpt_snapshot == "阀体"
    assert note.user_excerpt == "阀体"
    assert note.personal_note == "关注这里"
    assert app.text_area(key=f"note_text_create_excerpt_{page.id}").value == ""
    assert any("文字选区笔记已保存" in success.value for success in app.success)


def test_create_selection_note_custom_user_excerpt(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("回路").run()
    app.text_area(key=f"note_text_create_user_{page.id}").input("回路（关键）").run()
    app.text_area(key=f"note_text_create_personal_{page.id}").input("笔记").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert note_service.list_page_notes(page.id)[0].note.user_excerpt == "回路（关键）"


# --- C. creation failures ------------------------------------------------------


def test_create_rejects_empty_fields(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert "原文选段不能为空" in _warnings(app)

    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("阀体").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert "个人笔记不能为空" in _warnings(app)
    assert note_service.list_page_notes(page.id) == []


def test_create_zero_and_multiple_hits(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("没有这段").run()
    app.text_area(key=f"note_text_create_personal_{page.id}").input("笔记").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert any("没有在当前文字来源中找到" in value for value in _warnings(app))

    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("压").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert any("出现多次" in value for value in _warnings(app))
    assert note_service.list_page_notes(page.id) == []


def test_create_failure_keeps_inputs(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)

    def fail(*args, **kwargs):
        raise NoteWriteError("保存笔记失败，请重试")

    monkeypatch.setattr(NoteService, "create_text_selection_note", fail)
    app.text_area(key=f"note_text_create_excerpt_{page.id}").input("阀体").run()
    app.text_area(key=f"note_text_create_personal_{page.id}").input("别丢").run()
    _button(app, f"note_text_create_save_{page.id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert app.text_area(key=f"note_text_create_excerpt_{page.id}").value == "阀体"
    assert app.text_area(key=f"note_text_create_personal_{page.id}").value == "别丢"


# --- D. display & status ---------------------------------------------------------


def _seed_selection(note_service: NoteService, page_id: int) -> int:
    return note_service.create_text_selection_note(
        page_id, "阀体", "个人判断", user_excerpt="阀体摘录"
    ).note.id


def test_display_sections_and_valid_status(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)
    captions = _captions(app)
    assert any(f"文字选区笔记 #{note_id}" in value for value in captions)
    assert any("来源有效" in value for value in captions)
    assert any("原文快照（只读）" in value for value in captions)
    assert any("原文位置：5 – 7" in value for value in captions)
    markdowns = [markdown.value for markdown in app.markdown]
    assert "> 阀体" in markdowns
    assert "阀体摘录" in markdowns
    assert "个人判断" in markdowns


def test_changed_status_warning_and_content_still_visible(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _seed_selection(note_service, page.id)
    _set_page_text(database, page.id, "完全不同的文本")
    app.run(timeout=25)
    assert any("无法自动重新定位" in value for value in _warnings(app))
    markdowns = [markdown.value for markdown in app.markdown]
    assert "> 阀体" in markdowns and "个人判断" in markdowns


def test_missing_status_warning(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _seed_selection(note_service, page.id)
    _set_page_text(database, page.id, "")
    app.run(timeout=25)
    assert any("已经不存在" in value for value in _warnings(app))


# --- E. content editing -----------------------------------------------------------


def test_edit_selection_content_preserves_anchor(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_text_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_text_edit_excerpt_{note_id}").input("修订摘录").run()
    assert any("有未保存修改" in value for value in _warnings(app))
    _button(app, f"note_text_edit_save_{note_id}").click().run()

    updated = note_service.get_note(note_id).note
    assert updated.user_excerpt == "修订摘录"
    assert updated.personal_note == "个人判断"
    assert updated.source_excerpt_snapshot == "阀体"
    assert (updated.selection_start, updated.selection_end) == (5, 7)
    assert updated.source_page_text_sha256 is not None


def test_edit_failure_keeps_draft(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_text_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_text_edit_personal_{note_id}").input("失败草稿").run()
    monkeypatch.setattr(
        NoteService,
        "update_text_selection_content",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_text_edit_save_{note_id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert note_service.get_note(note_id).note.personal_note == "个人判断"


# --- F/G. rebind preview & apply ----------------------------------------------------


def test_rebind_preview_is_readonly_and_shows_summary(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    before = note_service.get_note(note_id).note
    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    after = note_service.get_note(note_id).note
    assert after == before  # 预览只读

    infos = [info.value for info in app.info]
    assert any("回路" in value and "阀体" in value for value in infos)
    apply_button = _button(app, f"note_text_rebind_apply_{note_id}")
    assert apply_button.disabled


def test_rebind_preview_failures_show_no_confirm(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("没有这段").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("没有在当前文字来源中找到" in value for value in _warnings(app))
    assert f"note_text_rebind_confirm_{note_id}" not in {
        element.key for element in app.checkbox
    }

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("压").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("出现多次" in value for value in _warnings(app))


def test_rebind_apply_updates_anchor_and_preserves_personal(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    before = note_service.get_note(note_id).note
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    app.checkbox(key=f"note_text_rebind_confirm_{note_id}").check().run()
    assert not _button(app, f"note_text_rebind_apply_{note_id}").disabled
    _button(app, f"note_text_rebind_apply_{note_id}").click().run()

    rebound = note_service.get_note(note_id).note
    assert rebound.id == before.id
    assert rebound.created_at == before.created_at
    assert rebound.personal_note == "个人判断"
    assert rebound.user_excerpt == "回路"
    assert rebound.source_excerpt_snapshot == "回路"
    assert (rebound.selection_start, rebound.selection_end) == (8, 10)
    assert app.text_area(key=f"note_text_rebind_input_{note_id}").value == ""
    assert f"note_text_rebind_preview_{note_id}" not in [
        element.key for element in app.checkbox
    ]
    assert note_service.get_note(note_id).source_status is NoteSourceStatus.VALID


def test_rebind_failure_keeps_original(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    before = note_service.get_note(note_id).note
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    app.checkbox(key=f"note_text_rebind_confirm_{note_id}").check().run()
    monkeypatch.setattr(
        NoteService,
        "rebind_text_selection",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_text_rebind_apply_{note_id}").click().run()
    assert any("重新绑定失败" in value or "保存失败" in value for value in _errors(app))
    assert note_service.get_note(note_id).note == before


def test_rebind_execute_matches_last_confirmed_preview(
    tmp_path: Path, monkeypatch
) -> None:
    """预览 A 后改输入为 B：陈旧预览必须作废，禁止按 A 的确认执行 B。"""
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("回路" in info.value for info in app.info)

    # 修改输入后不重新预览：预览与确认区必须消失
    app.text_area(key=f"note_text_rebind_input_{note_id}").input("压力").run()
    assert any("重新预览" in value for value in _warnings(app))
    assert not any("回路" in info.value for info in app.info)
    assert not [
        button
        for button in app.button
        if button.key == f"note_text_rebind_apply_{note_id}"
    ]

    # 重新预览当前输入后执行，落库内容必须与最后确认的预览一致
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("压力" in info.value for info in app.info)
    app.checkbox(key=f"note_text_rebind_confirm_{note_id}").check().run()
    _button(app, f"note_text_rebind_apply_{note_id}").click().run()
    rebound = note_service.get_note(note_id).note
    assert rebound.source_excerpt_snapshot == "压力"
    assert rebound.user_excerpt == "压力"
    assert rebound.personal_note == "个人判断"


def test_rebind_draft_change_clears_confirmation(tmp_path: Path, monkeypatch) -> None:
    """已勾选的确认 checkbox 在输入变化后必须一并作废。"""
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    app.checkbox(key=f"note_text_rebind_confirm_{note_id}").check().run()
    assert not _button(app, f"note_text_rebind_apply_{note_id}").disabled

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("压力").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert app.checkbox(key=f"note_text_rebind_confirm_{note_id}").value is False
    assert _button(app, f"note_text_rebind_apply_{note_id}").disabled


def test_rebind_failed_preview_discards_previous_preview(
    tmp_path: Path, monkeypatch
) -> None:
    """预览失败不得保留上一次成功的预览与确认入口。"""
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    app.run(timeout=25)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("回路").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("回路" in info.value for info in app.info)

    app.text_area(key=f"note_text_rebind_input_{note_id}").input("没有这段").run()
    _button(app, f"note_text_rebind_preview_btn_{note_id}").click().run()
    assert any("没有在当前文字来源中找到" in value for value in _warnings(app))
    assert not any("回路" in info.value for info in app.info)
    assert f"note_text_rebind_confirm_{note_id}" not in {
        element.key for element in app.checkbox
    }


# --- H. deletion ---------------------------------------------------------------------
def test_delete_selection_note(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_selection(note_service, page.id)
    page_note_id = note_service.create_page_note(page.id, "页面级保留").note.id
    before_page = database.get_page(page.id)
    app.run(timeout=25)

    delete_button = _button(app, f"note_delete_{note_id}")
    assert delete_button.disabled
    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    _button(app, f"note_delete_{note_id}").click().run()

    with pytest.raises(NoteNotFoundError):
        note_service.get_note(note_id)
    assert note_service.get_note(page_note_id).note.personal_note == "页面级保留"
    after_page = database.get_page(page.id)
    assert after_page.extracted_text == before_page.extracted_text
    assert after_page.markdown_content == before_page.markdown_content
    assert after_page.status == before_page.status


# --- I. type isolation ------------------------------------------------------------------


def test_image_region_stays_readonly_and_types_stay_separate(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_service.create_page_note(page.id, "页面级")
    _seed_selection(note_service, page.id)
    note_service.create_image_region_note(page.id, 10, 20, 300, 400, "区域")
    app.run(timeout=25)
    assert not app.exception
    captions = _captions(app)
    assert any("图片区域笔记" in value for value in captions)
    # 四类均已开放：页面级 + 选区 + 区域均有编辑/删除入口
    edit_buttons = [b for b in app.button if b.label == "编辑"]
    delete_buttons = [b for b in app.button if b.label == "永久删除这条笔记"]
    assert len(edit_buttons) == 3
    assert len(delete_buttons) == 3


# --- J. page-switch isolation --------------------------------------------------------------


def test_create_inputs_do_not_leak_across_pages(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page1 = _page1(database, document_id)
    page2 = database.get_page_by_number(document_id, 2)
    app.text_area(key=f"note_text_create_excerpt_{page1.id}").input("第1页草稿").run()

    page_select = next(sb for sb in app.selectbox if sb.label == "页码")
    page_select.select("2").run()
    keys = {element.key for element in app.text_area}
    assert f"note_text_create_excerpt_{page2.id}" in keys
    assert app.text_area(key=f"note_text_create_excerpt_{page2.id}").value == ""
