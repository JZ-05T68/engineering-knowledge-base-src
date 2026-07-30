"""List-page query support tests: count_notes / list_note_summaries / options."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.models import NoteSourceStatus, NoteType
from src.note_service import NoteService, NoteValidationError

TS = "2026-07-30T00:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64
TEXT = "液压系统 阀体 回路 压力"


@pytest.fixture
def env(tmp_path: Path) -> dict:
    database = Database(tmp_path / "knowledge.db")
    service = NoteService(database)
    png = tmp_path / "pages" / "page_0001.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(png)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for doc_id, title in ((1, "手册甲"), (2, "手册乙")):
            connection.execute(
                "INSERT INTO documents(id, title, filename, source_path, sha256,"
                " page_count, created_at, updated_at)"
                " VALUES (?, ?, 'a.pdf', 'data/raw/a.pdf', ?, 1, ?, ?)",
                (doc_id, title, HASH_A if doc_id == 1 else HASH_B, TS, TS),
            )
            connection.execute(
                "INSERT INTO pages(id, document_id, page_number, image_path,"
                " extracted_text, status, review_status, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?, 'text_extracted', 'pending', ?, ?)",
                (doc_id, doc_id, str(png), TEXT if doc_id == 1 else "另一页", TS, TS),
            )
        connection.commit()
    # 文档甲：四类笔记；文档乙：文档级 + 页面级
    service.create_document_note(1, "甲文档笔记")
    service.create_page_note(1, "甲页面笔记")
    service.create_text_selection_note(1, "阀体", "甲选区笔记")
    service.create_image_region_note(1, 10, 20, 300, 400, "甲区域笔记")
    service.create_document_note(2, "乙文档笔记")
    service.create_page_note(2, "乙页面笔记")
    return {"database": database, "service": service, "png": png}


def test_count_matches_list_semantics(env: dict) -> None:
    service: NoteService = env["service"]
    assert service.count_notes() == 6
    assert len(service.list_note_summaries(limit=500)) == 6
    assert service.count_notes(document_id=1) == 4
    assert service.count_notes(document_id=2) == 2
    assert service.count_notes(note_type=NoteType.TEXT_SELECTION) == 1
    assert service.count_notes(document_id=1, note_type="page") == 1
    assert service.count_notes(page_id=2) == 1
    assert service.count_notes(document_id=999) == 0


def test_document_filter_covers_page_scoped_notes(env: dict) -> None:
    service: NoteService = env["service"]
    items = service.list_note_summaries(document_id=1, limit=100)
    assert {item.note.note_type for item in items} == {
        NoteType.DOCUMENT,
        NoteType.PAGE,
        NoteType.TEXT_SELECTION,
        NoteType.IMAGE_REGION,
    }
    assert all(item.document_title == "手册甲" for item in items)
    page_scoped = [item for item in items if item.note.note_type is not NoteType.DOCUMENT]
    assert all(item.page_number == 1 for item in page_scoped)
    document_note = next(
        item for item in items if item.note.note_type is NoteType.DOCUMENT
    )
    assert document_note.page_number is None
    assert document_note.document_id == 1


def test_list_pagination_and_order(env: dict) -> None:
    service: NoteService = env["service"]
    first = service.list_note_summaries(limit=2, offset=0)
    second = service.list_note_summaries(limit=2, offset=2)
    third = service.list_note_summaries(limit=2, offset=4)
    assert len(first) == len(second) == len(third) == 2
    ids = [item.note.id for item in (*first, *second, *third)]
    assert len(set(ids)) == 6
    # 最新创建的乙页面笔记（id 最大）应排最前
    assert first[0].note.note_type is NoteType.PAGE
    assert first[0].note.id == max(ids)


def test_pagination_validation(env: dict) -> None:
    service: NoteService = env["service"]
    with pytest.raises(NoteValidationError):
        service.list_note_summaries(limit=0)
    with pytest.raises(NoteValidationError):
        service.list_note_summaries(limit=501)
    with pytest.raises(NoteValidationError):
        service.list_note_summaries(offset=-1)
    with pytest.raises(NoteValidationError):
        service.count_notes(note_type="importance")


def test_document_options(env: dict) -> None:
    options = env["service"].list_note_document_options()
    # 按标题 NOCASE 排序：手册乙 在 手册甲 之前
    assert options == [(2, "手册乙"), (1, "手册甲")]


def test_text_selection_status_inline(env: dict) -> None:
    service: NoteService = env["service"]
    item = next(
        item
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is NoteType.TEXT_SELECTION
    )
    assert item.source_status is NoteSourceStatus.VALID
    with sqlite3.connect(env["database"].database_path) as connection:
        connection.execute(
            "UPDATE pages SET extracted_text = '已被改写' WHERE id = 1"
        )
        connection.commit()
    item = next(
        item
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is NoteType.TEXT_SELECTION
    )
    assert item.source_status is NoteSourceStatus.CHANGED
    with sqlite3.connect(env["database"].database_path) as connection:
        connection.execute("UPDATE pages SET extracted_text = '' WHERE id = 1")
        connection.commit()
    item = next(
        item
        for item in service.list_note_summaries(limit=100)
        if item.note.note_type is NoteType.TEXT_SELECTION
    )
    assert item.source_status is NoteSourceStatus.MISSING


def test_region_status_stays_lazy_and_no_per_note_queries(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: NoteService = env["service"]
    calls = {"get_page": 0, "read_png": 0, "get_note": 0}
    original_get_page = Database.get_page
    original_read = NoteService._read_page_image

    def counted_page(self, page_id):
        calls["get_page"] += 1
        return original_get_page(self, page_id)

    def counted_read(self, path):
        calls["read_png"] += 1
        return original_read(self, path)

    monkeypatch.setattr(Database, "get_page", counted_page)
    monkeypatch.setattr(NoteService, "_read_page_image", counted_read)
    items = service.list_note_summaries(limit=100)
    assert len(items) == 6
    region = next(
        item for item in items if item.note.note_type is NoteType.IMAGE_REGION
    )
    assert region.source_status is None  # 图片身份状态保持懒加载
    assert calls == {"get_page": 0, "read_png": 0, "get_note": 0}
