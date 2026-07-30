"""Structured notes service tests (v0.3.0).

All fixtures use temporary databases and synthetic PNGs; production data,
ports and services are never touched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.models import NoteSourceStatus, NoteType
from src.note_service import (
    DuplicateExcerptError,
    ExcerptNotFoundError,
    InvalidImageRegionError,
    NoteDocumentNotFoundError,
    NoteNotFoundError,
    NotePageNotFoundError,
    NoteService,
    NoteTypeMismatchError,
    NoteValidationError,
    NoteWriteError,
    PageImageMissingError,
    PageImageUnreadableError,
    TextSourceUnavailableError,
)

TS = "2026-07-30T00:00:00+00:00"
HASH_A = "a" * 64
EXTRACTED = "液压系统 阀体 回路 压力"


def _make_png(path: Path, size: tuple[int, int] = (800, 1200), color: str = "white") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    ImageDraw_module = __import__("PIL.ImageDraw", fromlist=["ImageDraw"])
    draw = ImageDraw_module.Draw(image)
    draw.rectangle([10, 10, size[0] // 2, size[1] // 2], outline="red", width=4)
    image.save(path)
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


def _set_page_text(database: Database, extracted: str | None, ocr: str | None) -> None:
    with sqlite3.connect(database.database_path) as connection:
        if extracted is not None:
            connection.execute(
                "UPDATE pages SET extracted_text = ? WHERE id = 1", (extracted,)
            )
        if ocr is not None:
            connection.execute("UPDATE pages SET ocr_text = ? WHERE id = 1", (ocr,))
        connection.commit()


def _page_row(database: Database) -> sqlite3.Row:
    with sqlite3.connect(database.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM pages WHERE id = 1").fetchone()


def _note_count(database: Database) -> int:
    with sqlite3.connect(database.database_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]


def _fail_write_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make write transactions roll back at commit time; reads stay intact."""

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


# --- A. document notes -------------------------------------------------------


def test_document_note_create_read_update_delete(env: dict) -> None:
    service: NoteService = env["service"]
    view = service.create_document_note(1, "  整本手册不错  ")
    note = view.note
    assert note.note_type is NoteType.DOCUMENT
    assert note.document_id == 1 and note.page_id is None
    assert note.personal_note == "整本手册不错"
    assert note.created_at == note.updated_at
    assert view.source_status is None

    fetched = service.get_note(note.id)
    assert fetched.note == note

    updated = service.update_document_note(note.id, "修改后的笔记")
    assert updated.note.personal_note == "修改后的笔记"
    assert updated.note.updated_at > note.created_at
    assert updated.note.document_id == 1

    service.delete_note(note.id)
    assert _note_count(env["database"]) == 0
    with pytest.raises(NoteNotFoundError):
        service.get_note(note.id)


def test_document_notes_multiple_and_independent_delete(env: dict) -> None:
    service: NoteService = env["service"]
    first = service.create_document_note(1, "第一条")
    second = service.create_document_note(1, "第二条")
    assert [v.note.id for v in service.list_document_notes(1)] == [second.note.id, first.note.id]

    service.delete_note(first.note.id)
    remaining = service.list_document_notes(1)
    assert [v.note.id for v in remaining] == [second.note.id]
    assert env["database"].get_document(1) is not None


def test_document_note_requires_existing_document(env: dict) -> None:
    with pytest.raises(NoteDocumentNotFoundError):
        env["service"].create_document_note(999, "孤儿")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 20001])
def test_personal_note_validation(env: dict, bad: str) -> None:
    with pytest.raises(NoteValidationError):
        env["service"].create_document_note(1, bad)


def test_personal_note_must_be_string(env: dict) -> None:
    with pytest.raises(NoteValidationError):
        env["service"].create_document_note(1, 123)  # type: ignore[arg-type]


# --- B. page notes -----------------------------------------------------------


def test_page_note_lifecycle_and_page_untouched(env: dict) -> None:
    service: NoteService = env["service"]
    before = _page_row(env["database"])

    first = service.create_page_note(1, "本页第一点")
    second = service.create_page_note(1, "本页第二点")
    assert [v.note.id for v in service.list_page_notes(1)] == [second.note.id, first.note.id]

    updated = service.update_page_note(first.note.id, "改写")
    assert updated.note.personal_note == "改写"
    assert updated.note.page_id == 1

    service.delete_note(second.note.id)
    assert [v.note.id for v in service.list_page_notes(1)] == [first.note.id]

    after = _page_row(env["database"])
    assert after["review_status"] == before["review_status"] == "draft"
    assert after["markdown_content"] == before["markdown_content"] == "# 整理稿"
    assert after["updated_at"] == before["updated_at"]


def test_page_note_requires_existing_page(env: dict) -> None:
    with pytest.raises(NotePageNotFoundError):
        env["service"].create_page_note(999, "孤儿")


# --- C. text selection creation ----------------------------------------------


def test_text_selection_uses_pdf_text_first(env: dict) -> None:
    _set_page_text(env["database"], None, "OCR 内容")
    view = env["service"].create_text_selection_note(1, "阀体", "关注这个")
    note = view.note
    assert note.note_type is NoteType.TEXT_SELECTION
    assert note.source_kind == "pdf_text"
    assert note.source_page_text_sha256 == hashlib.sha256(EXTRACTED.encode()).hexdigest()
    assert note.source_excerpt_snapshot == "阀体"
    assert (note.selection_start, note.selection_end) == (5, 7)
    assert note.user_excerpt == "阀体"
    assert view.source_status is NoteSourceStatus.VALID


def test_text_selection_falls_back_to_ocr(env: dict) -> None:
    _set_page_text(env["database"], "", "识别出的阀体")
    view = env["service"].create_text_selection_note(1, "阀体", "ocr 笔记")
    assert view.note.source_kind == "ocr_text"


def test_text_selection_rejected_without_text_layer(env: dict) -> None:
    _set_page_text(env["database"], "", "")
    with pytest.raises(TextSourceUnavailableError):
        env["service"].create_text_selection_note(1, "阀体", "无源")


def test_text_selection_never_concatenates_sources(env: dict) -> None:
    _set_page_text(env["database"], None, "只在 OCR 里的片段")
    # pdf_text 优先：只在 ocr_text 中出现的片段必须拒绝，不得拼接来源
    with pytest.raises(ExcerptNotFoundError):
        env["service"].create_text_selection_note(1, "只在 OCR 里的片段", "跨源")


def test_text_selection_zero_and_multiple_hits(env: dict) -> None:
    service: NoteService = env["service"]
    with pytest.raises(ExcerptNotFoundError):
        service.create_text_selection_note(1, "不存在的内容", "零命中")
    with pytest.raises(DuplicateExcerptError):
        service.create_text_selection_note(1, "压", "多命中片段")


def test_text_selection_custom_user_excerpt(env: dict) -> None:
    view = env["service"].create_text_selection_note(
        1, "阀体", "笔记", user_excerpt="阀体（核心部件）"
    )
    assert view.note.user_excerpt == "阀体（核心部件）"
    assert view.note.source_excerpt_snapshot == "阀体"


def test_text_selection_leaves_source_columns_untouched(env: dict) -> None:
    service: NoteService = env["service"]
    before = _page_row(env["database"])
    service.create_text_selection_note(1, "阀体", "关注")
    after = _page_row(env["database"])
    for column in ("extracted_text", "ocr_text", "markdown_content", "review_status"):
        assert after[column] == before[column]


# --- D. text selection update & rebind ----------------------------------------


def test_update_text_selection_content_preserves_anchor(env: dict) -> None:
    service: NoteService = env["service"]
    original = service.create_text_selection_note(1, "阀体", "原始笔记").note
    updated = service.update_text_selection_content(
        original.id, user_excerpt="阀体（修订摘录）", personal_note="修订笔记"
    ).note
    assert updated.user_excerpt == "阀体（修订摘录）"
    assert updated.personal_note == "修订笔记"
    for field in (
        "source_kind", "source_page_text_sha256", "source_excerpt_snapshot",
        "selection_start", "selection_end", "page_id", "created_at",
    ):
        assert getattr(updated, field) == getattr(original, field)


def test_update_text_selection_content_requires_change(env: dict) -> None:
    note = env["service"].create_text_selection_note(1, "阀体", "笔记").note
    with pytest.raises(NoteValidationError):
        env["service"].update_text_selection_content(note.id)


def test_text_selection_status_changed_and_missing(env: dict) -> None:
    service: NoteService = env["service"]
    note = service.create_text_selection_note(1, "阀体", "笔记").note
    _set_page_text(env["database"], "完全不同的文本", None)
    assert service.get_note(note.id).source_status is NoteSourceStatus.CHANGED
    _set_page_text(env["database"], "", None)
    assert service.get_note(note.id).source_status is NoteSourceStatus.MISSING
    # 来源变化不得修改数据库中的锚点
    unchanged = service.get_note(note.id).note
    assert unchanged.source_excerpt_snapshot == "阀体"
    assert unchanged.selection_start == 5


def test_rebind_text_selection_atomic_update(env: dict) -> None:
    service: NoteService = env["service"]
    original = service.create_text_selection_note(1, "阀体", "保留我", user_excerpt="旧摘录").note
    rebound = service.rebind_text_selection(original.id, "回路").note
    assert rebound.id == original.id
    assert rebound.created_at == original.created_at
    assert rebound.personal_note == "保留我"
    assert rebound.user_excerpt == "回路"
    assert rebound.source_excerpt_snapshot == "回路"
    assert (rebound.selection_start, rebound.selection_end) == (8, 10)
    assert rebound.updated_at > original.updated_at


def test_rebind_text_selection_rolls_back_on_failure(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: NoteService = env["service"]
    original = service.create_text_selection_note(1, "阀体", "保留我").note
    _fail_write_commits(monkeypatch)
    with pytest.raises(NoteWriteError):
        service.rebind_text_selection(original.id, "回路")
    monkeypatch.undo()
    unchanged = service.get_note(original.id).note
    assert unchanged == original


def test_rebind_requires_selection_type(env: dict) -> None:
    note = env["service"].create_page_note(1, "普通").note
    with pytest.raises(NoteTypeMismatchError):
        env["service"].rebind_text_selection(note.id, "回路")


def test_preview_text_selection_rebind(env: dict) -> None:
    service: NoteService = env["service"]
    note = service.create_text_selection_note(1, "阀体", "笔记").note
    preview = service.preview_text_selection_rebind(note.id, "回路")
    assert preview == {
        "old_source_kind": "pdf_text",
        "old_snapshot": "阀体",
        "new_source_kind": "pdf_text",
        "new_snapshot": "回路",
        "selection_start": 8,
        "selection_end": 10,
    }
    # 预览不得写入
    assert service.get_note(note.id).note.source_excerpt_snapshot == "阀体"


# --- source preview (read-only) ------------------------------------------------


def test_source_preview_prefers_pdf_text(env: dict) -> None:
    preview = env["service"].get_text_selection_source_preview(1)
    assert preview.source_kind == "pdf_text"
    assert preview.source_text == EXTRACTED
    assert preview.label == "来源：PDF 文本层"


def test_source_preview_falls_back_to_ocr(env: dict) -> None:
    _set_page_text(env["database"], "", "仅 OCR 内容")
    preview = env["service"].get_text_selection_source_preview(1)
    assert preview.source_kind == "ocr_text"
    assert preview.source_text == "仅 OCR 内容"
    assert preview.label == "来源：OCR 初稿"


def test_source_preview_rejected_without_text(env: dict) -> None:
    _set_page_text(env["database"], "", "")
    with pytest.raises(TextSourceUnavailableError):
        env["service"].get_text_selection_source_preview(1)


def test_source_preview_requires_existing_page(env: dict) -> None:
    with pytest.raises(NotePageNotFoundError):
        env["service"].get_text_selection_source_preview(999)


def test_source_preview_is_read_only(env: dict) -> None:
    service: NoteService = env["service"]
    service.create_text_selection_note(1, "阀体", "笔记")
    before_page = dict(_page_row(env["database"]))
    before_notes = _note_count(env["database"])
    service.get_text_selection_source_preview(1)
    assert dict(_page_row(env["database"])) == before_page
    assert _note_count(env["database"]) == before_notes


def test_text_selection_status_unavailable_on_read_error(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: NoteService = env["service"]
    note = service.create_text_selection_note(1, "阀体", "笔记").note

    def fail_get_page(page_id: int):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(Database, "get_page", fail_get_page)
    assert service.get_note(note.id).source_status is NoteSourceStatus.UNAVAILABLE


# --- E. image region creation --------------------------------------------------


def test_image_region_create_reads_real_png(env: dict) -> None:
    view = env["service"].create_image_region_note(1, 10, 20, 300, 400, "阀体区域")
    note = view.note
    expected_hash = hashlib.sha256(env["png"].read_bytes()).hexdigest()
    assert note.region_image_width == 800 and note.region_image_height == 1200
    assert note.region_image_sha256 == expected_hash
    assert (note.region_x0, note.region_y0, note.region_x1, note.region_y1) == (10, 20, 300, 400)
    assert view.source_status is NoteSourceStatus.VALID


def test_image_region_reversed_coordinates_sorted(env: dict) -> None:
    view = env["service"].create_image_region_note(1, 300, 400, 10, 20, "反向")
    note = view.note
    assert (note.region_x0, note.region_y0, note.region_x1, note.region_y1) == (10, 20, 300, 400)


def test_image_region_out_of_bounds_clamped(env: dict) -> None:
    view = env["service"].create_image_region_note(1, -50, -50, 5000, 5000, "越界")
    note = view.note
    assert (note.region_x0, note.region_y0, note.region_x1, note.region_y1) == (0, 0, 800, 1200)


def test_image_region_fully_outside_rejected(env: dict) -> None:
    with pytest.raises(InvalidImageRegionError):
        env["service"].create_image_region_note(1, 5000, 5000, 6000, 6000, "全在外")


def test_image_region_non_integer_rejected(env: dict) -> None:
    with pytest.raises(InvalidImageRegionError):
        env["service"].create_image_region_note(1, 0.5, 0, 100, 100, "浮点")


def test_image_region_missing_and_unreadable_png(env: dict) -> None:
    env["png"].unlink()
    with pytest.raises(PageImageMissingError):
        env["service"].create_image_region_note(1, 0, 0, 100, 100, "无图")

    _make_png(env["png"])
    env["png"].write_bytes(b"not a png")
    with pytest.raises(PageImageUnreadableError):
        env["service"].create_image_region_note(1, 0, 0, 100, 100, "坏图")


def test_image_region_requires_existing_page(env: dict) -> None:
    with pytest.raises(NotePageNotFoundError):
        env["service"].create_image_region_note(999, 0, 0, 100, 100, "孤儿")


def test_image_region_delete_keeps_png_and_page(env: dict) -> None:
    service: NoteService = env["service"]
    note = service.create_image_region_note(1, 10, 20, 300, 400, "区域").note
    before_hash = hashlib.sha256(env["png"].read_bytes()).hexdigest()
    before_page = _page_row(env["database"])
    service.delete_note(note.id)
    assert hashlib.sha256(env["png"].read_bytes()).hexdigest() == before_hash
    after_page = _page_row(env["database"])
    assert dict(after_page) == dict(before_page)


# --- F. image region status & rebind -------------------------------------------


def test_image_region_status_transitions(env: dict) -> None:
    service: NoteService = env["service"]
    note = service.create_image_region_note(1, 10, 20, 300, 400, "区域").note
    assert service.get_note(note.id).source_status is NoteSourceStatus.VALID

    _make_png(env["png"], size=(1000, 1200))  # 宽度变化
    assert service.get_note(note.id).source_status is NoteSourceStatus.CHANGED

    _make_png(env["png"], size=(800, 1000))  # 高度变化
    assert service.get_note(note.id).source_status is NoteSourceStatus.CHANGED

    _make_png(env["png"], size=(800, 1200), color="blue")  # 同尺寸不同内容
    assert service.get_note(note.id).source_status is NoteSourceStatus.CHANGED

    env["png"].unlink()
    assert service.get_note(note.id).source_status is NoteSourceStatus.MISSING

    _make_png(env["png"])
    env["png"].write_bytes(b"definitely not a png")
    assert service.get_note(note.id).source_status is NoteSourceStatus.UNREADABLE


def test_rebind_image_region_atomic_update(env: dict) -> None:
    service: NoteService = env["service"]
    original = service.create_image_region_note(1, 10, 20, 300, 400, "保留我").note
    _make_png(env["png"], size=(1000, 800), color="gray")
    rebound = service.rebind_image_region(original.id, 50, 60, 700, 500).note
    assert rebound.id == original.id
    assert rebound.created_at == original.created_at
    assert rebound.personal_note == "保留我"
    assert rebound.region_image_width == 1000 and rebound.region_image_height == 800
    assert rebound.region_image_sha256 == hashlib.sha256(env["png"].read_bytes()).hexdigest()
    assert (rebound.region_x0, rebound.region_y0, rebound.region_x1, rebound.region_y1) == (
        50, 60, 700, 500,
    )
    assert rebound.updated_at > original.updated_at
    assert service.get_note(original.id).source_status is NoteSourceStatus.VALID


def test_rebind_image_region_rolls_back_on_failure(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: NoteService = env["service"]
    original = service.create_image_region_note(1, 10, 20, 300, 400, "保留我").note
    _fail_write_commits(monkeypatch)
    with pytest.raises(NoteWriteError):
        service.rebind_image_region(original.id, 50, 60, 700, 500)
    monkeypatch.undo()
    assert service.get_note(original.id).note == original


# --- G. queries ------------------------------------------------------------------


def _seed_all_types(service: NoteService) -> dict[str, int]:
    return {
        "document": service.create_document_note(1, "文档笔记").note.id,
        "page": service.create_page_note(1, "页面笔记").note.id,
        "text": service.create_text_selection_note(1, "阀体", "选区笔记").note.id,
        "region": service.create_image_region_note(1, 1, 1, 100, 100, "区域笔记").note.id,
    }


def test_get_note_missing_raises(env: dict) -> None:
    with pytest.raises(NoteNotFoundError):
        env["service"].get_note(4242)


def test_list_document_notes_excludes_page_notes(env: dict) -> None:
    ids = _seed_all_types(env["service"])
    listed = env["service"].list_document_notes(1)
    assert [v.note.id for v in listed] == [ids["document"]]


def test_list_page_notes_returns_three_types(env: dict) -> None:
    _seed_all_types(env["service"])
    types = {v.note.note_type for v in env["service"].list_page_notes(1)}
    assert types == {NoteType.PAGE, NoteType.TEXT_SELECTION, NoteType.IMAGE_REGION}


def test_list_notes_filters_and_document_join(env: dict) -> None:
    service: NoteService = env["service"]
    ids = _seed_all_types(service)
    # 文档筛选包含直接笔记与页面笔记
    by_document = service.list_notes(document_id=1)
    assert {v.note.id for v in by_document} == set(ids.values())
    by_type = service.list_notes(document_id=1, note_type=NoteType.TEXT_SELECTION)
    assert [v.note.id for v in by_type] == [ids["text"]]
    by_page = service.list_notes(page_id=1)
    assert {v.note.id for v in by_page} == {ids["page"], ids["text"], ids["region"]}
    limited = service.list_notes(document_id=1, limit=2)
    assert len(limited) == 2
    paged = service.list_notes(document_id=1, limit=2, offset=2)
    assert len(paged) == 2
    assert {v.note.id for v in limited}.isdisjoint({v.note.id for v in paged})
    assert service.list_notes(document_id=999) == []


def test_list_notes_rejects_bad_pagination(env: dict) -> None:
    service: NoteService = env["service"]
    with pytest.raises(NoteValidationError):
        service.list_notes(limit=0)
    with pytest.raises(NoteValidationError):
        service.list_notes(limit=501)
    with pytest.raises(NoteValidationError):
        service.list_notes(offset=-1)
    with pytest.raises(NoteValidationError):
        service.list_notes(note_type="importance")


def test_list_notes_default_order(env: dict) -> None:
    service: NoteService = env["service"]
    ids = _seed_all_types(service)
    listed = service.list_notes(document_id=1)
    # 区域笔记最后创建，应排最前；文档笔记最早，应排最后
    assert listed[0].note.id == ids["region"]
    assert listed[-1].note.id == ids["document"]


# --- H. delete isolation ---------------------------------------------------------


def _seed_evidence(database: Database) -> None:
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO evidence_baskets(id, name, created_at, updated_at)"
            " VALUES (1, '默认', ?, ?)", (TS, TS),
        )
        connection.execute(
            "INSERT INTO evidence_items(basket_id, document_id, page_id, document_title,"
            " filename, page_number, review_status, evidence_text, text_kind, context,"
            " context_kind, user_note,"
            " source_text_sha256, source_locator, selection_sha256, added_at, position)"
            " VALUES (1, 1, 1, '液压手册', 'a.pdf', 1, 'draft', '阀体',"
            " 'original_material', '', 'system_generated', '', ?, 'page:1', ?, ?, 1)",
            (HASH_A, "c" * 64, TS),
        )
        connection.commit()


@pytest.mark.parametrize("note_type", ["document", "page", "text", "region"])
def test_delete_isolation_per_type(env: dict, note_type: str) -> None:
    database: Database = env["database"]
    service: NoteService = env["service"]
    ids = _seed_all_types(service)
    _seed_evidence(database)
    png_hash = hashlib.sha256(env["png"].read_bytes()).hexdigest()
    before_page = dict(_page_row(database))
    before_doc = database.get_document(1)

    target = ids[note_type]
    service.delete_note(target)

    remaining = {v.note.id for v in service.list_notes(document_id=1)}
    assert remaining == set(ids.values()) - {target}
    assert database.get_document(1) == before_doc
    assert dict(_page_row(database)) == before_page
    assert hashlib.sha256(env["png"].read_bytes()).hexdigest() == png_hash
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_missing_note_raises(env: dict) -> None:
    with pytest.raises(NoteNotFoundError):
        env["service"].delete_note(31337)


# --- I. database failures ---------------------------------------------------------


def test_insert_failure_rolls_back(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_write_commits(monkeypatch)
    with pytest.raises(NoteWriteError):
        env["service"].create_page_note(1, "写不进去")
    monkeypatch.undo()
    assert _note_count(env["database"]) == 0


def test_update_failure_rolls_back(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    service: NoteService = env["service"]
    original = service.create_page_note(1, "原始").note
    _fail_write_commits(monkeypatch)
    with pytest.raises(NoteWriteError):
        service.update_page_note(original.id, "篡改")
    monkeypatch.undo()
    assert service.get_note(original.id).note == original


def test_delete_failure_rolls_back(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    service: NoteService = env["service"]
    note = service.create_page_note(1, "删不掉").note
    _fail_write_commits(monkeypatch)
    with pytest.raises(NoteWriteError):
        service.delete_note(note.id)
    monkeypatch.undo()
    assert service.get_note(note.id).note == note
