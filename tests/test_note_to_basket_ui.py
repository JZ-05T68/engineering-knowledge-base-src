"""Reader-page whole-page evidence and note→basket workflow tests (AppTest).

Fixtures use temporary databases and synthetic PNGs only. Production data and
port 8501 are never touched.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import EvidenceType
from src.note_service import NoteService

READER = str(next((Path(__file__).parents[1] / "pages").glob("3_*.py")))


def _build_reader(
    tmp_path: Path, monkeypatch
) -> tuple[AppTest, Database, NoteService, EvidenceBasketService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    (raw_dir / "notes-ui.pdf").write_bytes(b"pdf")
    document = database.create_document(
        title="笔记证据界面测试",
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
    basket_service = EvidenceBasketService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime, "application_evidence_basket_service", lambda: basket_service
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
    return app, database, note_service, basket_service, document.id


def _button(app: AppTest, key: str):
    """Return the latest widget with ``key`` (st.rerun 会在 AppTest 树中留下双份）。"""

    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}；现有：{[b.key for b in app.button][:12]}"
    return matches[-1]


def test_whole_page_evidence_button_adds_once(tmp_path: Path, monkeypatch) -> None:
    app, database, _, basket_service, document_id = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    page = database.get_page_by_number(document_id, 1)

    _button(app, f"reader_add_page_basket_{page.id}").click().run()
    items = basket_service.list_items()
    assert len(items) == 1
    assert items[0].evidence_type is EvidenceType.PAGE
    assert items[0].page_id == page.id
    assert items[0].document_id == document_id

    _button(app, f"reader_add_page_basket_{page.id}").click().run()
    assert any("整页证据已在证据篮中" in item.value for item in app.info)
    assert len(basket_service.list_items()) == 1


def test_text_selection_note_adds_to_basket(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, basket_service, document_id = _build_reader(
        tmp_path, monkeypatch
    )
    page = database.get_page_by_number(document_id, 1)
    note_view = note_service.create_text_selection_note(page.id, "阀体", "选区笔记")
    app.run(timeout=25)
    assert not app.exception

    _button(app, f"note_add_basket_{note_view.note.id}").click().run()
    items = basket_service.list_items()
    assert len(items) == 1
    item = items[0]
    assert item.evidence_type is EvidenceType.TEXT_SELECTION
    assert item.evidence_text == "阀体"
    assert item.user_note == "选区笔记"
    assert item.page_id == page.id
    assert item.document_id == document_id
    assert any("已加入证据篮" in success.value for success in app.success)

    _button(app, f"note_add_basket_{note_view.note.id}").click().run()
    assert any("已在证据篮中" in item.value for item in app.info)
    assert len(basket_service.list_items()) == 1


def test_image_region_note_adds_to_basket(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, basket_service, document_id = _build_reader(
        tmp_path, monkeypatch
    )
    page = database.get_page_by_number(document_id, 1)
    note_view = note_service.create_image_region_note(
        page.id, 10, 20, 300, 400, "区域笔记"
    )
    app.run(timeout=25)
    assert not app.exception

    _button(app, f"note_add_basket_{note_view.note.id}").click().run()
    items = basket_service.list_items()
    assert len(items) == 1
    item = items[0]
    assert item.evidence_type is EvidenceType.IMAGE_REGION
    assert (item.region_x0, item.region_y0, item.region_x1, item.region_y1) == (
        10,
        20,
        300,
        400,
    )
    assert item.region_image_width == 800
    assert item.region_image_height == 1200
    assert item.user_note == "区域笔记"
    assert item.document_id == document_id
