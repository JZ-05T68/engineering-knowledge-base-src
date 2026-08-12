"""Tests for typed evidence objects: page, text-selection and image-region items.

Covers the v0.4.0 slice 1-3 service layer: three evidence types sharing the
single evidence_items table, manual confirmation state transitions, per-type
source validation and export compatibility. Fixture pattern follows
tests/test_evidence_basket_service.py (real placeholder PDF + PIL PNGs).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
    EvidenceBasketService,
    EvidenceSourceError,
)
from src.models import (
    EvidenceConfirmationStatus,
    EvidenceTextKind,
    EvidenceType,
    PageStatus,
)

PAGE_SIZE = (40, 30)


def _library(tmp_path: Path) -> tuple[Database, EvidenceBasketService, int, list[int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "液压手册.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"local pdf")
    document = database.create_document(
        title="液压系统手册",
        filename="液压手册.pdf",
        source_path=source_path,
        sha256="a" * 64,
    )
    page_ids: list[int] = []
    texts = (
        "液压泵需要定期检查压力和温度。",
        "阀组安装后应执行泄漏测试。",
    )
    for page_number, text in enumerate(texts, start=1):
        image_path = tmp_path / "pages" / "1" / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", PAGE_SIZE, "white").save(image_path)
        page = database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=text,
            status=PageStatus.REVIEWED,
        )
        page_ids.append(page.id)
    database.update_document_page_count(document.id, len(texts))
    return database, EvidenceBasketService(database), document.id, page_ids


def _page_image_path(database: Database, page_id: int) -> Path:
    page = database.get_page(page_id)
    assert page is not None
    return page.image_path


# --- T1 页面证据 ---------------------------------------------------------------


def test_page_evidence_roundtrip_validation_and_export(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()

    item = service.add_page_item(basket.id, document_id, page_ids[0], user_note="整页留存")

    assert item.evidence_type is EvidenceType.PAGE
    assert item.evidence_text == ""
    assert item.text_kind is EvidenceTextKind.ORIGINAL
    assert item.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED
    assert item.confirmed_at is None
    assert item.region_image_sha256 is None and item.region_x0 is None
    assert item.document_id == document_id and item.page_id == page_ids[0]
    assert f"document_id={document_id}" in item.source_locator
    assert f"page_id={page_ids[0]}" in item.source_locator

    validated = service.validated_item(item.id)
    assert validated.evidence_type is EvidenceType.PAGE
    assert validated.image_path == _page_image_path(database, page_ids[0])

    with pytest.raises(DuplicateEvidenceError):
        service.add_page_item(basket.id, document_id, page_ids[0])

    package = service.export_markdown()
    assert "整页证据" in package
    assert "来源定位" in package


# --- T2 文字选区证据回归 ----------------------------------------------------------


def test_text_selection_evidence_stays_unchanged_path(tmp_path: Path) -> None:
    _, service, document_id, page_ids = _library(tmp_path)

    item = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )

    assert item.evidence_type is EvidenceType.TEXT_SELECTION
    assert item.text_kind is EvidenceTextKind.ORIGINAL
    assert item.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED
    assert item.region_image_sha256 is None
    validated = service.validated_item(item.id)
    assert validated.evidence_text == "液压泵需要定期检查压力和温度。"

    package = service.export_markdown()
    assert "液压泵需要定期检查压力和温度。" in package
    assert "可信度：该选区已在加入时匹配当前 PDF 文本层或 OCR 原始文本。" in package


# --- T3 区域证据 ---------------------------------------------------------------


def test_region_evidence_measures_real_png_and_validates_anchor(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()
    page_id = page_ids[0]
    image_path = _page_image_path(database, page_id)
    expected_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()

    item = service.add_region_item(
        basket.id, document_id, page_id, x0=5, y0=5, x1=25, y1=20
    )

    assert item.evidence_type is EvidenceType.IMAGE_REGION
    assert item.evidence_text == ""
    assert (item.region_image_width, item.region_image_height) == PAGE_SIZE
    assert item.region_image_sha256 == expected_sha256
    assert (item.region_x0, item.region_y0, item.region_x1, item.region_y1) == (
        5,
        5,
        25,
        20,
    )
    assert service.validated_item(item.id).evidence_type is EvidenceType.IMAGE_REGION
    with sqlite3.connect(database.database_path) as connection:
        stored_selection = connection.execute(
            "SELECT selection_sha256 FROM evidence_items WHERE id = ?", (item.id,)
        ).fetchone()[0]
    assert stored_selection == hashlib.sha256(b"5,5,25,20").hexdigest()

    with pytest.raises(DuplicateEvidenceError):
        service.add_region_item(basket.id, document_id, page_id, x0=5, y0=5, x1=25, y1=20)
    with pytest.raises(DuplicateEvidenceError):  # 反向拖拽规范化后仍是同一区域
        service.add_region_item(basket.id, document_id, page_id, x0=25, y0=20, x1=5, y1=5)

    package = service.export_markdown()
    assert "图片区域证据" in package
    assert "(5, 5) – (25, 20)" in package
    assert f"SHA-256 摘要 {expected_sha256[:16]}" in package

    # 页面图像被替换后，区域锚点必须失效
    Image.new("RGB", PAGE_SIZE, "black").save(image_path)
    with pytest.raises(EvidenceSourceError, match="页面图像已发生变化"):
        service.validated_item(item.id)


def test_region_evidence_rejects_invalid_geometry_and_sources(tmp_path: Path) -> None:
    _, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()

    with pytest.raises(EvidenceBasketError, match="图片区域无效"):  # 完全在图外
        service.add_region_item(
            basket.id, document_id, page_ids[0], x0=100, y0=100, x1=120, y1=120
        )
    with pytest.raises(EvidenceBasketError, match="图片区域无效"):  # 零面积
        service.add_region_item(
            basket.id, document_id, page_ids[0], x0=5, y0=5, x1=5, y1=20
        )
    with pytest.raises(EvidenceBasketError, match="图片区域无效"):  # 非整数坐标
        service.add_region_item(
            basket.id, document_id, page_ids[0], x0=1.5, y0=5, x1=25, y1=20
        )
    with pytest.raises(EvidenceSourceError, match="页面记录不存在"):
        service.add_region_item(basket.id, document_id, 9999, x0=0, y0=0, x1=5, y1=5)
    assert service.list_items() == []


def test_region_evidence_rejects_corrupt_page_image(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()
    image_path = _page_image_path(database, page_ids[1])
    image_path.write_bytes(b"not a real png")

    with pytest.raises(EvidenceSourceError, match="页面图像无法读取"):
        service.add_region_item(
            basket.id, document_id, page_ids[1], x0=0, y0=0, x1=5, y1=5
        )
    assert service.list_items() == []


# --- T4 确认状态 ---------------------------------------------------------------


def test_confirmation_roundtrip_persists_and_rejects_noop(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()
    item = service.add_page_item(basket.id, document_id, page_ids[0])

    confirmed = service.set_confirmation(item.id, True)
    assert confirmed.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
    assert confirmed.confirmed_at is not None

    reopened = EvidenceBasketService(Database(database.database_path))
    loaded = next(candidate for candidate in reopened.list_items() if candidate.id == item.id)
    assert loaded.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
    assert loaded.confirmed_at == confirmed.confirmed_at

    reverted = reopened.set_confirmation(item.id, False)
    assert reverted.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED
    assert reverted.confirmed_at is None

    with pytest.raises(EvidenceBasketError, match="已是未确认状态"):
        reopened.set_confirmation(item.id, False)
    with pytest.raises(EvidenceBasketError, match="不存在"):
        reopened.set_confirmation(9999, True)
    with pytest.raises(EvidenceBasketError, match="布尔值"):
        reopened.set_confirmation(item.id, "yes")  # type: ignore[arg-type]


# --- T5 无效来源 ---------------------------------------------------------------


def test_invalid_sources_are_rejected_without_orphans(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()

    other_source = tmp_path / "raw" / "其他.pdf"
    other_source.write_bytes(b"other pdf")
    other_document = database.create_document(
        title="其他手册",
        filename="其他.pdf",
        source_path=other_source,
        sha256="b" * 64,
    )
    other_image = tmp_path / "pages" / "2" / "page_0001.png"
    other_image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", PAGE_SIZE, "white").save(other_image)
    other_page = database.create_page(
        document_id=other_document.id,
        page_number=1,
        image_path=other_image,
        extracted_text="其他文档内容。",
        status=PageStatus.REVIEWED,
    )
    database.update_document_page_count(other_document.id, 1)

    with pytest.raises(EvidenceSourceError, match="文档记录不存在"):
        service.add_page_item(basket.id, 9999, page_ids[0])
    with pytest.raises(EvidenceSourceError, match="页面记录不存在"):
        service.add_page_item(basket.id, document_id, 9999)
    with pytest.raises(EvidenceBasketError, match="证据篮 9999 不存在"):
        service.add_page_item(9999, document_id, page_ids[0])
    with pytest.raises(EvidenceSourceError, match="所属文档不一致"):
        service.add_page_item(basket.id, document_id, other_page.id)

    missing_image_page = page_ids[1]
    _page_image_path(database, missing_image_page).unlink()
    with pytest.raises(EvidenceSourceError, match="页面图像缺失"):
        service.add_page_item(basket.id, document_id, missing_image_page)
    with pytest.raises(EvidenceSourceError, match="页面图像缺失"):
        service.add_region_item(
            basket.id, document_id, missing_image_page, x0=0, y0=0, x1=5, y1=5
        )

    assert service.list_items() == []
    with sqlite3.connect(database.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0]
    assert count == 0


# --- T6 引用完整性 ---------------------------------------------------------------


def test_document_delete_cascades_all_evidence_types(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    basket = service.default_basket()
    service.add_page_item(basket.id, document_id, page_ids[0])
    service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    service.add_region_item(basket.id, document_id, page_ids[1], x0=1, y0=1, x1=9, y1=9)
    assert len(service.list_items()) == 3

    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        connection.commit()
        remaining = connection.execute(
            "SELECT COUNT(*) FROM evidence_items"
        ).fetchone()[0]
    assert remaining == 0
