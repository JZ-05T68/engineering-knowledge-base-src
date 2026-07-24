"""Regression tests that run the real PyMuPDF path against generated PDFs.

Unlike ``tests/test_document_service.py`` (which uses fakes), these tests open,
render and extract a real PDF produced at runtime by ``pdf_test_helpers``.
They establish a baseline for later long-document work without changing any
production code.
"""

from __future__ import annotations

import hashlib
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
from src.pdf_service import PdfProcessingError, PdfService, ProcessedPage

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


def test_process_page_handles_a_single_normal_page(
    sample_pdf: Path, tmp_path: Path
) -> None:
    output_dir = tmp_path / "single"
    page = PdfService().process_page(sample_pdf, output_dir, 3)

    assert page.page_number == 3
    assert expected_page_line(3) in page.extracted_text
    assert page.needs_review is False
    assert page.effective_text_length >= 20
    assert page.processing_error == ""
    assert page.image_path.name == "page_0003.png"
    assert page.image_path.is_file()
    assert page.image_path.stat().st_size > 0
    assert page.image_path.read_bytes()[:8] == PNG_MAGIC
    # Only the requested page image is produced.
    assert [path.name for path in output_dir.iterdir()] == ["page_0003.png"]


def test_process_page_blank_page_needs_review(
    sample_pdf: Path, tmp_path: Path
) -> None:
    page = PdfService().process_page(sample_pdf, tmp_path / "single", BLANK_PAGE)

    assert page.page_number == BLANK_PAGE
    assert page.extracted_text == ""
    assert page.effective_text_length == 0
    assert page.needs_review is True
    assert page.processing_error == ""
    assert page.image_path.read_bytes()[:8] == PNG_MAGIC


def test_process_page_short_text_page_needs_review(
    sample_pdf: Path, tmp_path: Path
) -> None:
    page = PdfService().process_page(sample_pdf, tmp_path / "single", SHORT_TEXT_PAGE)

    assert page.page_number == SHORT_TEXT_PAGE
    assert page.extracted_text == SHORT_PAGE_TEXT
    assert page.effective_text_length < 20
    assert page.needs_review is True
    assert page.processing_error == ""


def test_process_page_rotated_page(sample_pdf: Path, tmp_path: Path) -> None:
    page = PdfService().process_page(sample_pdf, tmp_path / "single", ROTATED_PAGE)

    assert page.page_number == ROTATED_PAGE
    assert expected_page_line(ROTATED_PAGE) in page.extracted_text
    assert page.needs_review is False
    assert page.processing_error == ""
    assert page.image_path.read_bytes()[:8] == PNG_MAGIC


def test_process_page_reuse_existing_keeps_image_and_extracts_text(
    sample_pdf: Path, tmp_path: Path
) -> None:
    output_dir = tmp_path / "single"
    service = PdfService()
    first = service.process_page(sample_pdf, output_dir, 3)

    # Replace the image with sentinel bytes: reuse_existing must not re-render.
    first.image_path.write_bytes(b"sentinel image bytes")
    resumed = service.process_page(sample_pdf, output_dir, 3, reuse_existing=True)

    assert resumed.image_path.read_bytes() == b"sentinel image bytes"
    assert expected_page_line(3) in resumed.extracted_text
    assert resumed.processing_error == ""

    with pytest.raises(PdfProcessingError, match="已存在"):
        service.process_page(sample_pdf, output_dir, 3)
    assert first.image_path.read_bytes() == b"sentinel image bytes"


def test_process_page_rejects_out_of_range_page_numbers(
    sample_pdf: Path, tmp_path: Path
) -> None:
    service = PdfService()

    with pytest.raises(PdfProcessingError, match="超出范围"):
        service.process_page(sample_pdf, tmp_path / "low", 0)
    with pytest.raises(PdfProcessingError, match="超出范围"):
        service.process_page(sample_pdf, tmp_path / "high", PAGE_COUNT + 1)

    assert not (tmp_path / "low").exists()
    assert not (tmp_path / "high").exists()


def _page_snapshot(page: object) -> tuple[object, ...]:
    return (
        page.extracted_text,
        page.ocr_text,
        page.markdown_content,
        page.status,
        page.processing_status,
        page.processing_error,
        str(page.image_path),
    )


def test_reprocess_page_only_touches_the_target_page(
    sample_pdf: Path, tmp_path: Path
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )

    result = service.import_pdf(sample_pdf.read_bytes(), "sample.pdf", title="重处理样例")
    document = result.document
    pages_before = {page.page_number: page for page in database.list_pages(document.id)}
    snapshots_before = {
        number: _page_snapshot(page) for number, page in pages_before.items()
    }
    hashes_before = {
        number: hashlib.sha256(Path(page.image_path).read_bytes()).hexdigest()
        for number, page in pages_before.items()
    }

    target = pages_before[BLANK_PAGE]
    updated = service.reprocess_page(target.id)

    assert updated.id == target.id
    assert updated.status is PageStatus.PENDING
    assert updated.processing_status == "pending_review"
    assert updated.processing_error == ""
    assert updated.extracted_text == ""
    # The target image is reused (not re-rendered) under reuse_existing.
    assert (
        hashlib.sha256(Path(updated.image_path).read_bytes()).hexdigest()
        == hashes_before[BLANK_PAGE]
    )

    pages_after = {page.page_number: page for page in database.list_pages(document.id)}
    assert set(pages_after) == set(pages_before)
    for number, snapshot in snapshots_before.items():
        if number == BLANK_PAGE:
            continue
        assert _page_snapshot(pages_after[number]) == snapshot
        assert (
            hashlib.sha256(
                Path(pages_after[number].image_path).read_bytes()
            ).hexdigest()
            == hashes_before[number]
        )

    reloaded = database.get_document(document.id)
    assert reloaded.page_count == PAGE_COUNT
    assert reloaded.import_status is ImportStatus.COMPLETED
    assert reloaded.import_error == ""
