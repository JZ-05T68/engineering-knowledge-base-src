"""Application service for durable local document and Markdown imports."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from uuid import uuid4

from src.models import Document, ImportRecord, ImportStatus, Page, PageStatus
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


class DocumentImportError(RuntimeError):
    """Raised when a document cannot be imported into local storage safely."""


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
    ) -> None:
        self.database = database
        self.raw_dir = Path(raw_dir)
        self.pages_dir = Path(pages_dir)
        self.markdown_dir = Path(markdown_dir)
        self.pdf_service = pdf_service or PdfService()

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
        import_record = self._create_import_record(safe_filename, document_title, sha256)
        source_path: Path | None = None
        document: Document | None = None
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
                    completed_record = None
                    if import_record is not None:
                        completed_record = self._finish_import_record(
                            import_record,
                            status=ImportStatus.COMPLETED,
                            document_id=existing_document.id,
                            total_pages=recorded_page_count,
                            processed_pages=len(existing_pages),
                            text_pages=sum(
                                page.processing_status
                                in {"text_extracted", "ocr_completed"}
                                for page in existing_pages
                            ),
                            review_pages=sum(
                                page.status
                                in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}
                                for page in existing_pages
                            ),
                            failed_pages=sum(
                                page.status is PageStatus.FAILED for page in existing_pages
                            ),
                            error_message="该文件已经导入",
                        )
                    return ImportResult(
                        document=existing_document,
                        pages=existing_pages,
                        duplicate=True,
                        import_record=completed_record,
                    )

                source_path = Path(existing_document.source_path)
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
            if not source_path.exists():
                with source_path.open("xb") as destination:
                    destination.write(content)
            stored_sha256 = self.pdf_service.calculate_sha256(source_path)
            if stored_sha256 != sha256:
                raise DocumentImportError(
                    f"PDF 保存后校验失败，文件已保留以便排查：{source_path}"
                )

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
            self._record_failure(import_record, document, str(exc))
            LOGGER.exception("PDF 导入失败，已保留已写入的原文件和页面图片：%s", source_path)
            raise
        except Exception as exc:
            self._record_failure(import_record, document, str(exc))
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

    def delete_document(self, document_id: int, *, confirmed: bool = False) -> None:
        """Delete one document after explicit confirmation and clean only its files."""

        if not confirmed:
            raise DocumentImportError("删除文档需要明确二次确认。")
        document = self.database.get_document(document_id)
        if document is None:
            raise DocumentImportError(f"找不到文档：{document_id}")
        pages = self.database.list_pages(document_id)
        self.database.delete_document(document_id)
        failures: list[str] = []
        paths = [document.source_path]
        paths.extend(page.image_path for page in pages)
        paths.extend(page.markdown_path for page in pages if page.markdown_path is not None)
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        for directory in (self.pages_dir / str(document_id), self.markdown_dir / str(document_id)):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("文档目录非空，已保留：%s", directory)
        if failures:
            raise DocumentImportError(
                "文档记录已删除，但部分独占文件未能清理：" + "；".join(failures)
            )

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
        self, record: ImportRecord | None, document: Document | None, message: str
    ) -> None:
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

    def _choose_raw_path(self, sha256: str, filename: str) -> Path:
        """Choose a non-overwriting local path, reusing only byte-identical files."""

        filename_path = Path(filename)
        stem = filename_path.stem[:MAX_STORED_STEM_LENGTH].rstrip(" .") or "document"
        candidate = self.raw_dir / f"{sha256}_{stem}.pdf"
        suffix_number = 1
        while candidate.exists():
            try:
                if self.pdf_service.calculate_sha256(candidate) == sha256:
                    return candidate
            except OSError:
                LOGGER.warning("无法读取已存在的原文件，将使用新路径：%s", candidate, exc_info=True)
            candidate = self.raw_dir / f"{sha256}_{stem}_{suffix_number}.pdf"
            suffix_number += 1
        return candidate


__all__ = [
    "DocumentImportError",
    "DocumentService",
    "ImportResult",
    "first_reviewable_import_page",
]
