"""Local PDF hashing, rendering, and text extraction services."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

# Page images are rendered into a sibling temporary file first and atomically
# moved into place only after the temp file is verified complete and decodable.
# The suffix keeps temp files recognizable and invisible to page-PNG scans
# (``Path("page_0001.png.tmp-x").suffix`` is not ``.png``).
TEMP_IMAGE_INFIX = ".tmp-"


class PdfProcessingError(RuntimeError):
    """Raised when a local PDF cannot be opened or processed completely."""


@dataclass(frozen=True, slots=True)
class PageDiagnostics:
    """Deterministic, independently flagged geometry/text facts about one page.

    ``width``/``height`` describe the page's display geometry (PyMuPDF
    ``page.rect``, which already reflects rotation); ``rotation`` is the
    normalized ``page.rotation`` value. The boolean flags are independent by
    design: one page may be landscape, rotated and short-text at once.
    """

    width: float = 0.0
    height: float = 0.0
    rotation: int = 0
    effective_char_count: int = 0
    is_blank: bool = True
    is_short_text: bool = False
    is_landscape: bool = False
    is_rotated: bool = False


@dataclass(frozen=True, slots=True)
class ProcessedPage:
    """Result of rendering and extracting one PDF page."""

    page_number: int
    image_path: Path
    extracted_text: str
    needs_review: bool
    effective_text_length: int
    processing_error: str = ""
    diagnostics: PageDiagnostics = field(default_factory=PageDiagnostics)


@dataclass(frozen=True, slots=True)
class DocumentDiagnosticsSummary:
    """Deterministic document-level aggregation of one import's page results.

    Content-type counts (blank/short/landscape/rotated) cover only pages that
    were processed successfully: a failed page carries default diagnostics and
    must not be mistaken for a blank page. ``needs_review`` likewise counts
    only successful pages, because the flag on a failed page is a meaningless
    default. Page-number tuples are sorted and duplicate-free, and each count
    equals the length of its page-number tuple.
    """

    total_pages: int = 0
    successful_pages: int = 0
    failed_pages: int = 0
    blank_pages: int = 0
    short_text_pages: int = 0
    landscape_pages: int = 0
    rotated_pages: int = 0
    needs_review_pages: int = 0
    failed_page_numbers: tuple[int, ...] = ()
    blank_page_numbers: tuple[int, ...] = ()
    short_text_page_numbers: tuple[int, ...] = ()
    landscape_page_numbers: tuple[int, ...] = ()
    rotated_page_numbers: tuple[int, ...] = ()
    needs_review_page_numbers: tuple[int, ...] = ()


def summarize_page_diagnostics(
    pages: Sequence[ProcessedPage],
) -> DocumentDiagnosticsSummary:
    """Aggregate processed pages into a deterministic document summary.

    Pure function: no file I/O, no database access, no PDF re-opening, no
    mutation of the input pages, and no recomputation of text length or page
    geometry — it only reads existing ``ProcessedPage``/``PageDiagnostics``
    fields.
    """

    successful = [page for page in pages if not page.processing_error]
    failed = [page for page in pages if page.processing_error]

    def _numbers(selected: Iterable[ProcessedPage]) -> tuple[int, ...]:
        return tuple(sorted({page.page_number for page in selected}))

    blank = [page for page in successful if page.diagnostics.is_blank]
    short = [page for page in successful if page.diagnostics.is_short_text]
    landscape = [page for page in successful if page.diagnostics.is_landscape]
    rotated = [page for page in successful if page.diagnostics.is_rotated]
    review = [page for page in successful if page.needs_review]
    return DocumentDiagnosticsSummary(
        total_pages=len(pages),
        successful_pages=len(successful),
        failed_pages=len(failed),
        blank_pages=len(blank),
        short_text_pages=len(short),
        landscape_pages=len(landscape),
        rotated_pages=len(rotated),
        needs_review_pages=len(review),
        failed_page_numbers=_numbers(failed),
        blank_page_numbers=_numbers(blank),
        short_text_page_numbers=_numbers(short),
        landscape_page_numbers=_numbers(landscape),
        rotated_page_numbers=_numbers(rotated),
        needs_review_page_numbers=_numbers(review),
    )


def _load_pymupdf() -> Any:
    """Load PyMuPDF while supporting both its current and legacy import names."""

    try:
        import pymupdf

        return pymupdf
    except ImportError:
        try:
            import fitz

            return fitz
        except ImportError as exc:
            raise PdfProcessingError(
                "缺少 PyMuPDF，无法处理 PDF。请先安装 requirements.txt 中的依赖。"
            ) from exc


def is_complete_png(path: Path) -> bool:
    """Return True only for an existing, non-empty, fully decodable PNG.

    Existence and a positive size are not enough: an interrupted write can
    leave a truncated file that a strict decode rejects.  Any failure means
    "incomplete", so callers can safely re-render instead of reusing or
    refusing the file.
    """

    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        pixmap = _load_pymupdf().Pixmap(str(path))
        return pixmap.width > 0 and pixmap.height > 0
    except Exception:
        return False


def _cleanup_stale_temp_images(image_path: Path) -> None:
    """Remove temp renders of this exact page left by killed earlier runs."""

    for stale in image_path.parent.glob(f"{image_path.name}{TEMP_IMAGE_INFIX}*"):
        try:
            stale.unlink()
        except OSError:
            LOGGER.warning("无法清理旧临时页图：%s", stale)


def render_page_image_atomically(
    page: Any, image_path: Path, matrix: Any, page_number: int
) -> None:
    """Render one page image and move it into place atomically.

    The pixmap is written to a unique temp file in the same directory (same
    filesystem), verified non-empty and decodable, then ``os.replace``d onto
    the final path — Windows-atomic even when replacing a corrupt previous
    file, so a verified image is never deleted to make room and a failed
    render never leaves a plausible-looking final PNG behind.
    """

    _cleanup_stale_temp_images(image_path)
    temp_path = image_path.with_name(
        f"{image_path.name}{TEMP_IMAGE_INFIX}{uuid4().hex[:12]}"
    )
    try:
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        # tobytes() pins the PNG encoder explicitly: the temp name carries no
        # .png extension (by design), so save() cannot infer the format.
        temp_path.write_bytes(pixmap.tobytes("png"))
        if not is_complete_png(temp_path):
            raise PdfProcessingError(
                f"第 {page_number} 页的 PNG 图片未能完整写入：{image_path}"
            )
        os.replace(temp_path, image_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("无法删除失败的临时页图：%s", temp_path)
        raise


class PdfService:
    """Render PDF pages to PNG and extract any available text layer."""

    def __init__(self, minimum_text_length: int = 20, dpi: int = 150) -> None:
        if minimum_text_length < 0:
            raise ValueError("最少有效文本长度不能小于 0。")
        if dpi < 72:
            raise ValueError("PDF 渲染 DPI 不能小于 72。")
        self.minimum_text_length = minimum_text_length
        self.dpi = dpi

    @staticmethod
    def calculate_sha256(file_path: Path | str, chunk_size: int = 1024 * 1024) -> str:
        """Return the SHA-256 digest of a file without loading it all into memory."""

        path = Path(file_path)
        if chunk_size <= 0:
            raise ValueError("哈希读取块大小必须大于 0。")
        if not path.is_file():
            raise FileNotFoundError(f"找不到待计算哈希的文件：{path}")

        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def effective_text_length(text: str) -> int:
        """Count meaningful Unicode letters and digits, ignoring layout noise."""

        return sum(character.isalnum() for character in text)

    def _diagnose_page(self, page: Any, effective_char_count: int) -> PageDiagnostics:
        """Build deterministic diagnostics from the shared page and text facts.

        ``effective_char_count`` is the exact value already computed for the
        ``needs_review`` decision, so both share one counting rule.
        """

        width = float(page.rect.width)
        height = float(page.rect.height)
        rotation = int(page.rotation) % 360
        return PageDiagnostics(
            width=width,
            height=height,
            rotation=rotation,
            effective_char_count=effective_char_count,
            is_blank=effective_char_count == 0,
            is_short_text=0 < effective_char_count < self.minimum_text_length,
            is_landscape=width > height,
            is_rotated=rotation != 0,
        )

    @staticmethod
    def _open_document(source_path: Path) -> tuple[Any, Any]:
        """Open a local PDF after applying the shared safety checks."""

        if not source_path.is_file():
            raise FileNotFoundError(f"找不到 PDF 文件：{source_path}")

        pymupdf = _load_pymupdf()
        try:
            document = pymupdf.open(str(source_path))
        except Exception as exc:
            LOGGER.exception("无法打开 PDF：%s", source_path)
            raise PdfProcessingError(f"无法打开 PDF 文件“{source_path.name}”：{exc}") from exc

        if getattr(document, "needs_pass", False):
            document.close()
            raise PdfProcessingError(f"PDF 文件“{source_path.name}”受密码保护，暂不支持导入。")
        if int(document.page_count) <= 0:
            document.close()
            raise PdfProcessingError(f"PDF 文件“{source_path.name}”不包含可导入页面。")
        return pymupdf, document

    def _process_page(
        self,
        document: Any,
        page_index: int,
        destination: Path,
        matrix: Any,
        source_path: Path,
        *,
        reuse_existing: bool,
    ) -> ProcessedPage:
        """Render one page (unless a reusable image exists) and extract its text."""

        page_number = page_index + 1
        image_path = destination / f"page_{page_number:04d}.png"
        image_exists = image_path.exists()
        if image_exists and not reuse_existing:
            raise PdfProcessingError(
                f"第 {page_number} 页的目标图片已存在，为避免覆盖已停止导入：{image_path}"
            )
        if image_exists and not image_path.is_file():
            raise PdfProcessingError(
                f"第 {page_number} 页的图片路径不是常规文件，为避免误操作已停止导入：{image_path}"
            )

        # Reuse only a fully verified image; a zero-byte or undecodable file
        # is an interrupted write and is re-rendered (via a verified temp file
        # + atomic replace, never a delete-first overwrite).
        reuse_image = image_exists and reuse_existing and is_complete_png(image_path)

        try:
            page = document.load_page(page_index)
            if not reuse_image:
                render_page_image_atomically(page, image_path, matrix, page_number)
            extracted_text = (page.get_text("text") or "").strip()
        except Exception as exc:
            LOGGER.exception(
                "处理 PDF 页面失败：file=%s page=%s", source_path, page_number
            )
            return ProcessedPage(
                page_number=page_number,
                image_path=image_path,
                extracted_text="",
                needs_review=True,
                effective_text_length=0,
                processing_error=f"第 {page_number} 页处理失败：{exc}",
            )

        effective_length = self.effective_text_length(extracted_text)
        return ProcessedPage(
            page_number=page_number,
            image_path=image_path,
            extracted_text=extracted_text,
            needs_review=effective_length < self.minimum_text_length,
            effective_text_length=effective_length,
            diagnostics=self._diagnose_page(page, effective_length),
        )

    def process(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        *,
        reuse_existing: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[ProcessedPage]:
        """Render every page and extract text, preserving all generated page images.

        ``reuse_existing`` is reserved for recovering an interrupted import. It
        reuses page images that pass a strict completeness check (non-empty and
        decodable) instead of overwriting them, while zero-byte or truncated
        leftovers of a killed run are rendered again via a verified temp file
        and an atomic replace.  The PDF is always reopened to extract the page
        text again.
        """

        source_path = Path(pdf_path)
        destination = Path(output_dir)
        pymupdf, document = self._open_document(source_path)
        try:
            page_count = int(document.page_count)
            destination.mkdir(parents=True, exist_ok=True)
            scale = self.dpi / 72
            matrix = pymupdf.Matrix(scale, scale)
            processed_pages: list[ProcessedPage] = []

            for page_index in range(page_count):
                processed_pages.append(
                    self._process_page(
                        document,
                        page_index,
                        destination,
                        matrix,
                        source_path,
                        reuse_existing=reuse_existing,
                    )
                )
                if on_progress is not None:
                    on_progress(page_index + 1, page_count)

            return processed_pages
        finally:
            document.close()

    def process_page(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        page_number: int,
        *,
        reuse_existing: bool = False,
    ) -> ProcessedPage:
        """Render and extract exactly one page without touching any other page.

        ``page_number`` uses the same one-based numbering as the rest of the
        application. Out-of-range page numbers raise :class:`PdfProcessingError`
        instead of being wrapped into ``ProcessedPage.processing_error``.
        """

        source_path = Path(pdf_path)
        destination = Path(output_dir)
        pymupdf, document = self._open_document(source_path)
        try:
            page_count = int(document.page_count)
            if not 1 <= page_number <= page_count:
                raise PdfProcessingError(
                    f"页码 {page_number} 超出范围：PDF 文件“{source_path.name}”共有 "
                    f"{page_count} 页。"
                )
            destination.mkdir(parents=True, exist_ok=True)
            scale = self.dpi / 72
            matrix = pymupdf.Matrix(scale, scale)
            return self._process_page(
                document,
                page_number - 1,
                destination,
                matrix,
                source_path,
                reuse_existing=reuse_existing,
            )
        finally:
            document.close()

    def render_and_extract(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        *,
        reuse_existing: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[ProcessedPage]:
        """Compatibility alias with an explicit description of :meth:`process`."""

        return self.process(
            pdf_path,
            output_dir,
            reuse_existing=reuse_existing,
            on_progress=on_progress,
        )


# Keep the common acronym spelling available to callers without duplicating logic.
PDFService = PdfService

__all__ = [
    "DocumentDiagnosticsSummary",
    "PDFService",
    "PageDiagnostics",
    "PdfProcessingError",
    "PdfService",
    "ProcessedPage",
    "summarize_page_diagnostics",
]
