"""P0 regression tests: recovering interrupted PDF page rendering.

Covers the S3A finding: a killed import could leave a zero-byte or truncated
page PNG that blocked every retry, and resume-branch failures left the
document stuck in PROCESSING.  The fix renders into a verified temp file and
atomically replaces the final PNG, re-renders incomplete images on reuse, and
marks the document FAILED on catchable resume errors.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pdf_test_helpers import (
    DIAG_BLANK_PAGE,
    DIAG_LANDSCAPE_PAGE,
    DIAG_MIXED_PAGE,
    DIAG_ROTATED_PAGE,
    DIAG_SHORT_PAGE,
    assert_decodable_png,
    build_diagnostics_pdf,
    build_sample_pdf,
)

import src.pdf_service as pdf_service_module
from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus
from src.pdf_service import (
    TEMP_IMAGE_INFIX,
    PdfProcessingError,
    PdfService,
    is_complete_png,
)

GARBAGE_BYTES = b"\x89PNG-not-a-real-png-payload"


def _import_sample(tmp_path: Path):
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )
    source = build_sample_pdf(tmp_path / "sample.pdf")
    result = service.import_pdf(source.read_bytes(), source.name, title="样例")
    return database, service, source, result


def _temp_files(output_dir: Path) -> list[Path]:
    return list(output_dir.glob(f"*{TEMP_IMAGE_INFIX}*"))


def test_valid_existing_png_is_reused_untouched(tmp_path: Path) -> None:
    source = build_sample_pdf(tmp_path / "sample.pdf")
    output_dir = tmp_path / "pages"
    service = PdfService()
    first = service.process_page(source, output_dir, 1)
    original_bytes = first.image_path.read_bytes()
    original_mtime = first.image_path.stat().st_mtime_ns

    resumed = service.process_page(source, output_dir, 1, reuse_existing=True)

    assert resumed.processing_error == ""
    assert resumed.image_path.read_bytes() == original_bytes
    assert resumed.image_path.stat().st_mtime_ns == original_mtime
    assert _temp_files(output_dir) == []


@pytest.mark.parametrize("damage", ["zero", "garbage"])
def test_incomplete_png_is_rerendered_on_reuse(
    tmp_path: Path, damage: str
) -> None:
    source = build_sample_pdf(tmp_path / "sample.pdf")
    output_dir = tmp_path / "pages"
    service = PdfService()
    first = service.process_page(source, output_dir, 1)
    if damage == "zero":
        first.image_path.write_bytes(b"")
    else:
        first.image_path.write_bytes(GARBAGE_BYTES)
    assert not is_complete_png(first.image_path)

    resumed = service.process_page(source, output_dir, 1, reuse_existing=True)

    assert resumed.processing_error == ""
    assert_decodable_png(resumed.image_path)
    assert resumed.image_path.read_bytes() not in (b"", GARBAGE_BYTES)
    assert _temp_files(output_dir) == []


def test_stale_temp_image_does_not_block_retry(tmp_path: Path) -> None:
    source = build_sample_pdf(tmp_path / "sample.pdf")
    output_dir = tmp_path / "pages"
    output_dir.mkdir()
    # Simulate a killed run: temp leftovers, final PNG never appeared.
    (output_dir / f"page_0001.png{TEMP_IMAGE_INFIX}deadbeef01").write_bytes(b"partial")
    (output_dir / f"page_0001.png{TEMP_IMAGE_INFIX}deadbeef02").write_bytes(b"")

    processed = PdfService().process_page(source, output_dir, 1, reuse_existing=True)

    assert processed.processing_error == ""
    assert_decodable_png(processed.image_path)
    assert _temp_files(output_dir) == []


class _ExplodingPixmapPage:
    rect = SimpleNamespace(width=595.0, height=842.0)
    rotation = 0

    def get_pixmap(self, *, matrix: object, alpha: bool) -> object:
        raise RuntimeError("simulated render crash")

    def get_text(self, mode: str) -> str:
        return "some text"


class _ExplodingModule:
    def __init__(self) -> None:
        self.document = SimpleNamespace(
            needs_pass=False,
            page_count=1,
            load_page=lambda index: _ExplodingPixmapPage(),
            close=lambda: None,
        )

    @staticmethod
    def Matrix(x: float, y: float) -> tuple[float, float]:  # noqa: N802
        return (x, y)

    @staticmethod
    def Pixmap(path: str) -> object:  # noqa: N802
        from pdf_test_helpers import open_image

        return open_image(path)

    def open(self, path: str) -> object:
        return self.document


def test_render_failure_leaves_no_final_png_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    monkeypatch.setattr(
        pdf_service_module, "_load_pymupdf", lambda: _ExplodingModule()
    )
    output_dir = tmp_path / "pages"

    processed = PdfService().process_page(source_path, output_dir, 1)

    assert "render crash" in processed.processing_error
    assert not (output_dir / "page_0001.png").exists()
    assert _temp_files(output_dir) == []


class _GarbagePixmap:
    def tobytes(self, fmt: str) -> bytes:
        return GARBAGE_BYTES


class _GarbageTempPage:
    rect = SimpleNamespace(width=595.0, height=842.0)
    rotation = 0

    def get_pixmap(self, *, matrix: object, alpha: bool) -> object:
        return _GarbagePixmap()

    def get_text(self, mode: str) -> str:
        return "some text"


class _GarbageTempModule(_ExplodingModule):
    def __init__(self) -> None:
        self.document = SimpleNamespace(
            needs_pass=False,
            page_count=1,
            load_page=lambda index: _GarbageTempPage(),
            close=lambda: None,
        )


def test_failed_temp_verification_keeps_existing_formal_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    monkeypatch.setattr(
        pdf_service_module, "_load_pymupdf", lambda: _GarbageTempModule()
    )
    output_dir = tmp_path / "pages"
    output_dir.mkdir()
    formal = output_dir / "page_0001.png"
    formal.write_bytes(GARBAGE_BYTES)  # pre-existing corrupt formal image

    processed = PdfService().process_page(source_path, output_dir, 1, reuse_existing=True)

    # The new render could not be verified either: the old file must survive
    # untouched (never replaced by unverified bytes, never deleted first).
    assert processed.processing_error != ""
    assert formal.read_bytes() == GARBAGE_BYTES
    assert _temp_files(output_dir) == []


def test_atomic_replace_produces_decodable_png(tmp_path: Path) -> None:
    source = build_sample_pdf(tmp_path / "sample.pdf")
    output_dir = tmp_path / "pages"

    processed = PdfService().process_page(source, output_dir, 2)

    assert processed.processing_error == ""
    assert_decodable_png(processed.image_path)
    assert _temp_files(output_dir) == []


def test_catchable_import_failure_marks_document_failed(tmp_path: Path) -> None:
    database, service, source, result = _import_sample(tmp_path)
    assert result.document.import_status is ImportStatus.COMPLETED
    # Force the resume branch: page_count mismatch + unreadable stored PDF.
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("UPDATE documents SET page_count = 0")
    Path(result.document.source_path).write_bytes(b"corrupted pdf bytes")

    with pytest.raises(PdfProcessingError):
        service.import_pdf(source.read_bytes(), source.name)

    stored = database.get_document_by_sha256(result.document.sha256)
    assert stored is not None
    assert stored.import_status is ImportStatus.FAILED
    assert stored.import_status is not ImportStatus.COMPLETED
    assert stored.import_error != ""


def test_retry_after_damage_recovers_consistent_counts(tmp_path: Path) -> None:
    database, service, source, result = _import_sample(tmp_path)
    document_id = result.document.id
    pages_dir = tmp_path / "pages" / str(document_id)
    victim = pages_dir / "page_0003.png"
    victim.write_bytes(b"")  # interrupted-write leftover
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("UPDATE documents SET page_count = 0")
        connection.execute("DELETE FROM pages WHERE page_number > 2")

    retry = service.import_pdf(source.read_bytes(), source.name)

    assert retry.document.import_status is ImportStatus.COMPLETED
    stats = database.dashboard_stats()
    assert stats.documents == 1
    assert stats.pages == 10
    summary_connection = sqlite3.connect(database.database_path)
    try:
        fts = summary_connection.execute("SELECT COUNT(*) FROM page_search").fetchone()[0]
        duplicates = summary_connection.execute(
            """
            SELECT document_id, page_number, COUNT(*) AS copies
            FROM pages GROUP BY document_id, page_number HAVING copies > 1
            """
        ).fetchall()
    finally:
        summary_connection.close()
    assert fts == 10
    assert duplicates == []
    pngs = sorted(pages_dir.glob("*.png"))
    assert len(pngs) == 10
    for png in pngs:
        assert_decodable_png(png)
    assert _temp_files(pages_dir) == []


def test_normal_import_and_special_pages_do_not_regress(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )
    source = build_diagnostics_pdf(tmp_path / "diag.pdf")

    result = service.import_pdf(source.read_bytes(), source.name, title="诊断样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    summary = result.diagnostics
    assert summary.blank_page_numbers == (DIAG_BLANK_PAGE,)
    assert summary.short_text_page_numbers == (DIAG_SHORT_PAGE, DIAG_MIXED_PAGE)
    assert summary.landscape_page_numbers == (
        DIAG_LANDSCAPE_PAGE,
        DIAG_ROTATED_PAGE,
        DIAG_MIXED_PAGE,
    )
    assert summary.rotated_page_numbers == (DIAG_ROTATED_PAGE, DIAG_MIXED_PAGE)
    assert summary.failed_page_numbers == ()
    pages_dir = tmp_path / "pages" / str(result.document.id)
    for png in pages_dir.glob("*.png"):
        assert_decodable_png(png)
    assert _temp_files(pages_dir) == []


def test_pdf_service_rejects_new_render_over_existing_image(tmp_path: Path) -> None:
    source = build_sample_pdf(tmp_path / "sample.pdf")
    output_dir = tmp_path / "pages"
    service = PdfService()
    first = service.process_page(source, output_dir, 1)
    original = first.image_path.read_bytes()

    with pytest.raises(PdfProcessingError, match="已存在"):
        service.process_page(source, output_dir, 1)
    assert first.image_path.read_bytes() == original
