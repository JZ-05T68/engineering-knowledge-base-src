"""Page-level failure isolation tests for multi-page PDF processing.

A controlled fake PyMuPDF document injects deterministic render/text failures
on chosen pages while the real ``PdfService.process`` loop, real temporary
directories and the real ``Database``/``DocumentService`` persistence path run
unmodified. Later pages must genuinely be processed, failed pages must be
recorded as FAILED, and the document must never look fully successful.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pdf_test_helpers import open_image, png_bytes_for

import src.pdf_service as pdf_service_module
from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus, PageStatus
from src.pdf_service import PageDiagnostics, PdfService, ProcessedPage

FAILING_PAGE_COUNT = 5
FAILING_PAGE = 3
PAGE_TEXT_TEMPLATE = "Isolation fixture page {} with enough body text."


class _FakePixmap:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def save(self, path: str) -> None:
        Path(path).write_bytes(png_bytes_for(self.payload))

    def tobytes(self, fmt: str) -> bytes:
        assert fmt == "png"
        return png_bytes_for(self.payload)


class _IsolationPage:
    """Fake page with optional deterministic render/text failure injection."""

    def __init__(self, number: int, *, fail_render: bool, fail_text: bool) -> None:
        self.number = number
        self.fail_render = fail_render
        self.fail_text = fail_text
        self.rect = SimpleNamespace(width=595.0, height=842.0)
        self.rotation = 0
        self.pixmap_calls = 0
        self.get_text_calls = 0

    def get_pixmap(self, *, matrix: object, alpha: bool) -> _FakePixmap:
        del matrix, alpha
        self.pixmap_calls += 1
        if self.fail_render:
            raise RuntimeError(f"render blew up on page {self.number}")
        return _FakePixmap(f"png-{self.number}".encode())

    def get_text(self, mode: str) -> str:
        assert mode == "text"
        self.get_text_calls += 1
        if self.fail_text:
            raise RuntimeError(f"text blew up on page {self.number}")
        return PAGE_TEXT_TEMPLATE.format(self.number)


class _IsolationDocument:
    """Fake document recording which pages were actually accessed."""

    needs_pass = False

    def __init__(self, *, fail_render: bool = False, fail_text: bool = False) -> None:
        self.pages = [
            _IsolationPage(
                number,
                fail_render=fail_render and number == FAILING_PAGE,
                fail_text=fail_text and number == FAILING_PAGE,
            )
            for number in range(1, FAILING_PAGE_COUNT + 1)
        ]
        self.page_count = len(self.pages)
        self.closed = False
        self.load_page_calls: list[int] = []

    def load_page(self, index: int) -> _IsolationPage:
        self.load_page_calls.append(index)
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


class _IsolationModule:
    """Minimal PyMuPDF module substitute returning the controlled document."""

    def __init__(self, document: _IsolationDocument) -> None:
        self.document = document

    @staticmethod
    def Matrix(x_scale: float, y_scale: float) -> tuple[float, float]:  # noqa: N802
        return (x_scale, y_scale)

    @staticmethod
    def Pixmap(path: str) -> object:  # noqa: N802
        return open_image(path)

    def open(self, path: str) -> _IsolationDocument:
        assert Path(path).is_file()
        return self.document


@pytest.fixture()
def isolation_document(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> _IsolationDocument:
    """Install a controlled fake document; parametrized via indirect kwargs."""

    document = _IsolationDocument(**getattr(request, "param", {}))
    monkeypatch.setattr(
        pdf_service_module,
        "_load_pymupdf",
        lambda: _IsolationModule(document),
    )
    return document


def _page_by_number(pages: list[ProcessedPage], page_number: int) -> ProcessedPage:
    return next(page for page in pages if page.page_number == page_number)


def _successful_pages(pages: list[ProcessedPage]) -> list[ProcessedPage]:
    return [page for page in pages if page.page_number != FAILING_PAGE]


@pytest.mark.parametrize(
    "isolation_document", [{"fail_render": True}], indirect=True
)
def test_process_continues_after_middle_page_render_failure(
    isolation_document: _IsolationDocument, tmp_path: Path
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    output_dir = tmp_path / "pages"
    calls: list[tuple[int, int]] = []

    pages = PdfService().process(
        source_path,
        output_dir,
        on_progress=lambda done, total: calls.append((done, total)),
    )

    # Every page is attempted once, in order, through to the last page.
    assert len(pages) == FAILING_PAGE_COUNT
    assert [page.page_number for page in pages] == list(
        range(1, FAILING_PAGE_COUNT + 1)
    )
    assert isolation_document.load_page_calls == list(range(FAILING_PAGE_COUNT))
    assert calls == [
        (number, FAILING_PAGE_COUNT)
        for number in range(1, FAILING_PAGE_COUNT + 1)
    ]
    assert isolation_document.closed is True

    failed = _page_by_number(pages, FAILING_PAGE)
    assert failed.processing_error != ""
    assert f"第 {FAILING_PAGE} 页" in failed.processing_error
    assert "render blew up" in failed.processing_error
    assert failed.needs_review is True
    assert failed.extracted_text == ""
    assert failed.diagnostics == PageDiagnostics()
    # The render failed before saving, so no PNG exists for the failed page.
    assert not (output_dir / f"page_{FAILING_PAGE:04d}.png").exists()

    # Surrounding pages were genuinely rendered and extracted afterwards.
    for page in _successful_pages(pages):
        assert page.processing_error == ""
        assert PAGE_TEXT_TEMPLATE.format(page.page_number) in page.extracted_text
        assert page.needs_review is False
        assert page.diagnostics.effective_char_count >= 20
        assert page.image_path.read_bytes() == png_bytes_for(
            f"png-{page.page_number}".encode()
        )
    later_pages = isolation_document.pages[FAILING_PAGE:]
    assert all(page.get_text_calls == 1 for page in later_pages)
    assert all(page.pixmap_calls == 1 for page in later_pages)

    # Exactly one PNG per successful page; nothing overwritten or stray.
    on_disk = sorted(path.name for path in output_dir.iterdir())
    expected = [
        f"page_{number:04d}.png"
        for number in range(1, FAILING_PAGE_COUNT + 1)
        if number != FAILING_PAGE
    ]
    assert on_disk == expected


@pytest.mark.parametrize("isolation_document", [{"fail_text": True}], indirect=True)
def test_process_continues_after_middle_page_text_failure(
    isolation_document: _IsolationDocument, tmp_path: Path
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    output_dir = tmp_path / "pages"

    pages = PdfService().process(source_path, output_dir)

    assert [page.page_number for page in pages] == list(
        range(1, FAILING_PAGE_COUNT + 1)
    )
    failed = _page_by_number(pages, FAILING_PAGE)
    assert f"第 {FAILING_PAGE} 页" in failed.processing_error
    assert "text blew up" in failed.processing_error
    assert failed.extracted_text == ""
    assert failed.diagnostics == PageDiagnostics()
    # The PNG was rendered before the text failure; it stays as a FAILED page's
    # image and is never deleted or reused for another page.
    assert (output_dir / f"page_{FAILING_PAGE:04d}.png").read_bytes() == png_bytes_for(
        f"png-{FAILING_PAGE}".encode()
    )

    for page in _successful_pages(pages):
        assert page.processing_error == ""
        assert PAGE_TEXT_TEMPLATE.format(page.page_number) in page.extracted_text
        assert page.image_path.read_bytes() == png_bytes_for(
            f"png-{page.page_number}".encode()
        )
    assert isolation_document.load_page_calls == list(range(FAILING_PAGE_COUNT))
    assert isolation_document.closed is True


@pytest.mark.parametrize("isolation_document", [{"fail_text": True}], indirect=True)
def test_document_service_persists_partial_failure(
    isolation_document: _IsolationDocument, tmp_path: Path
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )

    result = service.import_pdf(b"fake pdf bytes", "isolation.pdf", title="失败隔离")

    document = result.document
    assert document.page_count == FAILING_PAGE_COUNT
    assert document.processed_page_count == FAILING_PAGE_COUNT
    # The failed page carries no text layer; the other four do.
    assert document.text_page_count == FAILING_PAGE_COUNT - 1
    assert document.import_status is ImportStatus.PARTIALLY_COMPLETED
    assert document.import_status is not ImportStatus.COMPLETED
    assert document.import_error != ""

    pages = result.pages
    assert len(pages) == FAILING_PAGE_COUNT
    assert [page.page_number for page in pages] == list(
        range(1, FAILING_PAGE_COUNT + 1)
    )
    for page in pages:
        if page.page_number == FAILING_PAGE:
            assert page.status is PageStatus.FAILED
            assert page.processing_status == "failed"
            assert f"第 {FAILING_PAGE} 页" in page.processing_error
        else:
            assert page.status is PageStatus.PENDING
            assert page.processing_status == "text_extracted"
            assert page.processing_error == ""
            assert PAGE_TEXT_TEMPLATE.format(page.page_number) in page.extracted_text
            assert Path(page.image_path).read_bytes() == png_bytes_for(
                f"png-{page.page_number}".encode()
            )

    # The database view agrees: all five rows exist, nothing rolled back.
    stored = database.list_pages(document.id)
    assert len(stored) == FAILING_PAGE_COUNT
    assert [page.page_number for page in stored] == list(
        range(1, FAILING_PAGE_COUNT + 1)
    )

    record = result.import_record
    assert record is not None
    assert record.status is ImportStatus.PARTIALLY_COMPLETED
    assert record.total_pages == FAILING_PAGE_COUNT
    assert record.processed_pages == FAILING_PAGE_COUNT
    assert record.failed_pages == 1
    assert record.error_message != ""


@pytest.mark.parametrize("isolation_document", [{"fail_render": True}], indirect=True)
def test_process_page_and_reprocess_keep_single_page_isolation(
    isolation_document: _IsolationDocument, tmp_path: Path
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    service = PdfService()

    failed = service.process_page(source_path, tmp_path / "single", FAILING_PAGE)
    assert failed.page_number == FAILING_PAGE
    assert "render blew up" in failed.processing_error
    assert failed.diagnostics == PageDiagnostics()
    assert isolation_document.load_page_calls == [FAILING_PAGE - 1]

    healthy = service.process_page(source_path, tmp_path / "single2", FAILING_PAGE + 1)
    assert healthy.processing_error == ""
    assert PAGE_TEXT_TEMPLATE.format(FAILING_PAGE + 1) in healthy.extracted_text
    assert isolation_document.load_page_calls == [FAILING_PAGE - 1, FAILING_PAGE]

    # reprocess_page on the failed page marks only that page FAILED again.
    database = Database(tmp_path / "database" / "knowledge.db")
    document_service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )
    result = document_service.import_pdf(b"fake pdf bytes", "isolation.pdf")
    pages_before = {
        page.page_number: page for page in database.list_pages(result.document.id)
    }
    target = pages_before[FAILING_PAGE]
    assert target.status is PageStatus.FAILED

    updated = document_service.reprocess_page(target.id)
    assert updated.id == target.id
    assert updated.status is PageStatus.FAILED
    assert "render blew up" in updated.processing_error

    pages_after = {
        page.page_number: page for page in database.list_pages(result.document.id)
    }
    for number, before in pages_before.items():
        if number == FAILING_PAGE:
            continue
        after = pages_after[number]
        assert after.status is before.status
        assert after.extracted_text == before.extracted_text
        assert after.processing_error == before.processing_error
