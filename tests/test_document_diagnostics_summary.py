"""Document-level diagnostics summary tests.

Covers the pure aggregation over ``ProcessedPage`` results, the real
6-page diagnostics PDF, and a partially failed import, ensuring failed
pages are never miscounted as blank and multi-flag pages appear in every
matching page-number list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pdf_test_helpers import build_diagnostics_pdf
from test_pdf_page_failure_isolation import (
    FAILING_PAGE,
    FAILING_PAGE_COUNT,
    _IsolationDocument,
    _IsolationModule,
)

import src.pdf_service as pdf_service_module
from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus
from src.pdf_service import (
    DocumentDiagnosticsSummary,
    PageDiagnostics,
    PdfService,
    ProcessedPage,
    summarize_page_diagnostics,
)


def _page(
    number: int,
    *,
    error: str = "",
    needs_review: bool = False,
    diagnostics: PageDiagnostics | None = None,
) -> ProcessedPage:
    return ProcessedPage(
        page_number=number,
        image_path=Path(f"page_{number:04d}.png"),
        extracted_text="",
        needs_review=needs_review,
        effective_text_length=0,
        processing_error=error,
        diagnostics=diagnostics if diagnostics is not None else PageDiagnostics(),
    )


def test_summary_aggregates_constructed_pages() -> None:
    normal = PageDiagnostics(
        width=595.0, height=842.0, effective_char_count=50, is_blank=False
    )
    blank = PageDiagnostics(
        width=595.0, height=842.0, effective_char_count=0, is_blank=True
    )
    short = PageDiagnostics(
        width=595.0,
        height=842.0,
        effective_char_count=5,
        is_blank=False,
        is_short_text=True,
    )
    landscape = PageDiagnostics(
        width=842.0, height=595.0, effective_char_count=50, is_blank=False,
        is_landscape=True,
    )
    rotated = PageDiagnostics(
        width=842.0, height=595.0, rotation=90, effective_char_count=50,
        is_blank=False, is_landscape=True, is_rotated=True,
    )
    mixed = PageDiagnostics(
        width=842.0, height=595.0, rotation=90, effective_char_count=5,
        is_blank=False, is_short_text=True, is_landscape=True, is_rotated=True,
    )
    pages = [
        _page(1, diagnostics=normal),
        _page(2, needs_review=True, diagnostics=blank),
        _page(3, needs_review=True, diagnostics=short),
        _page(4, diagnostics=landscape),
        _page(5, diagnostics=rotated),
        _page(6, needs_review=True, diagnostics=mixed),
        # A failed page keeps default diagnostics (is_blank=True) and a
        # default needs_review=True; neither may leak into the statistics.
        _page(7, error="第 7 页处理失败：boom", needs_review=True),
    ]

    summary = summarize_page_diagnostics(pages)

    assert summary.total_pages == 7
    assert summary.successful_pages == 6
    assert summary.failed_pages == 1
    assert summary.failed_page_numbers == (7,)
    assert summary.blank_pages == 1
    assert summary.blank_page_numbers == (2,)
    assert summary.short_text_pages == 2
    assert summary.short_text_page_numbers == (3, 6)
    assert summary.landscape_pages == 3
    assert summary.landscape_page_numbers == (4, 5, 6)
    assert summary.rotated_pages == 2
    assert summary.rotated_page_numbers == (5, 6)
    assert summary.needs_review_pages == 3
    assert summary.needs_review_page_numbers == (2, 3, 6)

    # Multi-flag page 6 appears in every matching list.
    assert 6 in summary.short_text_page_numbers
    assert 6 in summary.landscape_page_numbers
    assert 6 in summary.rotated_page_numbers
    assert 6 in summary.needs_review_page_numbers

    # Lists are sorted, duplicate-free, and consistent with the counts.
    for numbers, count in (
        (summary.failed_page_numbers, summary.failed_pages),
        (summary.blank_page_numbers, summary.blank_pages),
        (summary.short_text_page_numbers, summary.short_text_pages),
        (summary.landscape_page_numbers, summary.landscape_pages),
        (summary.rotated_page_numbers, summary.rotated_pages),
        (summary.needs_review_page_numbers, summary.needs_review_pages),
    ):
        assert list(numbers) == sorted(set(numbers))
        assert len(numbers) == count

    # The input pages were not modified (frozen dataclasses stay intact).
    assert pages[6].processing_error == "第 7 页处理失败：boom"
    assert pages[6].diagnostics.is_blank is True
    assert len(pages) == 7


def test_summary_handles_empty_input() -> None:
    summary = summarize_page_diagnostics([])
    assert summary == DocumentDiagnosticsSummary()
    assert summary.total_pages == 0
    assert summary.failed_page_numbers == ()
    assert summary.needs_review_page_numbers == ()


def test_real_diagnostics_pdf_summary(tmp_path: Path) -> None:
    pdf_path = build_diagnostics_pdf(tmp_path / "diag.pdf")
    pages = PdfService().process(pdf_path, tmp_path / "pages")

    summary = summarize_page_diagnostics(pages)

    assert summary.total_pages == 6
    assert summary.successful_pages == 6
    assert summary.failed_pages == 0
    assert summary.failed_page_numbers == ()
    assert summary.blank_page_numbers == (2,)
    assert summary.short_text_page_numbers == (3, 6)
    # Page 4 is landscape; pages 5 and 6 display as landscape after rotation.
    assert summary.landscape_page_numbers == (4, 5, 6)
    assert summary.rotated_page_numbers == (5, 6)
    assert summary.needs_review_page_numbers == (2, 3, 6)


def test_partial_failure_import_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = _IsolationDocument(fail_text=True)
    monkeypatch.setattr(
        pdf_service_module,
        "_load_pymupdf",
        lambda: _IsolationModule(document),
    )
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )

    result = service.import_pdf(b"fake pdf bytes", "partial.pdf", title="部分失败")
    summary = result.diagnostics

    assert summary.total_pages == FAILING_PAGE_COUNT
    assert summary.successful_pages == FAILING_PAGE_COUNT - 1
    assert summary.failed_pages == 1
    assert summary.failed_page_numbers == (FAILING_PAGE,)
    # The failed page must not be miscounted as blank or short text.
    assert summary.blank_pages == 0
    assert summary.blank_page_numbers == ()
    assert summary.short_text_pages == 0
    # The summary agrees with the persisted document state: no fake success.
    assert result.document.import_status is ImportStatus.PARTIALLY_COMPLETED
    assert result.document.import_status is not ImportStatus.COMPLETED
