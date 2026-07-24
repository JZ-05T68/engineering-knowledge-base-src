"""Test-only helpers that build small real PDF files with PyMuPDF.

The generated documents exercise the real ``PdfService`` code path (open,
render, extract) without committing binary fixtures to the repository.
All files are written under pytest's ``tmp_path`` and never into the repo.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

PAGE_COUNT = 10
NORMAL_PAGES: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 9, 10)
BLANK_PAGE = 7
SHORT_TEXT_PAGE = 8
ROTATED_PAGE = 9
WIDE_PAGE = 10

SHORT_PAGE_TEXT = "-- --"


def expected_page_line(page_number: int) -> str:
    """Return the verifiable page-number line written into a normal page."""

    return f"Page {page_number} of {PAGE_COUNT}."


def _normal_page_text(page_number: int) -> str:
    return f"{expected_page_line(page_number)} Normal text page for real PyMuPDF coverage."


def build_sample_pdf(path: Path | str) -> Path:
    """Create a 10-page PDF mixing normal, blank, short, rotated and wide pages."""

    pdf_path = Path(path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        for page_number in range(1, PAGE_COUNT + 1):
            if page_number == WIDE_PAGE:
                page = document.new_page(width=842, height=595)
            else:
                page = document.new_page(width=595, height=842)

            if page_number == BLANK_PAGE:
                continue
            if page_number == SHORT_TEXT_PAGE:
                page.insert_text((72, 72), SHORT_PAGE_TEXT, fontsize=12)
                continue

            page.insert_text((72, 72), _normal_page_text(page_number), fontsize=12)
            if page_number == ROTATED_PAGE:
                page.set_rotation(90)

        document.save(str(pdf_path))
    finally:
        document.close()
    return pdf_path
