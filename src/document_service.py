"""Application service for durable local document, Markdown, and page OCR flows."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, BinaryIO
from uuid import uuid4

from src.models import Document, ImportRecord, ImportStatus, Page, PageStatus
from src.ocr_engine import OcrEngine, OcrExecutionError, require_ocr_engine
from src.ocr_policy import is_page_eligible_for_ocr
from src.office_pdf_converter import OFFICE_EXTENSIONS, OfficePdfConverter
from src.pdf_service import (
    DocumentDiagnosticsSummary,
    PdfProcessingError,
    PdfService,
    summarize_page_diagnostics,
)

if TYPE_CHECKING:
    from src.database import Database

LOGGER = logging.getLogger(__name__)
INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_STORED_STEM_LENGTH = 150
# Raw PDFs are written into a sibling temporary file first and atomically
# moved into place only after the temp file's SHA-256 matches the upload.
# The infix keeps temp files recognizable and invisible to PDF scans
# (``Path("x.pdf.tmp-y").suffix`` is not ``.pdf``) — the same convention the
# page renderer uses for page-PNG temp files.
TEMP_RAW_INFIX = ".tmp-"
# Uploaded bytes are written to the temp file in bounded chunks so the copy
# loop never allocates a second full-size buffer.
_PDF_WRITE_CHUNK_SIZE = 1024 * 1024
OCR_ERROR_PREFIX = "OCR："
OCR_ERROR_MESSAGE_LIMIT = 200
_LOCAL_PATH_PLACEHOLDER = "[本地路径]"
_FILE_URI_PATTERN = re.compile(r"file://\S*", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s，]*")
_POSIX_PATH_PATTERN = re.compile(r"(?:(?<=^)|(?<=[\s（(\"'：:，,；;=]))/[^\s，]+")


class DocumentImportError(RuntimeError):
    """Raised when a document cannot be imported into local storage safely."""


class PageOcrOutcome(StrEnum):
    """Typed outcome of one :meth:`DocumentService.run_page_ocr` call.

    ``OcrUnavailable`` is raised instead of an outcome when no usable
    engine exists, so callers distinguish the four results without any
    string parsing: completed, not eligible, engine unavailable
    (exception), and per-page execution failure.
    """

    COMPLETED = "completed"
    NOT_ELIGIBLE = "not_eligible"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PageOcrResult:
    """Result of one isolated single-page OCR attempt.

    ``page`` is the current page state after the attempt; it is unchanged
    unless the outcome is ``COMPLETED`` (OCR text persisted) or ``FAILED``
    (OCR error recorded in ``processing_error``).
    """

    page: Page
    outcome: PageOcrOutcome


def _clear_ocr_error(processing_error: str) -> str:
    """Remove only OCR-prefixed segments, preserving unrelated errors."""

    return "；".join(
        segment
        for segment in processing_error.split("；")
        if segment and not segment.startswith(OCR_ERROR_PREFIX)
    )


def _merge_ocr_error(processing_error: str, ocr_error: str) -> str:
    """Replace any previous OCR error while preserving unrelated errors."""

    kept = [
        segment
        for segment in processing_error.split("；")
        if segment and not segment.startswith(OCR_ERROR_PREFIX)
    ]
    kept.append(ocr_error)
    return "；".join(kept)


def _sanitize_ocr_error_message(message: str) -> str:
    """Normalize one engine error for durable, privacy-safe persistence.

    The stored message keeps the engine's failure description but drops
    tracebacks, local absolute paths (Windows, POSIX, UNC and ``file://``
    URIs — replaced by a fixed placeholder), the fullwidth segment
    separator ``；`` (normalized to ``，`` so it cannot masquerade as a
    ``processing_error`` segment boundary), and all excess whitespace.
    The result is bounded and never empty. Detection is purely textual:
    no filesystem, network, or path-existence checks are involved.
    """

    compact = " ".join((message or "").split()).replace("；", "，")
    for pattern in (
        _FILE_URI_PATTERN,
        _WINDOWS_PATH_PATTERN,
        _POSIX_PATH_PATTERN,
    ):
        compact = pattern.sub(_LOCAL_PATH_PLACEHOLDER, compact)
    compact = " ".join(compact.split())
    return compact[:OCR_ERROR_MESSAGE_LIMIT] or "识别失败。"


def _write_pdf_content(temp_path: Path, content: bytes) -> None:
    """Write PDF bytes to a fresh temp file in bounded chunks, then flush/close."""

    with temp_path.open("wb") as handle:
        for offset in range(0, len(content), _PDF_WRITE_CHUNK_SIZE):
            handle.write(content[offset : offset + _PDF_WRITE_CHUNK_SIZE])
        handle.flush()


def _cleanup_stale_temp_raws(raw_path: Path) -> None:
    """Remove temp saves of this exact raw target left by killed earlier runs.

    Only temps whose names start with the exact target filename are removed;
    temp files of any other PDF in the same directory are left untouched.
    """

    for stale in raw_path.parent.glob(f"{raw_path.name}{TEMP_RAW_INFIX}*"):
        try:
            stale.unlink()
        except OSError:
            LOGGER.warning("无法清理旧临时原文件：%s", stale)


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Outcome of a PDF import, including an intentional duplicate result.

    ``diagnostics`` summarizes this run's processed pages; a duplicate import
    re-runs nothing, so it keeps the empty default summary.
    """

    document: Document
    pages: tuple[Page, ...]
    duplicate: bool = False
    import_record: ImportRecord | None = None
    diagnostics: DocumentDiagnosticsSummary = field(
        default_factory=DocumentDiagnosticsSummary
    )


def first_reviewable_import_page(result: ImportResult) -> Page | None:
    """Return the first newly imported page that can enter the review queue.

    A complete duplicate is intentionally excluded even when its existing
    document still has reviewable pages: importing the same file must not look
    like it created new review work.
    """

    if result.duplicate:
        return None
    return next(
        (
            page
            for page in result.pages
            if page.status in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}
        ),
        None,
    )


class DocumentService:
    """Coordinate file storage, PDF processing, and database metadata writes."""

    def __init__(
        self,
        database: Database,
        raw_dir: Path | str,
        pages_dir: Path | str,
        markdown_dir: Path | str,
        pdf_service: PdfService | None = None,
        ocr_engine: OcrEngine | None = None,
        office_converter: OfficePdfConverter | None = None,
    ) -> None:
        self.database = database
        self.raw_dir = Path(raw_dir)
        self.pages_dir = Path(pages_dir)
        self.markdown_dir = Path(markdown_dir)
        self.pdf_service = pdf_service or PdfService()
        self.ocr_engine = ocr_engine
        self.office_converter = office_converter or OfficePdfConverter()

    def import_document(
        self,
        file_content: bytes | bytearray | memoryview | BinaryIO,
        filename: str,
        title: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ImportResult:
        """Import PDF, Word or PowerPoint through the existing PDF page pipeline.

        Office originals are saved locally before conversion.  The generated
        PDF is derived input for rendering, OCR and page-number citations; the
        original Office file remains untouched in ``raw/originals``.
        """

        safe_filename = self._safe_document_filename(filename)
        extension = Path(safe_filename).suffix.lower()
        if extension == ".pdf":
            return self.import_pdf(
                file_content,
                safe_filename,
                title=title,
                progress_callback=progress_callback,
            )
        if extension not in OFFICE_EXTENSIONS:
            raise DocumentImportError("目前支持 PDF、Word 和 PowerPoint 文件。")

        content = self._read_upload(file_content)
        if not content:
            raise DocumentImportError("上传的文件为空，请选择有效文件。")
        sha256 = hashlib.sha256(content).hexdigest()
        original_path = self._choose_original_path(sha256, safe_filename)
        self._save_original_document(original_path, content, sha256)
        conversion_root = self.raw_dir / ".office-conversion"
        conversion_root.mkdir(parents=True, exist_ok=True)
        try:
            with TemporaryDirectory(dir=conversion_root) as temporary_directory:
                converted_path = Path(temporary_directory) / f"{Path(safe_filename).stem}.pdf"
                self.office_converter.convert(original_path, converted_path)
                return self.import_pdf(
                    converted_path.read_bytes(),
                    f"{Path(safe_filename).stem}.pdf",
                    title=(title or "").strip() or Path(safe_filename).stem,
                    progress_callback=progress_callback,
                )
        except DocumentImportError:
            raise
        except Exception as exc:
            LOGGER.exception("Office 文档转换失败：filename=%s", safe_filename)
            raise DocumentImportError(
                "无法在本机读取这份 Word 或 PowerPoint 文件；原文件已安全保留。"
            ) from exc
        finally:
            with suppress(OSError):
                conversion_root.rmdir()

    def import_pdf(
        self,
        file_content: bytes | bytearray | memoryview | BinaryIO,
        filename: str,
        title: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ImportResult:
        """Import an uploaded PDF, returning the existing record for duplicates."""

        safe_filename = self._safe_pdf_filename(filename)
        content = self._read_upload(file_content)
        if not content:
            raise DocumentImportError("上传的 PDF 文件为空，请选择有效文件。")

        sha256 = hashlib.sha256(content).hexdigest()
        document_title = (title or "").strip() or Path(safe_filename).stem
        source_path: Path | None = None
        document: Document | None = None
        import_record: ImportRecord | None = None
        try:
            existing_document = self.database.get_document_by_sha256(sha256)
            if existing_document is not None:
                existing_pages = tuple(self.database.list_pages(existing_document.id))
                recorded_page_count = int(
                    getattr(existing_document, "page_count", len(existing_pages))
                )
                if recorded_page_count > 0 and len(existing_pages) == recorded_page_count:
                    LOGGER.info(
                        "检测到重复 PDF，未再次保存：filename=%s document_id=%s",
                        safe_filename,
                        existing_document.id,
                    )
                    # A complete duplicate returns before any import record is
                    # created: the deduplicated run must stay a zero-write no-op.
                    return ImportResult(
                        document=existing_document,
                        pages=existing_pages,
                        duplicate=True,
                        import_record=None,
                    )

            import_record = self._create_import_record(
                safe_filename, document_title, sha256
            )
            if existing_document is not None:
                source_path = Path(existing_document.source_path)
                # Keep the row identity even when the resume below raises, so
                # _record_failure can mark THIS document failed instead of
                # leaving it stuck in PROCESSING forever.
                document = existing_document
                LOGGER.warning(
                    "检测到未完成的 PDF 导入，将复用原文件和已有页面继续：document_id=%s",
                    existing_document.id,
                )
                document, pages, summary = self._process_document(
                    existing_document,
                    source_path,
                    reuse_existing_images=True,
                    progress_callback=progress_callback,
                )
                completed_record = self._record_result(import_record, document, pages)
                return ImportResult(
                    document=document,
                    pages=pages,
                    import_record=completed_record,
                    diagnostics=summary,
                )

            self.raw_dir.mkdir(parents=True, exist_ok=True)
            source_path = self._choose_raw_path(sha256, safe_filename)
            self._save_raw_pdf(source_path, content, sha256)

            document = self.database.create_document(
                title=document_title,
                filename=safe_filename,
                source_path=source_path,
                sha256=sha256,
                page_count=0,
                import_status=ImportStatus.PROCESSING,
            )
            document, pages, summary = self._process_document(
                document,
                source_path,
                reuse_existing_images=False,
                progress_callback=progress_callback,
            )
            completed_record = self._record_result(import_record, document, pages)
            LOGGER.info(
                "PDF 导入完成：filename=%s document_id=%s pages=%s",
                safe_filename,
                document.id,
                len(pages),
            )
            return ImportResult(
                document=document,
                pages=pages,
                import_record=completed_record,
                diagnostics=summary,
            )
        except (DocumentImportError, PdfProcessingError) as exc:
            self._record_failure(
                import_record,
                document,
                str(exc),
                record_context=(safe_filename, document_title, sha256),
            )
            LOGGER.exception("PDF 导入失败，已保留已写入的原文件和页面图片：%s", source_path)
            raise
        except Exception as exc:
            self._record_failure(
                import_record,
                document,
                str(exc),
                record_context=(safe_filename, document_title, sha256),
            )
            LOGGER.exception("PDF 导入失败，已保留已写入的文件：%s", source_path)
            raise DocumentImportError(
                f"导入 PDF“{safe_filename}”失败：{exc}。已写入的原文件和页面图片不会被删除。"
            ) from exc

    def _process_document(
        self,
        document: Document,
        source_path: Path,
        *,
        reuse_existing_images: bool,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[Document, tuple[Page, ...], DocumentDiagnosticsSummary]:
        """Render and persist pages, optionally completing an interrupted import."""

        if not source_path.is_file():
            raise DocumentImportError(f"找不到已保存的 PDF 原文件：{source_path}")

        output_dir = self.pages_dir / str(document.id)
        process_kwargs: dict[str, object] = {"reuse_existing": reuse_existing_images}
        if progress_callback is not None:
            process_kwargs["on_progress"] = progress_callback
        processed_pages = self.pdf_service.process(
            source_path,
            output_dir,
            **process_kwargs,  # type: ignore[arg-type]
        )

        existing_pages = {
            page.page_number: page for page in self.database.list_pages(document.id)
        }
        pages: list[Page] = []
        for processed_page in processed_pages:
            existing_page = existing_pages.get(processed_page.page_number)
            if existing_page is not None:
                pages.append(existing_page)
                continue

            if processed_page.processing_error:
                status = PageStatus.FAILED
            elif processed_page.needs_review:
                status = PageStatus.PENDING
            else:
                status = PageStatus.PENDING
            page = self.database.create_page(
                document_id=document.id,
                page_number=processed_page.page_number,
                image_path=processed_page.image_path,
                extracted_text=processed_page.extracted_text,
                status=status,
                processing_error=processed_page.processing_error,
                markdown_content="",
                markdown_path=None,
            )
            pages.append(page)

        failed_pages = sum(page.status is PageStatus.FAILED for page in pages)
        text_pages = sum(
            page.processing_status in {"text_extracted", "ocr_completed"}
            for page in pages
        )
        review_pages = sum(
            page.status in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}
            for page in pages
        )
        if failed_pages == len(processed_pages):
            import_status = ImportStatus.FAILED
        elif failed_pages:
            import_status = ImportStatus.PARTIALLY_COMPLETED
        else:
            import_status = ImportStatus.COMPLETED
        update_import = getattr(self.database, "update_document_import", None)
        if callable(update_import):
            updated_document = update_import(
                document.id,
                status=import_status,
                page_count=len(processed_pages),
                processed_pages=len(pages),
                text_pages=text_pages,
                review_pages=review_pages,
                error_message=(
                    f"{failed_pages} 页处理失败，请在待复核页面查看原因。"
                    if failed_pages
                    else ""
                ),
            )
        else:
            updated_document = self.database.update_document_page_count(
                document.id, len(processed_pages)
            )
        return updated_document, tuple(pages), summarize_page_diagnostics(processed_pages)

    def save_page_markdown(
        self,
        document_id: int,
        page_number: int,
        markdown_content: str,
        *,
        mark_reviewed: bool = False,
    ) -> Page:
        """Persist Markdown as a draft, or as reviewed only when explicitly requested."""

        if document_id <= 0 or page_number <= 0:
            raise ValueError("文档编号和页码必须大于 0。")
        if not isinstance(markdown_content, str):
            raise TypeError("Markdown 内容必须是字符串。")

        page = self.database.get_page_by_number(document_id, page_number)
        if page is None:
            raise DocumentImportError(f"找不到文档 {document_id} 的第 {page_number} 页。")

        page_directory = self.markdown_dir / str(document_id)
        markdown_path = page_directory / f"page_{page_number:04d}.md"
        temporary_path = page_directory / f".{markdown_path.name}.{uuid4().hex}.tmp"
        target_status = PageStatus.REVIEWED if mark_reviewed else PageStatus.DRAFT
        if (
            page.markdown_content == markdown_content
            and page.markdown_path == markdown_path
            and page.status is target_status
        ):
            try:
                if markdown_path.read_text(encoding="utf-8") == markdown_content:
                    return page
            except OSError:
                LOGGER.warning(
                    "页面 Markdown 文件需要修复：document_id=%s page_number=%s",
                    document_id,
                    page_number,
                    exc_info=True,
                )
        try:
            page_directory.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(markdown_content, encoding="utf-8", newline="\n")
            temporary_path.replace(markdown_path)
            updated_page = self.database.update_page_markdown(
                page.id,
                markdown_content,
                markdown_path,
                review_status=target_status,
            )
            LOGGER.info(
                "页面 Markdown 已保存：document_id=%s page_number=%s",
                document_id,
                page_number,
            )
            return updated_page
        except Exception as exc:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            LOGGER.exception(
                "保存页面 Markdown 失败：document_id=%s page_number=%s",
                document_id,
                page_number,
            )
            if isinstance(exc, DocumentImportError):
                raise
            raise DocumentImportError(
                f"保存文档 {document_id} 第 {page_number} 页的 Markdown 失败：{exc}"
            ) from exc

    def save_page_markdown_and_next(
        self,
        document_id: int,
        page_number: int,
        markdown_content: str,
        *,
        queue_document_id: int | None = None,
    ) -> tuple[Page, Page | None]:
        """Save, explicitly review, and return the next page in queue order."""

        updated = self.save_page_markdown(
            document_id,
            page_number,
            markdown_content,
            mark_reviewed=True,
        )
        next_page = self.database.get_adjacent_review_page(
            updated.id,
            "next",
            queue_document_id,
        )
        return updated, next_page

    def clear_page_markdown(self, document_id: int, page_number: int) -> Page:
        """Explicitly clear one page note without altering source text or images."""

        page = self.database.get_page_by_number(document_id, page_number)
        if page is None:
            raise DocumentImportError(f"找不到文档 {document_id} 的第 {page_number} 页。")
        if page.extracted_text.strip():
            status = PageStatus.PENDING
        elif page.ocr_text.strip():
            status = PageStatus.PENDING
        else:
            status = PageStatus.PENDING
        updated = self.database.update_page(
            page.id,
            markdown_content="",
            markdown_path=None,
            status=status,
        )
        if page.markdown_path is not None:
            page.markdown_path.unlink(missing_ok=True)
        LOGGER.info(
            "页面 Markdown 已清空：document_id=%s page_number=%s",
            document_id,
            page_number,
        )
        return updated

    def mark_page_reviewed(self, page_id: int) -> Page:
        """Mark a page as manually organized without changing user notes."""

        return self.database.update_page(
            page_id,
            status=PageStatus.REVIEWED,
            processing_error="",
        )

    def skip_page(self, page_id: int) -> Page:
        """Explicitly defer a page so it leaves the default review queue."""

        return self.database.update_page(page_id, status=PageStatus.SKIPPED)

    def skip_page_and_next(
        self, page_id: int, *, queue_document_id: int | None = None
    ) -> tuple[Page, Page | None]:
        """Skip one page and return the next page remaining in the queue."""

        updated = self.skip_page(page_id)
        next_page = self.database.get_adjacent_review_page(
            updated.id,
            "next",
            queue_document_id,
        )
        return updated, next_page

    def reprocess_page(self, page_id: int) -> Page:
        """Retry local rendering/text extraction for one failed page."""

        page = self.database.get_page(page_id)
        if page is None:
            raise DocumentImportError(f"找不到页面：{page_id}")
        document = self.database.get_document(page.document_id)
        if document is None:
            raise DocumentImportError(f"找不到页面所属文档：{page.document_id}")
        processed = self.pdf_service.process_page(
            document.source_path,
            self.pages_dir / str(document.id),
            page.page_number,
            reuse_existing=True,
        )
        if processed.processing_error:
            return self.database.update_page(
                page.id,
                status=PageStatus.FAILED,
                processing_status="failed",
                processing_error=processed.processing_error,
            )
        processing_status = (
            "pending_review" if processed.needs_review else "text_extracted"
        )
        return self.database.update_page(
            page.id,
            extracted_text=processed.extracted_text,
            image_path=processed.image_path,
            status=PageStatus.PENDING,
            processing_status=processing_status,
            processing_error="",
        )

    def run_page_ocr(self, page_id: int) -> PageOcrResult:
        """Run local OCR for one page with failure isolated to that page.

        The page's rendered PNG is checked, the stage-A eligibility policy
        decides whether OCR may run, and only then is the injected engine
        required and called exactly once. A missing engine raises
        ``OcrUnavailable`` without touching the database; an eligible page
        that fails recognition keeps all existing content and records only
        a bounded OCR-prefixed ``processing_error``. ``extracted_text``,
        user Markdown, ``review_status`` and the PNG are never modified.
        """

        page = self.database.get_page(page_id)
        if page is None:
            raise DocumentImportError(f"找不到页面：{page_id}")

        image_path = Path(page.image_path)
        image_available = self._page_image_available(image_path)
        if not is_page_eligible_for_ocr(
            extracted_text=page.extracted_text,
            ocr_text=page.ocr_text,
            image_available=image_available,
            minimum_text_length=self.pdf_service.minimum_text_length,
        ):
            return PageOcrResult(page=page, outcome=PageOcrOutcome.NOT_ELIGIBLE)

        engine = require_ocr_engine(self.ocr_engine)
        try:
            recognized = engine.recognize(image_path)
        except OcrExecutionError as exc:
            return self._record_page_ocr_failure(page, str(exc))
        if not isinstance(recognized, str):
            return self._record_page_ocr_failure(page, "引擎返回了非文本结果。")

        cleared_error = _clear_ocr_error(page.processing_error)
        update_fields: dict[str, object] = {
            "ocr_text": recognized,
            "processing_status": "ocr_completed",
        }
        if cleared_error != page.processing_error:
            update_fields["processing_error"] = cleared_error
        updated = self.database.update_page(page.id, **update_fields)  # type: ignore[arg-type]
        LOGGER.info(
            "页面 OCR 完成：document_id=%s page_number=%s",
            page.document_id,
            page.page_number,
        )
        return PageOcrResult(page=updated, outcome=PageOcrOutcome.COMPLETED)

    def _record_page_ocr_failure(self, page: Page, message: str) -> PageOcrResult:
        """Persist a bounded OCR-prefixed error without touching page content."""

        detail = _sanitize_ocr_error_message(message)
        ocr_error = OCR_ERROR_PREFIX + detail
        merged_error = _merge_ocr_error(page.processing_error, ocr_error)
        updated = self.database.update_page(page.id, processing_error=merged_error)
        LOGGER.warning(
            "页面 OCR 失败：document_id=%s page_number=%s error=%s",
            page.document_id,
            page.page_number,
            ocr_error,
        )
        return PageOcrResult(page=updated, outcome=PageOcrOutcome.FAILED)

    @staticmethod
    def _page_image_available(image_path: Path) -> bool:
        """Whether the page PNG is a non-empty regular file, without reading it."""

        try:
            return image_path.is_file() and image_path.stat().st_size > 0
        except OSError:
            return False

    def _create_import_record(
        self, filename: str, title: str, sha256: str
    ) -> ImportRecord | None:
        create_record = getattr(self.database, "create_import_record", None)
        if not callable(create_record):
            return None
        record = create_record(filename, title, sha256)
        update_record = getattr(self.database, "update_import_record", None)
        if callable(update_record):
            return update_record(record.id, status=ImportStatus.PROCESSING)
        return record

    def _finish_import_record(
        self, record: ImportRecord | None, **values: object
    ) -> ImportRecord | None:
        if record is None:
            return None
        update_record = getattr(self.database, "update_import_record", None)
        if not callable(update_record):
            return None
        return update_record(record.id, **values)

    def _record_result(
        self, record: ImportRecord | None, document: Document, pages: tuple[Page, ...]
    ) -> ImportRecord | None:
        if record is None:
            return None
        failed_pages = sum(page.status is PageStatus.FAILED for page in pages)
        text_pages = sum(
            page.processing_status in {"text_extracted", "ocr_completed"}
            for page in pages
        )
        review_pages = sum(
            page.status in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}
            for page in pages
        )
        return self._finish_import_record(
            record,
            status=document.import_status,
            document_id=document.id,
            total_pages=document.page_count,
            processed_pages=len(pages),
            text_pages=text_pages,
            review_pages=review_pages,
            failed_pages=failed_pages,
            error_message=document.import_error,
        )

    def _record_failure(
        self,
        record: ImportRecord | None,
        document: Document | None,
        message: str,
        *,
        record_context: tuple[str, str, str] | None = None,
    ) -> None:
        if record is None and record_context is not None:
            # The failure happened before the import record was created (for
            # example while probing for a duplicate). Create the record now on
            # a best-effort basis so the failure stays diagnosable; a failure
            # of this write itself must never mask the original exception.
            filename, title, sha256 = record_context
            try:
                record = self._create_import_record(filename, title, sha256)
            except Exception:
                LOGGER.exception("无法为本次导入失败创建 import record")
        try:
            self._finish_import_record(
                record,
                status=ImportStatus.FAILED,
                document_id=document.id if document is not None else None,
                error_message=message,
            )
            if document is not None:
                update_import = getattr(self.database, "update_document_import", None)
                if callable(update_import):
                    update_import(
                        document.id,
                        status=ImportStatus.FAILED,
                        page_count=document.page_count,
                        processed_pages=document.processed_page_count,
                        text_pages=document.text_page_count,
                        review_pages=document.review_page_count,
                        error_message=message,
                    )
        except Exception:
            LOGGER.exception("记录导入失败状态时发生错误")

    @staticmethod
    def _read_upload(
        file_content: bytes | bytearray | memoryview | BinaryIO,
    ) -> bytes:
        """Read common Streamlit upload values while restoring stream position."""

        if isinstance(file_content, (bytes, bytearray, memoryview)):
            return bytes(file_content)

        original_position: int | None = None
        try:
            if file_content.seekable():
                original_position = file_content.tell()
                file_content.seek(0)
            value = file_content.read()
        except (AttributeError, OSError) as exc:
            raise DocumentImportError(f"无法读取上传文件：{exc}") from exc
        finally:
            if original_position is not None:
                try:
                    file_content.seek(original_position)
                except OSError:
                    LOGGER.warning("无法恢复上传文件的读取位置。", exc_info=True)

        if not isinstance(value, bytes):
            raise DocumentImportError("上传内容不是有效的二进制 PDF 数据。")
        return value

    @staticmethod
    def _safe_pdf_filename(filename: str) -> str:
        """Return a traversal-safe display filename while preserving the PDF suffix."""

        if not isinstance(filename, str):
            raise TypeError("文件名必须是字符串。")
        basename = Path(filename).name
        sanitized = INVALID_FILENAME_CHARACTERS.sub("_", basename).strip(" .")
        if not sanitized:
            raise DocumentImportError("PDF 文件名为空或无效。")
        if Path(sanitized).suffix.lower() != ".pdf":
            raise DocumentImportError("仅支持导入扩展名为 .pdf 的文件。")
        return sanitized

    @staticmethod
    def _safe_document_filename(filename: str) -> str:
        """Return a traversal-safe filename for supported user documents."""

        if not isinstance(filename, str):
            raise TypeError("文件名必须是字符串。")
        basename = Path(filename).name
        sanitized = INVALID_FILENAME_CHARACTERS.sub("_", basename).strip(" .")
        if not sanitized:
            raise DocumentImportError("文件名为空或无效。")
        if Path(sanitized).suffix.lower() not in {".pdf", *OFFICE_EXTENSIONS}:
            raise DocumentImportError("目前支持 PDF、Word 和 PowerPoint 文件。")
        return sanitized

    def _choose_original_path(self, sha256: str, filename: str) -> Path:
        """Return a content-addressed path for a non-PDF uploaded original."""

        filename_path = Path(filename)
        stem = filename_path.stem[:MAX_STORED_STEM_LENGTH].rstrip(" .") or "document"
        return self.raw_dir / "originals" / f"{sha256}_{stem}{filename_path.suffix.lower()}"

    def _save_original_document(self, path: Path, content: bytes, sha256: str) -> None:
        """Atomically save one Office original without overwriting unrelated data."""

        resolved_root = (self.raw_dir / "originals").resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if resolved_path.parent != resolved_root:
            raise DocumentImportError("原文件目标路径不在受管目录内。")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and self.pdf_service.calculate_sha256(path) == sha256:
            return
        temporary = path.with_name(f"{path.name}{TEMP_RAW_INFIX}{uuid4().hex[:12]}")
        try:
            _write_pdf_content(temporary, content)
            if self.pdf_service.calculate_sha256(temporary) != sha256:
                raise DocumentImportError("原文件保存后校验失败。")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _choose_raw_path(self, sha256: str, filename: str) -> Path:
        """Return the content-addressed raw path for one upload.

        The SHA-256 prefix ties the path to exactly one byte sequence, so an
        existing file at this path either is that content (reuse) or is
        interrupted-save residue of the same target (rebuilt atomically by
        :meth:`_save_raw_pdf`) — never a different document's file to protect.
        """

        filename_path = Path(filename)
        stem = filename_path.stem[:MAX_STORED_STEM_LENGTH].rstrip(" .") or "document"
        return self.raw_dir / f"{sha256}_{stem}.pdf"

    def _require_managed_raw_path(self, raw_path: Path) -> None:
        """Refuse to create or replace anything outside the managed raw directory."""

        resolved_dir = self.raw_dir.resolve(strict=False)
        resolved_path = raw_path.resolve(strict=False)
        if resolved_path.parent != resolved_dir:
            raise DocumentImportError(f"原文件目标路径不在受管 raw 目录内：{raw_path}")

    def _save_raw_pdf(self, raw_path: Path, content: bytes, sha256: str) -> None:
        """Persist the uploaded PDF through a verified temp file and atomic replace.

        An existing file at the content-addressed target is reused untouched
        only when its SHA-256 equals the upload's. A zero-byte, unreadable, or
        hash-mismatching file there is interrupted-save residue of this same
        target and is rebuilt in place — never deleted first, never written to
        directly — by writing a unique same-directory temp file, verifying its
        SHA-256, and only then ``os.replace``-ing it onto the final path, so a
        killed or failed save never leaves a new partial formal file behind.
        """

        self._require_managed_raw_path(raw_path)
        if raw_path.is_file():
            try:
                if self.pdf_service.calculate_sha256(raw_path) == sha256:
                    return
            except OSError:
                LOGGER.warning(
                    "无法读取已存在的原文件，将按中断残留重建：%s", raw_path, exc_info=True
                )
            else:
                LOGGER.warning("已存在的原文件校验不一致，按中断残留重建：%s", raw_path)
        _cleanup_stale_temp_raws(raw_path)
        temp_path = raw_path.with_name(
            f"{raw_path.name}{TEMP_RAW_INFIX}{uuid4().hex[:12]}"
        )
        try:
            _write_pdf_content(temp_path, content)
            if self.pdf_service.calculate_sha256(temp_path) != sha256:
                raise DocumentImportError(f"PDF 保存后校验失败：{raw_path}")
            os.replace(temp_path, raw_path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("无法删除失败的临时原文件：%s", temp_path)
            raise


__all__ = [
    "DocumentImportError",
    "DocumentService",
    "ImportResult",
    "PageOcrOutcome",
    "PageOcrResult",
    "first_reviewable_import_page",
]
