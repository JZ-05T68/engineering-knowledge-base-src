"""Streamlit workflow tests for the durable evidence basket page."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.models import PageStatus


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _basket_runtime(
    tmp_path: Path, monkeypatch
) -> tuple[Database, EvidenceBasketService, int, list[int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "证据资料.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"pdf")
    document = database.create_document(
        title="证据资料",
        filename="证据资料.pdf",
        source_path=source_path,
        sha256="6" * 64,
    )
    page_ids: list[int] = []
    for page_number in (1, 2):
        image_path = tmp_path / "pages" / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        page = database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"第 {page_number} 页证据原文。",
            status=(PageStatus.REVIEWED if page_number == 1 else PageStatus.PENDING),
        )
        page_ids.append(page.id)
    database.update_document_page_count(document.id, 2)
    service = EvidenceBasketService(database)
    for page_number, page_id in enumerate(page_ids, start=1):
        service.add_item(
            document_id=document.id,
            page_id=page_id,
            evidence_text=f"第 {page_number} 页证据原文。",
        )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_evidence_basket_service", lambda: service)
    return database, service, document.id, page_ids


def test_basket_page_reorders_updates_notes_exports_and_returns_to_source(
    tmp_path: Path, monkeypatch
) -> None:
    _, service, document_id, page_ids = _basket_runtime(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    page_path = next((Path(__file__).parents[1] / "pages").glob("9_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)

    assert not app.exception
    assert any(metric.label == "当前证据" and metric.value == "2" for metric in app.metric)
    assert any("证据资料 · 第 1 页" in item.value for item in app.markdown)
    assert "生成证据包" in {button.label for button in app.button}

    first_item = service.list_items()[0]
    app.text_area(key=f"evidence_note_{first_item.id}").input("现场备注")
    _button(app, "保存备注").click().run()
    assert service.list_items()[0].user_note == "现场备注"

    reorder_app = AppTest.from_file(str(page_path)).run(timeout=10)
    move_down = reorder_app.button(key=f"evidence_down_{first_item.id}")
    assert not move_down.disabled
    move_down.click().run()
    assert [item.page_id for item in service.list_items()] == [page_ids[1], page_ids[0]]

    export_app = AppTest.from_file(str(page_path)).run(timeout=10)
    _button(export_app, "生成证据包").click().run()
    assert "basket_markdown_package" in export_app.session_state
    assert "证据条数：2" in export_app.session_state["basket_markdown_package"]

    source_app = AppTest.from_file(str(page_path)).run(timeout=10)
    _button(source_app, "返回原始页").click().run()
    assert switched[-1] == "pages/2_浏览资料.py"
    assert source_app.query_params["document"] == [str(document_id)]


def test_basket_page_delete_and_confirmed_clear(tmp_path: Path, monkeypatch) -> None:
    _, service, _, _ = _basket_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("9_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)

    _button(app, "删除").click().run()
    assert len(service.list_items()) == 1
    app.checkbox(key="confirm_clear_basket").check().run()
    _button(app, "清空证据篮").click().run()

    assert service.list_items() == []
    assert any("证据篮为空" in item.value for item in app.info)
