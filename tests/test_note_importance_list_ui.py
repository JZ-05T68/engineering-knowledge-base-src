"""v0.3.1 note list importance filter and display settings tests (AppTest).

Covers the frozen Phase 4 contract: single-dimension level filter, count/list
consistency, pagination clamp, list badges, and the single editable
「显示设置」 entry. Temporary databases and synthetic PNGs only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.models import NoteImportance, NoteType
from src.note_service import NoteService, NoteWriteError

LIST_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("6_*.py")))


def _build_app(tmp_path: Path, monkeypatch, *, extra_normal: int = 0):
    """Two documents; page 1 of doc1 carries primary/secondary/normal notes."""
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
    service.create_page_note(page1.id, "重点页面笔记", importance="primary")
    service.create_text_selection_note(page1.id, "阀体", "次重点选区", importance="secondary")
    service.create_image_region_note(page1.id, 10, 20, 300, 400, "一般区域", importance="normal")
    service.create_document_note(doc2.id, "乙文档笔记", importance="normal")
    for index in range(extra_normal):
        service.create_page_note(page1.id, f"分页笔记 {index + 1:02d}", importance="normal")
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    app = AppTest.from_file(LIST_PAGE).run(timeout=25)
    return app, database, service, doc1, doc2, page1


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _select(app: AppTest, label: str):
    return next(sb for sb in app.selectbox if sb.label == label)


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]


def _markdowns(app: AppTest) -> list[str]:
    return [markdown.value for markdown in app.markdown]


def _notes_rows(database: Database) -> list[tuple]:
    with sqlite3.connect(database.database_path) as connection:
        return connection.execute("SELECT * FROM notes ORDER BY id").fetchall()


# --- A. FILTER ----------------------------------------------------------


def test_filter_defaults_to_all(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    assert _select(app, "等级筛选").value == "全部等级"
    assert any("共 4 条" in value for value in _captions(app))


@pytest.mark.parametrize(
    ("level", "label", "expect"),
    [(NoteImportance.PRIMARY, "重点页面笔记", "共 1 条"),
     (NoteImportance.SECONDARY, "次重点选区", "共 1 条"),
     (NoteImportance.NORMAL, "一般区域", "共 2 条")],
)
def test_filter_by_level(tmp_path, monkeypatch, level, label, expect) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    _select(app, "等级筛选").set_value(level).run()
    captions = _captions(app)
    assert any(expect in value for value in captions)
    assert any(label in value for value in _markdowns(app))


def test_filter_back_to_all_and_empty_state(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    _select(app, "等级筛选").set_value(NoteImportance.PRIMARY).run()
    assert any("共 1 条" in value for value in _captions(app))
    _select(app, "等级筛选").set_value("全部等级").run()
    assert any("共 4 条" in value for value in _captions(app))
    # 非匹配组合 → 空态
    _select(app, "类型筛选").set_value(NoteType.IMAGE_REGION).run()
    _select(app, "等级筛选").set_value(NoteImportance.PRIMARY).run()
    assert any("当前筛选条件下没有结构化笔记" in info.value for info in app.info)


def test_count_and_list_consistent_and_order_kept(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch, extra_normal=3)
    _select(app, "等级筛选").set_value(NoteImportance.NORMAL).run()
    captions = _captions(app)
    assert any("共 5 条" in value for value in captions)
    expected = [
        view.note.personal_note
        for view in service.list_note_summaries(importance="normal", limit=20)
    ]
    shown = [value for value in _markdowns(app) if value in expected]
    assert shown[: len(expected)] == expected  # updated_at/id DESC 顺序不变


# --- B. FILTER COMBINATION ----------------------------------------------


def test_document_and_type_combine_with_level(tmp_path, monkeypatch) -> None:
    app, _, _, doc1, doc2, _ = _build_app(tmp_path, monkeypatch)
    _select(app, "文档筛选").set_value(doc2.id).run()
    _select(app, "等级筛选").set_value(NoteImportance.PRIMARY).run()
    assert any("当前筛选条件下没有结构化笔记" in info.value for info in app.info)
    _select(app, "等级筛选").set_value(NoteImportance.NORMAL).run()
    assert any("共 1 条" in value for value in _captions(app))
    _select(app, "文档筛选").set_value(doc1.id).run()
    _select(app, "类型筛选").set_value(NoteType.TEXT_SELECTION).run()
    _select(app, "等级筛选").set_value(NoteImportance.SECONDARY).run()
    assert any("共 1 条" in value for value in _captions(app))


# --- C. PAGINATION -------------------------------------------------------


def test_filter_shrinks_results_and_page_resets(tmp_path, monkeypatch) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch, extra_normal=22)
    # 全部 26 条 → 2 页；先到第 2 页
    _button(app, "note_list_next").click().run()
    assert any("第 2 / 2 页" in value for value in _captions(app))
    # 缩小筛选到重点（1 条）→ 安全回到有效页
    _select(app, "等级筛选").set_value(NoteImportance.PRIMARY).run()
    captions = _captions(app)
    assert any("第 1 / 1 页" in value for value in captions)
    assert any("共 1 条" in value for value in captions)
    assert any("重点页面笔记" in value for value in _markdowns(app))
    # 解除筛选后分页恢复
    _select(app, "等级筛选").set_value("全部等级").run()
    assert any("第 1 / 2 页" in value for value in _captions(app))
    assert any("共 26 条" in value for value in _captions(app))


# --- D. BADGE ------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "color"),
    [("重点", "#c0392b"), ("次重点", "#b8860b"), ("一般", "#5a6570")],
)
def test_list_badge_labels_and_colors(tmp_path, monkeypatch, label, color) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    assert any(
        label in value and color in value and ("#1a1a1a" in value or "#ffffff" in value)
        for value in _markdowns(app)
    )


def test_badge_extreme_backgrounds(tmp_path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    service.update_display_preferences("#ffffff", "#000000", "#5a6570")
    app.run(timeout=25)
    assert any(
        "重点" in value and "#ffffff" in value and "#1a1a1a" in value
        for value in _markdowns(app)
    )
    assert any(
        "次重点" in value and "#000000" in value and "color:#ffffff" in value
        for value in _markdowns(app)
    )


def test_badge_missing_preference_row_fallback(tmp_path, monkeypatch) -> None:
    app, database, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("DELETE FROM note_display_preferences WHERE id = 1")
        connection.commit()
    app.run(timeout=25)
    assert not app.exception
    assert any("重点" in value for value in _markdowns(app))


def test_unknown_importance_explicit_error_not_normal(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE notes SET importance = 'magenta' WHERE id = 1")
        connection.commit()
    app.run(timeout=25)
    assert any("重要程度数据异常" in error.value for error in app.error)


def test_preferences_queried_once_per_render(tmp_path, monkeypatch) -> None:
    database_calls = {"count": 0}
    original = NoteService.get_display_preferences

    def counting(self):
        database_calls["count"] += 1
        return original(self)

    monkeypatch.setattr(NoteService, "get_display_preferences", counting)
    _build_app(tmp_path, monkeypatch)
    assert database_calls["count"] == 1


# --- E. DISPLAY SETTINGS ---------------------------------------------------


def test_settings_load_current_colors_and_save(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    pickers = {picker.key: picker.value for picker in app.color_picker}
    assert pickers["note_list_pref_color_primary"] == "#c0392b"
    assert pickers["note_list_pref_color_secondary"] == "#b8860b"
    assert pickers["note_list_pref_color_normal"] == "#5a6570"

    before = service.get_display_preferences()
    app.color_picker(key="note_list_pref_color_primary").set_value("#102030").run()
    _button(app, "note_list_pref_save").click().run()
    updated = service.get_display_preferences()
    assert updated.color_primary == "#102030"
    assert updated.color_secondary == "#b8860b"
    assert updated.updated_at >= before.updated_at
    assert any("配色已保存" in success.value for success in app.success)
    # rerun 后控件展示数据库 canonical 值，badge 立即生效
    assert app.color_picker(key="note_list_pref_color_primary").value == "#102030"
    assert any("重点" in value and "#102030" in value for value in _markdowns(app))


def test_settings_save_failure_no_fake_success(tmp_path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        NoteService,
        "update_display_preferences",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("保存显示偏好失败")),
    )
    app.color_picker(key="note_list_pref_color_primary").set_value("#102030").run()
    _button(app, "note_list_pref_save").click().run()
    assert any("保存失败" in error.value or "保存显示偏好失败" in error.value
               for error in app.error)
    assert not any("配色已保存" in success.value for success in app.success)
    assert service.get_display_preferences().color_primary == "#c0392b"


def test_settings_change_does_not_touch_notes(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    before = _notes_rows(database)
    app.color_picker(key="note_list_pref_color_primary").set_value("#102030").run()
    _button(app, "note_list_pref_save").click().run()
    _button(app, "note_list_pref_reset").click().run()
    after = _notes_rows(database)
    assert after == before  # notes 全表逐行一致（含 importance 与 updated_at）


# --- F. RESET ----------------------------------------------------------


def test_reset_restores_defaults_and_keeps_filter(tmp_path, monkeypatch) -> None:
    app, _, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    service.update_display_preferences("#000001", "#000002", "#000003")
    _select(app, "等级筛选").set_value(NoteImportance.PRIMARY).run()
    _button(app, "note_list_pref_reset").click().run()
    preferences = service.get_display_preferences()
    assert (preferences.color_primary, preferences.color_secondary) == (
        "#c0392b",
        "#b8860b",
    )
    assert any("配色已恢复默认" in success.value for success in app.success)
    # badge 回到默认配色；等级筛选保持不清空
    assert any("重点" in value and "#c0392b" in value for value in _markdowns(app))
    assert _select(app, "等级筛选").value == NoteImportance.PRIMARY


# --- G. STATE ----------------------------------------------------------


def test_rerun_reads_database_as_authoritative(tmp_path, monkeypatch) -> None:
    app, database, service, _, _, _ = _build_app(tmp_path, monkeypatch)
    service.update_display_preferences("#112233", "#445566", "#778899")
    fresh = AppTest.from_file(LIST_PAGE).run(timeout=25)
    assert fresh.color_picker(key="note_list_pref_color_primary").value == "#112233"
    assert any("重点" in value and "#112233" in value for value in _markdowns(fresh))
