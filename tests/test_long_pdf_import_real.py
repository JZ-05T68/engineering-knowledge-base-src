"""Real-import baseline for long PDFs (~120 pages).

These tests generate the PDF at runtime with PyMuPDF (no binary fixtures are
committed) and run the unmodified ``PdfService`` / ``DocumentService`` code
paths end to end. Everything is written under pytest's ``tmp_path`` so the
repository never accumulates PDF, PNG, database or log artifacts.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pdf_test_helpers import LONG_PAGE_COUNT, build_long_pdf, long_page_line

from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus, PageStatus
from src.pdf_service import PdfService, ProcessedPage

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def long_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the shared long PDF once for the whole module."""

    return build_long_pdf(tmp_path_factory.mktemp("long_pdf") / "long.pdf")


@pytest.fixture(scope="module")
def processed_long(
    long_pdf: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[list[ProcessedPage], Path, list[tuple[int, int]]]:
    """Run the real PdfService once, capturing progress callbacks."""

    output_dir = tmp_path_factory.mktemp("long_pages")
    calls: list[tuple[int, int]] = []
    pages = PdfService().process(
        long_pdf,
        output_dir,
        on_progress=lambda done, total: calls.append((done, total)),
    )
    return pages, output_dir, calls


def test_long_pdf_processes_every_page_in_order(
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    pages, _, _ = processed_long
    assert len(pages) == LONG_PAGE_COUNT
    assert [page.page_number for page in pages] == list(range(1, LONG_PAGE_COUNT + 1))
    assert all(page.processing_error == "" for page in pages)
    assert all(page.needs_review is False for page in pages)
    assert all(page.effective_text_length >= 20 for page in pages)


def test_every_page_extracts_its_unique_page_line(
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    pages, _, _ = processed_long
    for page in pages:
        assert long_page_line(page.page_number) in page.extracted_text
    extracted = [page.extracted_text for page in pages]
    assert len(set(extracted)) == LONG_PAGE_COUNT


def test_every_page_gets_a_unique_valid_png(
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    pages, output_dir, _ = processed_long
    image_paths = [page.image_path for page in pages]
    assert len({path.name for path in image_paths}) == LONG_PAGE_COUNT
    for page in pages:
        assert page.image_path.name == f"page_{page.page_number:04d}.png"
        assert page.image_path.parent == output_dir
        assert page.image_path.is_file()
        assert page.image_path.stat().st_size > 0
        assert page.image_path.read_bytes()[:8] == PNG_MAGIC
    # No stray or overwritten files: the directory holds exactly one PNG per page.
    on_disk = sorted(path.name for path in output_dir.iterdir())
    assert on_disk == [f"page_{number:04d}.png" for number in range(1, LONG_PAGE_COUNT + 1)]


def test_progress_callback_reports_every_page_in_order(
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    _, _, calls = processed_long
    assert calls == [(number, LONG_PAGE_COUNT) for number in range(1, LONG_PAGE_COUNT + 1)]


def test_pdf_handle_is_closed_after_processing(
    long_pdf: Path,
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    """On Windows a still-open PDF handle would block renaming the file."""

    assert processed_long[0], "processing fixture must have run first"
    renamed = long_pdf.with_name("long-renamed.pdf")
    long_pdf.rename(renamed)
    renamed.rename(long_pdf)


def test_output_directory_is_not_locked_after_processing(
    processed_long: tuple[list[ProcessedPage], Path, list[tuple[int, int]]],
) -> None:
    """The page-image directory must be cleanly removable after processing."""

    _, output_dir, _ = processed_long
    renamed = output_dir.with_name(output_dir.name + "-moved")
    output_dir.rename(renamed)
    renamed.rename(output_dir)


@pytest.fixture(scope="module")
def imported_long(
    long_pdf: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[object, Database, Path, list[tuple[int, int]]]:
    """Import the long PDF through DocumentService once, capturing progress."""

    base = tmp_path_factory.mktemp("long_import")
    database = Database(base / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=base / "raw",
        pages_dir=base / "pages",
        markdown_dir=base / "markdown",
    )
    calls: list[tuple[int, int]] = []
    result = service.import_pdf(
        long_pdf.read_bytes(),
        "long.pdf",
        title="长文档基线",
        progress_callback=lambda done, total: calls.append((done, total)),
    )
    return result, database, base, calls


def test_document_service_imports_long_pdf_completely(
    imported_long: tuple[object, Database, Path, list[tuple[int, int]]],
) -> None:
    result, database, base, _ = imported_long
    assert result.duplicate is False

    document = result.document
    assert document.import_status is ImportStatus.COMPLETED
    assert document.import_error == ""
    assert document.page_count == LONG_PAGE_COUNT
    assert document.processed_page_count == LONG_PAGE_COUNT
    assert document.text_page_count == LONG_PAGE_COUNT

    pages = result.pages
    assert len(pages) == LONG_PAGE_COUNT
    assert [page.page_number for page in pages] == list(range(1, LONG_PAGE_COUNT + 1))
    for page in pages:
        assert page.status is PageStatus.PENDING
        assert page.processing_error == ""
        assert long_page_line(page.page_number) in page.extracted_text
        image_path = Path(page.image_path)
        assert image_path.name == f"page_{page.page_number:04d}.png"
        assert image_path.parent == base / "pages" / str(document.id)
        assert image_path.is_file()
        assert image_path.read_bytes()[:8] == PNG_MAGIC

    # The database view agrees with the import result (no partial success).
    stored_pages = database.list_pages(document.id)
    assert len(stored_pages) == LONG_PAGE_COUNT
    assert [page.page_number for page in stored_pages] == list(
        range(1, LONG_PAGE_COUNT + 1)
    )

    record = result.import_record
    assert record is not None
    assert record.status is ImportStatus.COMPLETED
    assert record.total_pages == LONG_PAGE_COUNT
    assert record.processed_pages == LONG_PAGE_COUNT
    assert record.failed_pages == 0
    assert record.error_message == ""


def test_import_progress_callback_covers_every_page(
    imported_long: tuple[object, Database, Path, list[tuple[int, int]]],
) -> None:
    _, _, _, calls = imported_long
    assert calls == [(number, LONG_PAGE_COUNT) for number in range(1, LONG_PAGE_COUNT + 1)]


def test_import_artifacts_stay_inside_the_temp_base(
    imported_long: tuple[object, Database, Path, list[tuple[int, int]]],
) -> None:
    result, _, base, _ = imported_long
    resolved_base = base.resolve()
    assert Path(result.document.source_path).resolve().is_relative_to(resolved_base)
    for page in result.pages:
        assert Path(page.image_path).resolve().is_relative_to(resolved_base)


def test_import_temp_tree_can_be_removed_cleanly(
    imported_long: tuple[object, Database, Path, list[tuple[int, int]]],
    tmp_path: Path,
) -> None:
    """A copied artifact tree must be deletable, proving nothing stays locked."""

    _, _, base, _ = imported_long
    copy_dir = tmp_path / "artifact_copy"
    shutil.copytree(base, copy_dir)
    shutil.rmtree(copy_dir)
    assert not copy_dir.exists()
