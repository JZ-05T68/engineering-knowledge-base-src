"""Streamlit workflow tests for the durable evidence basket page."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.models import EvidenceConfirmationStatus, EvidenceType, PageStatus


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _last_button(app: AppTest, key: str):
    """Return the latest widget with ``key`` (st.rerun 会在 AppTest 树中留下双份）。"""

    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[-1]


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
    page_path = next((Path(__file__).parents[1] / "pages").glob("7_*.py"))
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
    expected_source = service.list_items()[0]
    _button(source_app, "返回原始页").click().run()
    assert switched[-1] == "pages/3_浏览资料.py"
    assert source_app.query_params["document"] == [str(document_id)]
    assert source_app.session_state["pending_reader_query_params"] == {
        "document": str(document_id),
        "page": str(expected_source.page_number),
        "from_search": "0",
    }


def test_basket_page_delete_and_confirmed_clear(tmp_path: Path, monkeypatch) -> None:
    _, service, _, _ = _basket_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("7_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)

    _button(app, "删除").click().run()
    assert len(service.list_items()) == 1
    app.checkbox(key="confirm_clear_basket").check().run()
    _button(app, "清空证据篮").click().run()

    assert service.list_items() == []
    assert any("证据篮为空" in item.value for item in app.info)


def test_basket_page_confirmation_toggle(tmp_path: Path, monkeypatch) -> None:
    _, service, _, _ = _basket_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("7_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)
    assert not app.exception

    first_item = service.list_items()[0]
    assert first_item.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED
    confirm_button = _last_button(app, f"evidence_confirm_{first_item.id}")
    assert confirm_button.label == "确认证据"
    assert any(
        f"确认状态：{EvidenceConfirmationStatus.UNCONFIRMED.label}" in caption.value
        for caption in app.caption
    )

    confirm_button.click().run()
    confirmed = next(item for item in service.list_items() if item.id == first_item.id)
    assert confirmed.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
    assert confirmed.confirmed_at is not None
    assert any(
        f"确认状态：{EvidenceConfirmationStatus.CONFIRMED.label}" in caption.value
        and "确认于" in caption.value
        for caption in app.caption
    )
    cancel_button = _last_button(app, f"evidence_confirm_{first_item.id}")
    assert cancel_button.label == "取消确认"

    cancel_button.click().run()
    restored = next(item for item in service.list_items() if item.id == first_item.id)
    assert restored.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED
    assert restored.confirmed_at is None
    assert (
        _last_button(app, f"evidence_confirm_{first_item.id}").label == "确认证据"
    )


def _typed_basket_runtime(
    tmp_path: Path, monkeypatch
) -> tuple[Database, EvidenceBasketService, int, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "类型证据.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"pdf")
    document = database.create_document(
        title="类型证据",
        filename="类型证据.pdf",
        source_path=source_path,
        sha256="7" * 64,
    )
    image_path = tmp_path / "pages" / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 100), "white").save(image_path)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="整页与区域证据原文。",
        status=PageStatus.REVIEWED,
    )
    database.update_document_page_count(document.id, 1)
    service = EvidenceBasketService(database)
    basket = service.default_basket()
    service.add_page_item(basket.id, document.id, page.id, user_note="整页备注")
    service.add_region_item(
        basket.id, document.id, page.id, x0=10, y0=20, x1=30, y1=40
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_evidence_basket_service", lambda: service)
    return database, service, document.id, page.id


def _all_display_text(app: AppTest) -> list[str]:
    texts = [element.value for element in app.caption]
    texts.extend(element.value for element in app.markdown)
    return texts


def test_basket_page_type_badges_region_coords_and_page_body(
    tmp_path: Path, monkeypatch
) -> None:
    _, service, _, _ = _typed_basket_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("7_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)
    assert not app.exception

    texts = _all_display_text(app)
    assert any(EvidenceType.PAGE.label in text for text in texts)
    assert any(EvidenceType.IMAGE_REGION.label in text for text in texts)
    assert any("区域坐标：(10, 20) - (30, 40)" in text for text in texts)
    assert any("锚点图像尺寸：100 × 100 像素" in text for text in texts)
    assert any("整页证据引用整个页面" in text for text in texts)
    # 整页与图片区域证据都没有选区文本，不应出现证据代码块。
    assert len(app.code) == 0
    assert {item.evidence_type for item in service.list_items()} == {
        EvidenceType.PAGE,
        EvidenceType.IMAGE_REGION,
    }


def test_basket_page_prompt_package_is_confirmed_only(
    tmp_path: Path, monkeypatch
) -> None:
    _, service, _, _ = _basket_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("7_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)
    assert not app.exception

    # 两条证据均未确认：计数明确、空态清楚、生成入口禁用
    assert any(
        "已确认 0 条" in caption.value and "未确认 2 条" in caption.value
        for caption in app.caption
    )
    assert any("没有已确认的证据" in info.value for info in app.info)
    assert _last_button(app, "generate_prompt_package").disabled
    # 原 Markdown 证据包出口保持不变
    assert "生成证据包" in {button.label for button in app.button}

    first_item = service.list_items()[0]
    service.set_confirmation(first_item.id, True)
    confirmed_app = AppTest.from_file(str(page_path)).run(timeout=10)
    assert not confirmed_app.exception
    assert any(
        "已确认 1 条" in caption.value and "未确认 1 条" in caption.value
        for caption in confirmed_app.caption
    )
    confirmed_app.text_area(key="basket_prompt_question").input("液压系统如何维护？")
    _last_button(confirmed_app, "generate_prompt_package").click().run()

    package = confirmed_app.session_state["basket_prompt_package"]
    assert "液压系统如何维护？" in package
    assert "第 1 页证据原文。" in package
    assert "第 2 页证据原文。" not in package  # 未确认证据绝不混入
    assert "只能根据“知识片段”" in package
    assert any("第 1 页证据原文。" in block.value for block in confirmed_app.code)
