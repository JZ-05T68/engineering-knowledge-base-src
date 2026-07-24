"""Tests for safe PDF import, rendering decisions, and Markdown persistence."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.pdf_service as pdf_service_module
from src.database import Database
from src.document_service import DocumentImportError, DocumentService
from src.models import PageStatus
from src.pdf_service import PdfProcessingError, PdfService, ProcessedPage


class FakePdfService:
    """Create deterministic page files without requiring PyMuPDF in service tests."""

    def __init__(self) -> None:
        self.process_calls = 0
        self.reuse_existing_calls: list[bool] = []
        self.process_page_calls: list[int] = []

    @staticmethod
    def calculate_sha256(file_path: Path | str) -> str:
        return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()

    def process(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        *,
        reuse_existing: bool = False,
    ) -> list[ProcessedPage]:
        self.process_calls += 1
        self.reuse_existing_calls.append(reuse_existing)
        assert Path(pdf_path).is_file()
        page_directory = Path(output_dir)
        page_directory.mkdir(parents=True, exist_ok=True)
        first_image = page_directory / "page_0001.png"
        second_image = page_directory / "page_0002.png"
        if not first_image.exists():
            first_image.write_bytes(b"first page png")
        if not second_image.exists():
            second_image.write_bytes(b"second page png")
        return [
            ProcessedPage(1, first_image, "足够长的工程正文", False, 8),
            ProcessedPage(2, second_image, "", True, 0),
        ]

    def process_page(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        page_number: int,
        *,
        reuse_existing: bool = False,
    ) -> ProcessedPage:
        self.process_page_calls.append(page_number)
        assert Path(pdf_path).is_file()
        page_directory = Path(output_dir)
        page_directory.mkdir(parents=True, exist_ok=True)
        image = page_directory / f"page_{page_number:04d}.png"
        if not image.exists():
            image.write_bytes(f"page {page_number} png".encode())
        if page_number == 1:
            return ProcessedPage(1, image, "足够长的工程正文", False, 8)
        if page_number == 2:
            return ProcessedPage(2, image, "", True, 0)
        raise PdfProcessingError(f"页码 {page_number} 超出范围。")


class FakeDatabase:
    """Record the narrow database protocol used by :class:`DocumentService`."""

    def __init__(self, existing_document: Any | None = None) -> None:
        self.existing_document = existing_document
        self.document_values: dict[str, Any] | None = None
        self.page_values: list[dict[str, Any]] = []
        self.pages_by_number: dict[tuple[int, int], Any] = {}
        self.markdown_update: dict[str, Any] | None = None

    def get_document_by_sha256(self, sha256: str) -> Any | None:
        del sha256
        return self.existing_document

    def list_pages(self, document_id: int) -> list[Any]:
        return [
            page
            for (stored_document_id, _), page in self.pages_by_number.items()
            if stored_document_id == document_id
        ]

    def create_document(self, **values: Any) -> Any:
        self.document_values = values
        return SimpleNamespace(id=42, **values)

    def create_page(self, **values: Any) -> Any:
        processing_status = values.get("processing_status")
        if processing_status is None:
            if values.get("processing_error"):
                processing_status = "failed"
            elif values.get("ocr_text"):
                processing_status = "ocr_completed"
            elif values.get("extracted_text"):
                processing_status = "text_extracted"
            else:
                processing_status = "pending_review"
        values["processing_status"] = processing_status
        self.page_values.append(values)
        page = SimpleNamespace(id=len(self.page_values), **values)
        self.pages_by_number[(values["document_id"], values["page_number"])] = page
        return page

    @staticmethod
    def update_document_page_count(document_id: int, page_count: int) -> Any:
        return SimpleNamespace(id=document_id, page_count=page_count)

    def get_page_by_number(self, document_id: int, page_number: int) -> Any | None:
        return self.pages_by_number.get((document_id, page_number))

    def update_page_markdown(
        self,
        page_id: int,
        markdown_content: str,
        markdown_path: Path,
        *,
        review_status: PageStatus,
    ) -> Any:
        self.markdown_update = {
            "page_id": page_id,
            "markdown_content": markdown_content,
            "markdown_path": markdown_path,
            "review_status": review_status,
        }
        return SimpleNamespace(id=page_id, status=review_status, **self.markdown_update)


def make_service(
    tmp_path: Path,
    database: FakeDatabase,
    pdf_service: FakePdfService | None = None,
) -> DocumentService:
    """Build a service whose writable paths are isolated under ``tmp_path``."""

    return DocumentService(
        database=database,  # type: ignore[arg-type]
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        pdf_service=pdf_service or FakePdfService(),  # type: ignore[arg-type]
    )


def test_import_pdf_saves_original_images_and_metadata(tmp_path: Path) -> None:
    database = FakeDatabase()
    fake_pdf = FakePdfService()
    service = make_service(tmp_path, database, fake_pdf)
    uploaded = BytesIO(b"ignored prefix%PDF local test")
    uploaded.seek(len(b"ignored prefix"))

    result = service.import_pdf(uploaded, "../manual.pdf", "设备手册")

    assert uploaded.tell() == len(b"ignored prefix")
    assert result.duplicate is False
    assert result.document.id == 42
    assert len(result.pages) == 2
    assert fake_pdf.process_calls == 1
    assert database.document_values is not None
    assert database.document_values["filename"] == "manual.pdf"
    assert database.document_values["title"] == "设备手册"
    assert database.document_values["sha256"] == hashlib.sha256(
        b"ignored prefix%PDF local test"
    ).hexdigest()
    source_path = database.document_values["source_path"]
    assert source_path.parent == tmp_path / "raw"
    assert source_path.read_bytes() == b"ignored prefix%PDF local test"
    assert (tmp_path / "pages" / "42" / "page_0001.png").is_file()
    assert database.page_values[0]["status"] is PageStatus.PENDING
    assert database.page_values[1]["status"] is PageStatus.PENDING
    assert database.page_values[0]["processing_status"] == "text_extracted"
    assert database.page_values[1]["processing_status"] == "pending_review"


def test_duplicate_import_does_not_write_or_process(tmp_path: Path) -> None:
    existing_page = SimpleNamespace(id=5)
    existing_document = SimpleNamespace(id=9)
    database = FakeDatabase(existing_document)
    database.pages_by_number[(9, 1)] = existing_page
    fake_pdf = FakePdfService()
    service = make_service(tmp_path, database, fake_pdf)

    result = service.import_pdf(b"same pdf", "same.PDF")

    assert result.duplicate is True
    assert result.document is existing_document
    assert result.pages == (existing_page,)
    assert fake_pdf.process_calls == 0
    assert not (tmp_path / "raw").exists()


def test_incomplete_duplicate_resumes_with_existing_original(tmp_path: Path) -> None:
    source_path = tmp_path / "raw" / "stored.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"same pdf")
    existing_document = SimpleNamespace(
        id=9,
        page_count=0,
        source_path=source_path,
    )
    database = FakeDatabase(existing_document)
    fake_pdf = FakePdfService()
    service = make_service(tmp_path, database, fake_pdf)

    result = service.import_pdf(b"same pdf", "same.pdf")

    assert result.duplicate is False
    assert len(result.pages) == 2
    assert fake_pdf.reuse_existing_calls == [True]
    assert source_path.read_bytes() == b"same pdf"
    assert len(database.page_values) == 2


def test_import_rejects_non_pdf_filename_before_writing(tmp_path: Path) -> None:
    service = make_service(tmp_path, FakeDatabase())

    with pytest.raises(DocumentImportError, match=".pdf"):
        service.import_pdf(b"not a pdf", "notes.txt")

    assert not (tmp_path / "raw").exists()


def test_import_rejects_empty_pdf_and_accepts_chinese_name_with_spaces(tmp_path: Path) -> None:
    database = FakeDatabase()
    service = make_service(tmp_path, database)

    with pytest.raises(DocumentImportError, match="为空"):
        service.import_pdf(b"", "空白 文档.pdf")

    result = service.import_pdf(b"%PDF local", "中文 手册.pdf")
    assert result.document.page_count == 2
    assert database.document_values is not None
    assert database.document_values["filename"] == "中文 手册.pdf"
    assert database.document_values["source_path"].is_file()


def test_save_page_markdown_writes_utf8_and_updates_database(tmp_path: Path) -> None:
    database = FakeDatabase()
    database.pages_by_number[(42, 2)] = SimpleNamespace(
        id=7,
        markdown_content="",
        markdown_path=None,
        status=PageStatus.PENDING,
    )
    service = make_service(tmp_path, database)

    updated_page = service.save_page_markdown(42, 2, "# 校对内容\n\n泵站参数。")

    markdown_path = tmp_path / "markdown" / "42" / "page_0002.md"
    assert markdown_path.read_text(encoding="utf-8") == "# 校对内容\n\n泵站参数。"
    assert database.markdown_update == {
        "page_id": 7,
        "markdown_content": "# 校对内容\n\n泵站参数。",
        "markdown_path": markdown_path,
        "review_status": PageStatus.DRAFT,
    }
    assert updated_page.status is PageStatus.DRAFT
    assert updated_page.id == 7


def test_note_save_failure_is_reported_and_user_file_is_preserved(tmp_path: Path) -> None:
    class FailingDatabase(FakeDatabase):
        def update_page_markdown(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("database is read-only")

    database = FailingDatabase()
    database.pages_by_number[(42, 1)] = SimpleNamespace(
        id=1,
        markdown_content="",
        markdown_path=None,
        status=PageStatus.PENDING,
    )
    service = make_service(tmp_path, database)

    with pytest.raises(DocumentImportError, match="保存.*失败"):
        service.save_page_markdown(42, 1, "# 不应静默丢失")

    saved = tmp_path / "markdown" / "42" / "page_0001.md"
    assert saved.read_text(encoding="utf-8") == "# 不应静默丢失"


def test_markdown_draft_persists_after_database_and_service_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "database" / "knowledge.db"
    database = Database(database_path)
    document = database.create_document(
        title="持久化手册",
        filename="persistence.pdf",
        source_path=tmp_path / "raw" / "persistence.pdf",
        sha256="e" * 64,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "1" / "page_0001.png",
    )
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )

    saved_page = service.save_page_markdown(document.id, 1, "# 可恢复草稿\n\n本地保存。")
    markdown_path = tmp_path / "markdown" / str(document.id) / "page_0001.md"
    first_modified_time = markdown_path.stat().st_mtime_ns
    duplicate_save = service.save_page_markdown(
        document.id, 1, "# 可恢复草稿\n\n本地保存。"
    )

    assert saved_page.status is PageStatus.DRAFT
    assert duplicate_save == saved_page
    assert markdown_path.stat().st_mtime_ns == first_modified_time
    assert database.dashboard_stats().draft_pages == 1

    reopened_database = Database(database_path)
    reopened_service = DocumentService(
        reopened_database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    reopened_page = reopened_database.get_page_by_number(document.id, 1)
    assert reopened_page is not None
    assert reopened_page.markdown_content == "# 可恢复草稿\n\n本地保存。"
    assert reopened_page.status is PageStatus.DRAFT
    assert reopened_service.database.search('"恢复"')[0].page_id == reopened_page.id


def test_save_review_and_next_runs_continuously_until_queue_end(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="连续复核手册",
        filename="continuous.pdf",
        source_path=tmp_path / "raw" / "continuous.pdf",
        sha256="f" * 64,
    )
    pages = [
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / "1" / f"page_{page_number:04d}.png",
        )
        for page_number in range(1, 4)
    ]
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )

    first, next_page = service.save_page_markdown_and_next(
        document.id, 1, "第一页", queue_document_id=document.id
    )
    assert first.status is PageStatus.REVIEWED
    assert next_page == pages[1]
    second, next_page = service.save_page_markdown_and_next(
        document.id, 2, "第二页", queue_document_id=document.id
    )
    assert second.status is PageStatus.REVIEWED
    assert next_page == pages[2]
    third, next_page = service.save_page_markdown_and_next(
        document.id, 3, "第三页", queue_document_id=document.id
    )
    assert third.status is PageStatus.REVIEWED
    assert next_page is None
    assert database.list_review_pages(document.id) == []
    assert database.dashboard_stats().reviewed_pages == 3


def test_skipped_page_leaves_default_queue_and_returns_next_page(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="跳过测试",
        filename="skip.pdf",
        source_path=tmp_path / "raw" / "skip.pdf",
        sha256="1" * 64,
    )
    first = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "1" / "page_0001.png",
    )
    second = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=tmp_path / "pages" / "1" / "page_0002.png",
    )
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )

    skipped, next_page = service.skip_page_and_next(
        first.id, queue_document_id=document.id
    )

    assert skipped.status is PageStatus.SKIPPED
    assert next_page == second
    assert database.list_review_pages(document.id) == [second]
    stats = database.dashboard_stats()
    assert stats.skipped_pages == 1
    assert stats.pending_pages == 1


def test_single_page_failure_keeps_other_completed_pages(tmp_path: Path) -> None:
    class PartiallyFailingPdf(FakePdfService):
        def process(
            self,
            pdf_path: Path | str,
            output_dir: Path | str,
            *,
            reuse_existing: bool = False,
        ) -> list[ProcessedPage]:
            successful = super().process(
                pdf_path, output_dir, reuse_existing=reuse_existing
            )[0]
            return [
                successful,
                ProcessedPage(
                    2,
                    Path(output_dir) / "page_0002.png",
                    "",
                    True,
                    0,
                    "第 2 页损坏",
                ),
            ]

    database = FakeDatabase()
    service = make_service(tmp_path, database, PartiallyFailingPdf())

    result = service.import_pdf(b"%PDF partial", "partial.pdf")

    assert len(result.pages) == 2
    assert result.pages[0].status is PageStatus.PENDING
    assert result.pages[1].status is PageStatus.FAILED
    assert result.pages[1].processing_error == "第 2 页损坏"


class FakePixmap:
    """Minimal PyMuPDF pixmap substitute that writes deterministic bytes."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def save(self, path: str) -> None:
        Path(path).write_bytes(self.payload)


class FakePyMuPdfPage:
    """Minimal page substitute for renderer tests."""

    def __init__(self, page_number: int, text: str) -> None:
        self.page_number = page_number
        self.text = text

    def get_pixmap(self, *, matrix: object, alpha: bool) -> FakePixmap:
        assert matrix == (2.0, 2.0)
        assert alpha is False
        return FakePixmap(f"png-{self.page_number}".encode())

    def get_text(self, mode: str) -> str:
        assert mode == "text"
        return self.text


class FakePyMuPdfDocument:
    """Minimal closable document substitute for renderer tests."""

    needs_pass = False

    def __init__(self) -> None:
        self.pages = [FakePyMuPdfPage(1, "A B C 123"), FakePyMuPdfPage(2, " -- ")]
        self.page_count = len(self.pages)
        self.closed = False
        self.load_page_calls: list[int] = []

    def load_page(self, index: int) -> FakePyMuPdfPage:
        self.load_page_calls.append(index)
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


class FakePyMuPdf:
    """Minimal module substitute exposing the PyMuPDF calls used by the service."""

    def __init__(self, document: FakePyMuPdfDocument) -> None:
        self.document = document

    @staticmethod
    def Matrix(x_scale: float, y_scale: float) -> tuple[float, float]:  # noqa: N802
        return (x_scale, y_scale)

    def open(self, path: str) -> FakePyMuPdfDocument:
        assert Path(path).is_file()
        return self.document


def test_pdf_service_renders_pages_and_marks_short_text_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"fake pdf")
    fake_document = FakePyMuPdfDocument()
    monkeypatch.setattr(
        pdf_service_module,
        "_load_pymupdf",
        lambda: FakePyMuPdf(fake_document),
    )
    service = PdfService(minimum_text_length=4, dpi=144)

    pages = service.process(source_path, tmp_path / "rendered")

    assert [page.effective_text_length for page in pages] == [6, 0]
    assert [page.needs_review for page in pages] == [False, True]
    assert pages[0].image_path.read_bytes() == b"png-1"
    assert pages[1].image_path.read_bytes() == b"png-2"
    assert fake_document.closed is True

    resumed_pages = service.process(
        source_path,
        tmp_path / "rendered",
        reuse_existing=True,
    )
    assert [page.image_path.read_bytes() for page in resumed_pages] == [b"png-1", b"png-2"]

    with pytest.raises(PdfProcessingError, match="已存在"):
        service.process(source_path, tmp_path / "rendered")
    assert pages[0].image_path.read_bytes() == b"png-1"


def test_pdf_sha256_reads_file_in_chunks(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"chunked content")

    assert PdfService.calculate_sha256(source_path, chunk_size=3) == hashlib.sha256(
        b"chunked content"
    ).hexdigest()


def test_corrupt_pdf_reports_open_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenPyMuPdf:
        @staticmethod
        def open(path: str) -> None:
            raise RuntimeError(f"broken: {Path(path).name}")

    source_path = tmp_path / "损坏 文件.pdf"
    source_path.write_bytes(b"not a pdf")
    monkeypatch.setattr(pdf_service_module, "_load_pymupdf", lambda: BrokenPyMuPdf())

    with pytest.raises(PdfProcessingError, match="无法打开 PDF"):
        PdfService().process(source_path, tmp_path / "pages with spaces")


def test_reprocess_page_uses_single_page_processing(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    fake_pdf = FakePdfService()
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
        pdf_service=fake_pdf,
    )

    result = service.import_pdf(b"%PDF fake", "fake.pdf")
    target = result.pages[1]
    process_calls_before = fake_pdf.process_calls

    updated = service.reprocess_page(target.id)

    assert fake_pdf.process_calls == process_calls_before
    assert fake_pdf.process_page_calls == [target.page_number]
    assert updated.status is PageStatus.PENDING
    assert updated.processing_status == "pending_review"
    assert updated.processing_error == ""
    assert updated.extracted_text == ""


def test_reprocess_page_marks_target_failed_on_page_error(tmp_path: Path) -> None:
    class FailingPagePdf(FakePdfService):
        def process_page(
            self,
            pdf_path: Path | str,
            output_dir: Path | str,
            page_number: int,
            *,
            reuse_existing: bool = False,
        ) -> ProcessedPage:
            processed = super().process_page(
                pdf_path, output_dir, page_number, reuse_existing=reuse_existing
            )
            return ProcessedPage(
                processed.page_number,
                processed.image_path,
                "",
                True,
                0,
                f"第 {page_number} 页损坏",
            )

    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
        pdf_service=FailingPagePdf(),
    )

    result = service.import_pdf(b"%PDF fake", "fake.pdf")
    target = result.pages[0]

    updated = service.reprocess_page(target.id)

    assert updated.status is PageStatus.FAILED
    assert updated.processing_status == "failed"
    assert updated.processing_error == "第 1 页损坏"
