"""Unit tests for single-page PdfService processing with PyMuPDF fakes."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_document_service import FakePyMuPdf, FakePyMuPdfDocument

import src.pdf_service as pdf_service_module
from src.pdf_service import PdfProcessingError, PdfService


def _service_with_fake_document(
    monkeypatch: pytest.MonkeyPatch, document: FakePyMuPdfDocument
) -> PdfService:
    monkeypatch.setattr(
        pdf_service_module,
        "_load_pymupdf",
        lambda: FakePyMuPdf(document),
    )
    return PdfService(minimum_text_length=4, dpi=144)


def test_process_page_loads_only_the_requested_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    fake_document = FakePyMuPdfDocument()
    service = _service_with_fake_document(monkeypatch, fake_document)

    page = service.process_page(source_path, tmp_path / "rendered", 2)

    assert fake_document.load_page_calls == [1]
    assert page.page_number == 2
    assert page.extracted_text == "--"
    assert page.needs_review is True
    assert page.effective_text_length == 0
    assert page.processing_error == ""
    assert page.image_path.name == "page_0002.png"
    assert page.image_path.read_bytes() == b"png-2"
    assert not (tmp_path / "rendered" / "page_0001.png").exists()
    assert fake_document.closed is True


def test_process_page_reuses_existing_image_without_rerendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    fake_document = FakePyMuPdfDocument()
    service = _service_with_fake_document(monkeypatch, fake_document)
    output_dir = tmp_path / "rendered"

    first = service.process_page(source_path, output_dir, 1)
    assert first.image_path.read_bytes() == b"png-1"

    # Simulate a pre-existing page image: it must not be overwritten.
    first.image_path.write_bytes(b"user edited png")
    resumed = service.process_page(source_path, output_dir, 1, reuse_existing=True)
    assert resumed.image_path.read_bytes() == b"user edited png"
    assert resumed.extracted_text == "A B C 123"
    assert resumed.needs_review is False

    with pytest.raises(PdfProcessingError, match="已存在"):
        service.process_page(source_path, output_dir, 1)
    assert first.image_path.read_bytes() == b"user edited png"


def test_process_page_rejects_out_of_range_page_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    fake_document = FakePyMuPdfDocument()
    service = _service_with_fake_document(monkeypatch, fake_document)

    with pytest.raises(PdfProcessingError, match="超出范围"):
        service.process_page(source_path, tmp_path / "low", 0)
    with pytest.raises(PdfProcessingError, match="超出范围"):
        service.process_page(source_path, tmp_path / "high", 3)

    assert fake_document.load_page_calls == []
    assert not (tmp_path / "low").exists()
    assert not (tmp_path / "high").exists()
    assert fake_document.closed is True
