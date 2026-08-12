"""Page image rendering resilience tests (AppTest).

A single corrupted or undecodable page PNG must degrade only the affected
image area — the reader page, the structured-notes overlays and the notes
list page must never crash as a whole. Fixtures use temporary databases and
synthetic PNGs only; production data and port 8501 are never touched.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

import src.note_ui as note_ui
import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.note_service import NoteService

READER = str(next((Path(__file__).parents[1] / "pages").glob("3_*.py")))
LIST_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("6_*.py")))


def _seed_database(tmp_path: Path) -> tuple[Database, int, int, Path]:
    """One document, one page with a healthy PNG and one region note."""

    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    document = database.create_document(
        title="渲染韧性测试",
        filename="resilience.pdf",
        source_path=raw_dir / "resilience.pdf",
        sha256="b" * 64,
    )
    image_path = pages_dir / str(document.id) / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(image_path)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="第 1 页 阀体 回路",
    )
    database.update_document_page_count(document.id, 1)
    note_id = NoteService(database).create_image_region_note(
        page.id, 10, 20, 300, 400, "区域笔记"
    ).note.id
    return database, document.id, note_id, image_path


def _build_reader(tmp_path: Path, monkeypatch):
    database, document_id, note_id, image_path = _seed_database(tmp_path)
    service = DocumentService(
        database, tmp_path / "raw", tmp_path / "pages", tmp_path / "markdown"
    )
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
            raw_dir=tmp_path / "raw",
            pages_dir=tmp_path / "pages",
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )
    app = AppTest.from_file(READER).run(timeout=25)
    return app, database, document_id, note_id, image_path


def _build_list_page(tmp_path: Path, monkeypatch):
    database, document_id, note_id, image_path = _seed_database(tmp_path)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    app = AppTest.from_file(LIST_PAGE).run(timeout=25)
    return app, note_id, image_path


def _truncate(png: Path) -> None:
    """Keep a readable PNG header (size still parses) but destroy pixel data."""

    png.write_bytes(png.read_bytes()[:100])


def test_reader_main_image_failure_degrades_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """主图栏遇到截断 PNG：整页不崩溃，局部降级为中文错误提示。"""
    app, _, _, _, image_path = _build_reader(tmp_path, monkeypatch)
    _truncate(image_path)
    app.run(timeout=25)

    assert not app.exception
    assert any("页面图片无法显示" in error.value for error in app.error)
    # 页面其余区域（含结构化笔记 tab）仍然正常渲染
    assert any(header.value == "本页笔记" for header in app.subheader)


def test_reader_region_preview_failure_degrades_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """阅读页区域预览遇到截断 PNG：预览降级为提示，整页不崩溃。"""
    app, _, _, note_id, image_path = _build_reader(tmp_path, monkeypatch)
    _truncate(image_path)
    app.run(timeout=25)
    assert not app.exception

    app.checkbox(key=f"note_image_preview_toggle_{note_id}").check().run()
    assert not app.exception
    assert any("无法生成预览" in caption.value for caption in app.caption)


def test_list_page_region_preview_failure_degrades_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """列表页区域预览遇到截断 PNG：预览降级为提示，整页不崩溃。"""
    app, note_id, image_path = _build_list_page(tmp_path, monkeypatch)
    _truncate(image_path)
    app.run(timeout=25)
    assert not app.exception

    buttons = [b for b in app.button if b.key == f"note_list_preview_show_btn_{note_id}"]
    assert buttons
    buttons[0].click().run()
    assert not app.exception
    assert any("无法生成区域预览" in warning.value for warning in app.warning)


def test_region_overlay_render_failure_is_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    """overlay 生成本身失败（解码异常）时只降级该预览，不穿透页面。"""

    def broken_overlay(image_path, region):
        raise OSError("image file is truncated")

    monkeypatch.setattr(note_ui, "_region_overlay_bytes", broken_overlay)
    app, note_id, _ = _build_list_page(tmp_path, monkeypatch)
    assert not app.exception

    buttons = [b for b in app.button if b.key == f"note_list_preview_show_btn_{note_id}"]
    assert buttons
    buttons[0].click().run()
    assert not app.exception
    assert any("区域预览生成失败" in warning.value for warning in app.warning)
