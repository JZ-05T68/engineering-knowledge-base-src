"""v0.3.1 note importance and display-preference service tests.

Covers the frozen Phase 2 contract (docs/design-v0.3.1.md R1 §12):
create defaults, UPDATE None=preserve, atomic rejection, rebind/reframe
preservation, list filtering and the preference/color/foreground contract.
All fixtures use temporary databases and synthetic PNGs.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.models import NoteDisplayPreferences, NoteType
from src.note_service import (
    NoteService,
    NoteValidationError,
    badge_foreground,
)

TS = "2026-07-30T00:00:00+00:00"
HASH_A = "a" * 64
EXTRACTED = "液压系统 阀体 回路 压力"


def _make_png(path: Path, size: tuple[int, int] = (800, 1200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict:
    database = Database(tmp_path / "knowledge.db")
    service = NoteService(database)
    png = _make_png(tmp_path / "pages" / "page_0001.png")
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO documents(title, filename, source_path, sha256, page_count,"
            " created_at, updated_at) VALUES ('液压手册', 'a.pdf', 'data/raw/a.pdf',"
            " ?, 1, ?, ?)",
            (HASH_A, TS, TS),
        )
        connection.execute(
            "INSERT INTO pages(document_id, page_number, image_path, extracted_text,"
            " ocr_text, markdown_content, markdown_path, status, review_status,"
            " created_at, updated_at)"
            " VALUES (1, 1, ?, ?, '', '# 整理稿', 'data/markdown/1/page_0001.md',"
            " 'text_extracted', 'draft', ?, ?)",
            (str(png), EXTRACTED, TS, TS),
        )
        connection.commit()
    return {"database": database, "service": service, "png": png, "tmp": tmp_path}


def _fail_write_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def failing_connection(self: Database):
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            if connection.total_changes > 0:
                connection.rollback()
                raise sqlite3.OperationalError("simulated commit failure")
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(Database, "_connection", failing_connection)


def _notes_rows(database: Database) -> list[sqlite3.Row]:
    with sqlite3.connect(database.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM notes ORDER BY id").fetchall()


def _seed_leveled(service: NoteService) -> dict[str, int]:
    """One note of each level on page 1 (mixed types)."""
    return {
        "primary": service.create_page_note(1, "重点笔记", importance="primary").note.id,
        "secondary": service.create_text_selection_note(
            1, "阀体", "次重点选区", importance="secondary"
        ).note.id,
        "normal": service.create_image_region_note(
            1, 10, 20, 300, 400, "一般区域", importance="normal"
        ).note.id,
    }


# --- A. CREATE（四类参数化） -------------------------------------------------

FOUR_CREATES = {
    "document": lambda s, level: s.create_document_note(1, "文档笔记", importance=level),
    "page": lambda s, level: s.create_page_note(1, "页面笔记", importance=level),
    "text_selection": lambda s, level: s.create_text_selection_note(
        1, "阀体", "选区笔记", importance=level
    ),
    "image_region": lambda s, level: s.create_image_region_note(
        1, 10, 20, 300, 400, "区域笔记", importance=level
    ),
}


@pytest.mark.parametrize("note_type", sorted(FOUR_CREATES))
def test_create_defaults_to_normal(env: dict, note_type: str) -> None:
    service: NoteService = env["service"]
    view = (
        service.create_document_note(1, "文档笔记")
        if note_type == "document"
        else service.create_page_note(1, "页面笔记")
        if note_type == "page"
        else service.create_text_selection_note(1, "阀体", "选区笔记")
        if note_type == "text_selection"
        else service.create_image_region_note(1, 10, 20, 300, 400, "区域笔记")
    )
    assert view.note.importance == "normal"
    # 数据库中的实际值（不是 mapping 层默认）也是 normal
    rows = _notes_rows(env["database"])
    assert rows[-1]["importance"] == "normal"


@pytest.mark.parametrize("note_type", sorted(FOUR_CREATES))
@pytest.mark.parametrize("level", ["primary", "secondary", "normal"])
def test_create_with_explicit_level(env: dict, note_type: str, level: str) -> None:
    view = FOUR_CREATES[note_type](env["service"], level)
    assert view.note.importance == level


@pytest.mark.parametrize("note_type", sorted(FOUR_CREATES))
def test_create_rejects_invalid_level_without_write(env: dict, note_type: str) -> None:
    for bad in ("key", "high", "medium", "PRIMARY", "重点", ""):
        with pytest.raises(NoteValidationError):
            FOUR_CREATES[note_type](env["service"], bad)
    assert _notes_rows(env["database"]) == []


# --- B. UPDATE None=preserve -------------------------------------------------

ALL_UPDATES = {
    "document": lambda s, nid, **kw: s.update_document_note(nid, "新内容", **kw),
    "page": lambda s, nid, **kw: s.update_page_note(nid, "新内容", **kw),
    "text_selection": lambda s, nid, **kw: s.update_text_selection_content(
        nid, personal_note="新内容", **kw
    ),
    "image_region": lambda s, nid, **kw: s.update_image_region_note(nid, "新内容", **kw),
}


def _seed_of_type(service: NoteService, note_type: str, level: str) -> int:
    return FOUR_CREATES[note_type](service, level).note.id


@pytest.mark.parametrize("note_type", sorted(ALL_UPDATES))
@pytest.mark.parametrize("level", ["primary", "secondary"])
def test_legacy_update_preserves_level(env: dict, note_type: str, level: str) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, note_type, level)
    view = ALL_UPDATES[note_type](service, note_id)
    assert view.note.importance == level
    assert view.note.personal_note == "新内容"


@pytest.mark.parametrize("note_type", sorted(ALL_UPDATES))
@pytest.mark.parametrize(
    ("before", "after"),
    [("primary", "secondary"), ("secondary", "normal"), ("normal", "primary")],
)
def test_explicit_level_change(env: dict, note_type: str, before: str, after: str) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, note_type, before)
    view = ALL_UPDATES[note_type](service, note_id, importance=after)
    assert view.note.importance == after


@pytest.mark.parametrize("note_type", sorted(ALL_UPDATES))
def test_invalid_level_rolls_back_entire_update(env: dict, note_type: str) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, note_type, "primary")
    before = service.get_note(note_id).note
    with pytest.raises(NoteValidationError):
        ALL_UPDATES[note_type](service, note_id, importance="high")
    after = service.get_note(note_id).note
    assert after == before  # 内容与等级全部原样


def test_updated_at_behavior(env: dict) -> None:
    service: NoteService = env["service"]
    legacy = _seed_of_type(service, "page", "primary")
    before = service.get_note(legacy).note
    updated = service.update_page_note(legacy, "只改内容").note
    assert updated.importance == "primary"
    assert updated.updated_at > before.updated_at  # 既有行为：正文更新刷新时间
    leveled = service.update_page_note(legacy, "再改内容", importance="secondary").note
    assert leveled.importance == "secondary"
    assert leveled.updated_at >= updated.updated_at


def test_text_selection_anchor_untouched_by_level_change(env: dict) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, "text_selection", "secondary")
    before = service.get_note(note_id).note
    view = service.update_text_selection_content(note_id, importance="primary")
    assert view.note.importance == "primary"
    assert view.note.source_excerpt_snapshot == before.source_excerpt_snapshot
    assert (view.note.selection_start, view.note.selection_end) == (
        before.selection_start,
        before.selection_end,
    )
    assert view.note.source_page_text_sha256 == before.source_page_text_sha256
    assert view.note.user_excerpt == before.user_excerpt


# --- C. rebind / reframe preserve ------------------------------------------------


@pytest.mark.parametrize("level", ["primary", "secondary"])
def test_rebind_text_selection_preserves_level(env: dict, level: str) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, "text_selection", level)
    view = service.rebind_text_selection(note_id, "回路")
    assert view.note.importance == level
    assert view.note.source_excerpt_snapshot == "回路"


@pytest.mark.parametrize("level", ["primary", "secondary"])
def test_rebind_image_region_preserves_level(env: dict, level: str) -> None:
    service: NoteService = env["service"]
    note_id = _seed_of_type(service, "image_region", level)
    view = service.rebind_image_region(note_id, 50, 60, 700, 500)
    assert view.note.importance == level
    assert (view.note.region_x0, view.note.region_y0) == (50, 60)


# --- D. list filter ---------------------------------------------------------


def test_filter_none_returns_all(env: dict) -> None:
    service: NoteService = env["service"]
    ids = _seed_leveled(service)
    all_items = service.list_note_summaries(limit=100)
    assert {item.note.id for item in all_items} == set(ids.values())


@pytest.mark.parametrize("level", ["primary", "secondary", "normal"])
def test_filter_by_level(env: dict, level: str) -> None:
    service: NoteService = env["service"]
    ids = _seed_leveled(service)
    items = service.list_note_summaries(importance=level, limit=100)
    assert [item.note.id for item in items] == [ids[level]]
    assert service.count_notes(importance=level) == 1
    views = service.list_notes(importance=level, limit=100)
    assert [view.note.id for view in views] == [ids[level]]


def test_filter_combines_with_type_and_document(env: dict) -> None:
    service: NoteService = env["service"]
    ids = _seed_leveled(service)
    combined = service.list_note_summaries(
        document_id=1, note_type=NoteType.TEXT_SELECTION, importance="secondary",
        limit=100,
    )
    assert [item.note.id for item in combined] == [ids["secondary"]]
    empty = service.list_note_summaries(
        note_type=NoteType.TEXT_SELECTION, importance="primary", limit=100
    )
    assert empty == []
    assert service.count_notes(note_type=NoteType.TEXT_SELECTION, importance="primary") == 0


def test_filter_pagination_and_default_order(env: dict) -> None:
    service: NoteService = env["service"]
    ids = _seed_leveled(service)
    page_one = service.list_note_summaries(importance=None, limit=2, offset=0)
    page_two = service.list_note_summaries(importance=None, limit=2, offset=2)
    assert len(page_one) == 2 and len(page_two) == 1
    ordered = [item.note.id for item in (*page_one, *page_two)]
    assert ordered == sorted(ids.values(), reverse=True)  # updated_at/id DESC 不变
    with pytest.raises(NoteValidationError):
        service.list_note_summaries(importance="high", limit=100)
    with pytest.raises(NoteValidationError):
        service.count_notes(importance="key")
    with pytest.raises(NoteValidationError):
        service.list_notes(importance="重点", limit=100)


# --- E. preferences ----------------------------------------------------------


def test_default_preferences_read(env: dict) -> None:
    preferences = env["service"].get_display_preferences()
    assert preferences == NoteDisplayPreferences(
        color_primary="#c0392b",
        color_secondary="#b8860b",
        color_normal="#5a6570",
        updated_at=preferences.updated_at,
    )
    assert preferences.updated_at is not None


def test_update_preferences_canonicalizes_and_persists(env: dict) -> None:
    service: NoteService = env["service"]
    before = service.get_display_preferences()
    updated = service.update_display_preferences("#FFAA00", "#112233", "#A0B0C0")
    assert (
        updated.color_primary,
        updated.color_secondary,
        updated.color_normal,
    ) == ("#ffaa00", "#112233", "#a0b0c0")
    assert updated.updated_at is not None and before.updated_at is not None
    assert updated.updated_at >= before.updated_at
    with sqlite3.connect(env["database"].database_path) as connection:
        stored = connection.execute(
            "SELECT color_primary, color_secondary, color_normal"
            " FROM note_display_preferences WHERE id = 1"
        ).fetchone()
    assert stored == ("#ffaa00", "#112233", "#a0b0c0")


@pytest.mark.parametrize(
    "bad",
    ["#fff", "red", "rgb(1,2,3)", "", " #112233", "#1122334", "#11223", "#gg0000",
     "expression(alert(1))", "<b>#112233</b>", "#11223g", None, 123456],
)
def test_update_preferences_rejects_invalid_color(env: dict, bad: object) -> None:
    service: NoteService = env["service"]
    before = service.get_display_preferences()
    with pytest.raises(NoteValidationError):
        service.update_display_preferences("#112233", bad, "#445566")  # type: ignore[arg-type]
    after = service.get_display_preferences()
    assert (
        after.color_primary,
        after.color_secondary,
        after.color_normal,
    ) == (
        before.color_primary,
        before.color_secondary,
        before.color_normal,
    )


def test_preference_update_is_atomic_on_partial_invalid(env: dict) -> None:
    service: NoteService = env["service"]
    before = service.get_display_preferences()
    with pytest.raises(NoteValidationError):
        service.update_display_preferences("#000000", "not-a-color", "#ffffff")
    after = service.get_display_preferences()
    assert (after.color_primary, after.color_secondary, after.color_normal) == (
        before.color_primary,
        before.color_secondary,
        before.color_normal,
    )


def test_reset_preferences(env: dict) -> None:
    service: NoteService = env["service"]
    service.update_display_preferences("#000001", "#000002", "#000003")
    reset = service.reset_display_preferences()
    assert (reset.color_primary, reset.color_secondary, reset.color_normal) == (
        "#c0392b",
        "#b8860b",
        "#5a6570",
    )


def test_missing_row_falls_back_without_repairing(env: dict) -> None:
    service: NoteService = env["service"]
    with sqlite3.connect(env["database"].database_path) as connection:
        connection.execute("DELETE FROM note_display_preferences WHERE id = 1")
        connection.commit()
    preferences = service.get_display_preferences()
    assert (preferences.color_primary, preferences.color_secondary) == (
        "#c0392b",
        "#b8860b",
    )
    assert preferences.updated_at is None
    with sqlite3.connect(env["database"].database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM note_display_preferences"
        ).fetchone()[0]
    assert count == 0  # get 不静默修库


def test_preference_change_does_not_touch_notes(env: dict) -> None:
    service: NoteService = env["service"]
    _seed_leveled(service)
    before = [tuple(row) for row in _notes_rows(env["database"])]
    service.update_display_preferences("#111111", "#222222", "#333333")
    service.reset_display_preferences()
    after = [tuple(row) for row in _notes_rows(env["database"])]
    assert after == before


def test_preference_write_failure_rolls_back(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    service: NoteService = env["service"]
    before = service.get_display_preferences()
    _fail_write_commits(monkeypatch)
    from src.note_service import NoteWriteError

    with pytest.raises(NoteWriteError):
        service.update_display_preferences("#111111", "#222222", "#333333")
    monkeypatch.undo()
    after = service.get_display_preferences()
    assert (after.color_primary, after.color_secondary, after.color_normal) == (
        before.color_primary,
        before.color_secondary,
        before.color_normal,
    )


# --- F. foreground helper -------------------------------------------------


def test_badge_foreground_extremes_and_threshold() -> None:
    assert badge_foreground("#ffffff") == "#1a1a1a"
    assert badge_foreground("#000000") == "#ffffff"
    # 阈值边界：YIQ luma = 128 → 深色前景；127 → 浅色前景
    # luma(127,127,127) = 127；luma(128,128,128) = 128
    assert badge_foreground("#808080") == "#1a1a1a"
    assert badge_foreground("#7f7f7f") == "#ffffff"
    # 确定性
    assert badge_foreground("#c0392b") == badge_foreground("#c0392b")
    # 大小写均可（输入为 canonical 前的前端值由 service 规范化；
    # helper 本身也接受大写合法形式）
    assert badge_foreground("#FFAA00") == "#1a1a1a"
    with pytest.raises(NoteValidationError):
        badge_foreground("red")


def test_foreground_for_default_palette() -> None:
    defaults = NoteDisplayPreferences()
    for background in (
        defaults.color_primary,
        defaults.color_secondary,
        defaults.color_normal,
    ):
        assert badge_foreground(background) in {"#1a1a1a", "#ffffff"}
