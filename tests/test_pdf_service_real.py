"""Regression tests that run the real PyMuPDF path against generated PDFs.

Unlike ``tests/test_document_service.py`` (which uses fakes), these tests open,
render and extract a real PDF produced at runtime by ``pdf_test_helpers``.
They establish a baseline for later long-document work without changing any
production code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pdf_test_helpers import (
    BLANK_PAGE,
    NORMAL_PAGES,
    PAGE_COUNT,
    ROTATED_PAGE,
    SHORT_PAGE_TEXT,
    SHORT_TEXT_PAGE,
    WIDE_PAGE,
    build_sample_pdf,
    expected_page_line,
)

from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus, PageStatus
from src.pdf_service import PdfService, ProcessedPage

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> Path:
    """Build the shared sample PDF inside the test's temporary directory."""

    return build_sample_pdf(tmp_path / "sample.pdf")


@pytest.fixture()
def processed(sample_pdf: Path, tmp_path: Path) -> tuple[list[ProcessedPage], Path]:
    """Run the real PdfService once and share the result across assertions."""

    output_dir = tmp_path / "pages"
    service = PdfService()
    return service.process(sample_pdf, output_dir), output_dir


def _page_by_number(pages: list[ProcessedPage], page_number: int) -> ProcessedPage:
    return next(page for page in pages if page.page_number == page_number)


def test_process_opens_real_pdf_and_counts_pages(
    processed: tuple[list[ProcessedPage], Path],
) -> None:
    pages, _ = processed
    assert len(pages) == PAGE_COUNT
    assert [page.page_number for page in pages] == list(range(1, PAGE_COUNT + 1))


def test_every_page_gets_a_png_inside_output_dir(
    processed: tuple[list[ProcessedPage], Path],
) -> None:
    pages, output_dir = processed
    resolved_output = output_dir.resolve()
    for page in pages:
        image_path = page.image_path
        assert image_path.name == f"page_{page.page_number:04d}.png"
        assert image_path.parent == output_dir
        assert image_path.resolve().is_relative_to(resolved_output)
        assert image_path.is_file()
        assert image_path.stat().st_size > 0
        assert image_path.read_bytes()[:8] == PNG_MAGIC


def test_normal_pages_extract_expected_text(
    processed: tuple[list[ProcessedPage], Path],
) -> None:
    pages, _ = processed
    for page_number in NORMAL_PAGES:
        page = _page_by_number(pages, page_number)
        assert expected_page_line(page_number) in page.extracted_text
        assert page.needs_review is False
        assert page.effective_text_length >= 20
        assert page.processing_error == ""


def test_blank_page_needs_review(processed: tuple[list[ProcessedPage], Path]) -> None:
    page = _page_by_number(processed[0], BLANK_PAGE)
    assert page.extracted_text == ""
    assert page.effective_text_length == 0
    assert page.needs_review is True
    assert page.processing_error == ""


def test_short_text_page_needs_review(
    processed: tuple[list[ProcessedPage], Path],
) -> None:
    page = _page_by_number(processed[0], SHORT_TEXT_PAGE)
    assert page.extracted_text == SHORT_PAGE_TEXT
    assert page.effective_text_length < 20
    assert page.needs_review is True
    assert page.processing_error == ""


def test_rotation_and_page_size_change_do_not_break_processing(
    processed: tuple[list[ProcessedPage], Path],
) -> None:
    pages, _ = processed
    assert all(page.processing_error == "" for page in pages)

    rotated = _page_by_number(pages, ROTATED_PAGE)
    assert expected_page_line(ROTATED_PAGE) in rotated.extracted_text
    assert rotated.needs_review is False

    wide = _page_by_number(pages, WIDE_PAGE)
    assert expected_page_line(WIDE_PAGE) in wide.extracted_text
    assert wide.needs_review is False

    # Pages after the rotated/wide pages are still rendered and extracted.
    following = _page_by_number(pages, WIDE_PAGE)
    assert following.image_path.is_file()


def test_progress_callback_covers_every_page_in_order(
    sample_pdf: Path, tmp_path: Path
) -> None:
    calls: list[tuple[int, int]] = []
    service = PdfService()
    service.process(
        sample_pdf,
        tmp_path / "progress_pages",
        on_progress=lambda done, total: calls.append((done, total)),
    )
    assert calls == [(number, PAGE_COUNT) for number in range(1, PAGE_COUNT + 1)]


def test_document_service_imports_real_pdf_end_to_end(
    sample_pdf: Path, tmp_path: Path
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )

    result = service.import_pdf(sample_pdf.read_bytes(), "sample.pdf", title="回归样例")

    assert result.duplicate is False
    document = result.document
    assert document.import_status is ImportStatus.COMPLETED
    assert document.import_error == ""
    assert document.page_count == PAGE_COUNT
    assert document.processed_page_count == PAGE_COUNT
    # The short-text page still carries a non-empty text layer ("-- --"), so it
    # is counted as a text page; only the blank page is not.
    assert document.text_page_count == PAGE_COUNT - 1
    # Every imported page starts in the manual review queue (PENDING).
    assert document.review_page_count == PAGE_COUNT

    pages = result.pages
    assert len(pages) == PAGE_COUNT
    assert [page.page_number for page in pages] == list(range(1, PAGE_COUNT + 1))
    for page in pages:
        assert page.status is PageStatus.PENDING
        assert page.processing_error == ""
        image_path = Path(page.image_path)
        assert image_path.is_file()
        assert image_path.resolve().is_relative_to(tmp_path.resolve())

    normal_page = next(page for page in pages if page.page_number == 1)
    assert expected_page_line(1) in normal_page.extracted_text
    blank_page = next(page for page in pages if page.page_number == BLANK_PAGE)
    assert blank_page.extracted_text == ""
    short_page = next(page for page in pages if page.page_number == SHORT_TEXT_PAGE)
    assert short_page.extracted_text == SHORT_PAGE_TEXT

    record = result.import_record
    assert record is not None
    assert record.status is ImportStatus.COMPLETED
    assert record.total_pages == PAGE_COUNT
    assert record.processed_pages == PAGE_COUNT
    assert record.failed_pages == 0
    assert record.error_message == ""
