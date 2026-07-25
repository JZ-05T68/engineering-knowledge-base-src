"""Real RapidOCR tests: actual models, actual recognition, fully offline.

These tests load the real, locally installed RapidOCR engine exactly once
per test session and recognize generated temporary images. Network access
is hard-blocked during initialization and recognition to prove the engine
never downloads models or contacts any service. No repository files,
formal data, or formal databases are touched.
"""

from __future__ import annotations

import hashlib
import socket
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw, ImageFont

from src.database import Database
from src.document_service import DocumentService, PageOcrOutcome
from src.evidence_basket_service import EvidenceBasketService
from src.evidence_service import OCR_EVIDENCE_WARNING
from src.models import PageStatus, SearchField
from src.pdf_service import PdfService
from src.rapidocr_engine import RapidOcrEngine
from src.search_service import SearchService

TEST_TEXT = "STM32H750 ADC TEST 21MPa"


def _blocked_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("OCR 不得访问网络")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-block outbound connections and HTTP fetches for one test."""

    monkeypatch.setattr(socket.socket, "connect", _blocked_network)
    monkeypatch.setattr(socket, "create_connection", _blocked_network)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_network)


@pytest.fixture(scope="module")
def real_engine() -> RapidOcrEngine:
    """One shared real engine so the models initialize only once."""

    return RapidOcrEngine()


def _make_test_image(path: Path, text: str = TEST_TEXT) -> Path:
    """Render a high-contrast black-on-white test image in ``tmp_path``."""

    image = Image.new("RGB", (640, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 60), text, fill="black")
    image.save(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_recognition_is_offline_and_finds_stable_markers(
    tmp_path: Path, no_network: None, real_engine: RapidOcrEngine
) -> None:
    image_path = _make_test_image(tmp_path / "ocr-real.png")
    before = _sha256(image_path)

    text = real_engine.recognize(image_path)

    assert isinstance(text, str)
    assert text.strip()
    compact = text.replace(" ", "").upper()
    assert "STM32" in compact
    assert "ADC" in compact
    assert _sha256(image_path) == before


def test_real_recognition_returns_empty_string_for_blank_image(
    tmp_path: Path, no_network: None, real_engine: RapidOcrEngine
) -> None:
    blank_path = tmp_path / "blank.png"
    Image.new("RGB", (320, 120), "white").save(blank_path)

    assert real_engine.recognize(blank_path) == ""


def test_real_engine_document_service_roundtrip(
    tmp_path: Path, no_network: None, real_engine: RapidOcrEngine
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "real-ocr.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"local pdf bytes")
    document = database.create_document(
        title="真实 OCR 闭环测试",
        filename="real-ocr.pdf",
        source_path=source_path,
        sha256=uuid4().hex * 2,
    )
    database.update_document_page_count(document.id, 1)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(parents=True)
    image_path = _make_test_image(pages_dir / "page_0001.png")
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
    )
    database.update_page(
        page.id, extracted_text="", markdown_content="人工笔记保持不变"
    )
    before_hash = _sha256(image_path)
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        pdf_service=PdfService(),
        ocr_engine=real_engine,
    )

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.processing_status == "ocr_completed"
    compact = persisted.ocr_text.replace(" ", "").upper()
    assert "STM32" in compact
    assert persisted.extracted_text == ""
    assert persisted.markdown_content == "人工笔记保持不变"
    assert persisted.status is PageStatus.PENDING
    assert _sha256(image_path) == before_hash

    results = SearchService(database).search("STM32")
    assert [item.page_id for item in results] == [page.id]
    assert SearchField.OCR_TEXT in results[0].match_fields

    basket = EvidenceBasketService(database)
    basket.add_item(
        document_id=document.id,
        page_id=page.id,
        evidence_text=persisted.ocr_text.strip(),
    )
    assert OCR_EVIDENCE_WARNING in basket.export_markdown()


def test_chinese_smoke_when_system_font_available(
    tmp_path: Path, no_network: None, real_engine: RapidOcrEngine
) -> None:
    """Non-blocking Chinese smoke check; skipped without a system font."""

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.exists():
        pytest.skip("系统中文字体不可用，跳过中文 smoke")
    image = Image.new("RGB", (640, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 50), "本地OCR测试", fill="black", font=ImageFont.truetype(str(font_path), 48))
    image_path = tmp_path / "chinese-smoke.png"
    image.save(image_path)

    text = real_engine.recognize(image_path)

    print(f"中文 smoke 识别结果：{text!r}")
    assert isinstance(text, str)
    assert text.strip()
