"""Test-only helpers that build small real PDF files with PyMuPDF.

The generated documents exercise the real ``PdfService`` code path (open,
render, extract) without committing binary fixtures to the repository.
All files are written under pytest's ``tmp_path`` and never into the repo.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

PAGE_COUNT = 10
NORMAL_PAGES: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 9, 10)
BLANK_PAGE = 7
SHORT_TEXT_PAGE = 8
ROTATED_PAGE = 9
WIDE_PAGE = 10

SHORT_PAGE_TEXT = "-- --"


def png_bytes_for(seed: bytes) -> bytes:
    """Return deterministic, real, decodable PNG bytes derived from ``seed``.

    Fake renderers use these bytes so production code that verifies page-image
    decodability exercises a realistic path in unit tests.
    """

    digest = hashlib.sha256(seed).digest()
    size = 8
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    for index in range(size * size):
        pixmap.set_pixel(
            index % size,
            index // size,
            (digest[index % 32], digest[(index + 7) % 32], digest[(index + 13) % 32]),
        )
    return pixmap.tobytes("png")


def open_image(path: Path | str) -> pymupdf.Pixmap:
    """Open an image file with real PyMuPDF (decode verification helper)."""

    return pymupdf.Pixmap(str(path))


def assert_decodable_png(path: Path) -> None:
    """Assert the file is non-empty and fully decodable as an image."""

    assert Path(path).is_file(), f"缺少图片文件：{path}"
    assert Path(path).stat().st_size > 0, f"图片文件为空：{path}"
    pixmap = open_image(path)
    assert pixmap.width > 0 and pixmap.height > 0, f"图片不可解码：{path}"


def expected_page_line(page_number: int) -> str:
    """Return the verifiable page-number line written into a normal page."""

    return f"Page {page_number} of {PAGE_COUNT}."


def _normal_page_text(page_number: int) -> str:
    return f"{expected_page_line(page_number)} Normal text page for real PyMuPDF coverage."


LONG_PAGE_COUNT = 120
# A few pages use a landscape size and one page is rotated so the long
# document still covers differing page dimensions and orientations.
LONG_LANDSCAPE_PAGES: tuple[int, ...] = (30, 60, 90, 120)
LONG_ROTATED_PAGE = 45


def long_page_line(page_number: int) -> str:
    """Return the unique, verifiable line written into each long-PDF page."""

    return (
        f"Engineering long document baseline page {page_number} "
        f"of {LONG_PAGE_COUNT}."
    )


def build_long_pdf(path: Path | str, page_count: int = LONG_PAGE_COUNT) -> Path:
    """Create a long text PDF with a few landscape/rotated pages mixed in."""

    pdf_path = Path(path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        for page_number in range(1, page_count + 1):
            if page_number in LONG_LANDSCAPE_PAGES:
                page = document.new_page(width=842, height=595)
            else:
                page = document.new_page(width=595, height=842)
            page.insert_text((72, 72), long_page_line(page_number), fontsize=12)
            if page_number == LONG_ROTATED_PAGE:
                page.set_rotation(90)

        document.save(str(pdf_path))
    finally:
        document.close()
    return pdf_path


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


DIAG_PAGE_COUNT = 6
DIAG_NORMAL_PAGE = 1
DIAG_BLANK_PAGE = 2
DIAG_SHORT_PAGE = 3
DIAG_LANDSCAPE_PAGE = 4
DIAG_ROTATED_PAGE = 5
DIAG_MIXED_PAGE = 6
DIAG_ROTATION = 90
DIAG_SHORT_TEXT = "abc 12"


def diag_page_line(page_number: int) -> str:
    """Return the verifiable line written into a normal diagnostics page."""

    return f"Diagnostics sample page {page_number} of {DIAG_PAGE_COUNT} body text."


def build_diagnostics_pdf(path: Path | str) -> Path:
    """Create a 6-page PDF covering normal/blank/short/landscape/rotated pages.

    The mixed page combines a short text layer with rotation, and its display
    geometry becomes landscape after rotation, so it carries three flags.
    """

    pdf_path = Path(path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        for page_number in range(1, DIAG_PAGE_COUNT + 1):
            if page_number == DIAG_LANDSCAPE_PAGE:
                page = document.new_page(width=842, height=595)
            else:
                page = document.new_page(width=595, height=842)

            if page_number == DIAG_BLANK_PAGE:
                continue
            if page_number in (DIAG_SHORT_PAGE, DIAG_MIXED_PAGE):
                page.insert_text((72, 72), DIAG_SHORT_TEXT, fontsize=12)
            else:
                page.insert_text((72, 72), diag_page_line(page_number), fontsize=12)
            if page_number in (DIAG_ROTATED_PAGE, DIAG_MIXED_PAGE):
                page.set_rotation(DIAG_ROTATION)

        document.save(str(pdf_path))
    finally:
        document.close()
    return pdf_path
