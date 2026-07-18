"""Local PDF hashing, rendering, and text extraction services."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class PdfProcessingError(RuntimeError):
    """Raised when a local PDF cannot be opened or processed completely."""


@dataclass(frozen=True, slots=True)
class ProcessedPage:
    """Result of rendering and extracting one PDF page."""

    page_number: int
    image_path: Path
    extracted_text: str
    needs_review: bool
    effective_text_length: int
    processing_error: str = ""


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
        reuses non-empty page images instead of overwriting them while still
        reopening the PDF to extract the page text again.
        """

        source_path = Path(pdf_path)
        destination = Path(output_dir)
        if not source_path.is_file():
            raise FileNotFoundError(f"找不到 PDF 文件：{source_path}")

        pymupdf = _load_pymupdf()
        try:
            document = pymupdf.open(str(source_path))
        except Exception as exc:
            LOGGER.exception("无法打开 PDF：%s", source_path)
            raise PdfProcessingError(f"无法打开 PDF 文件“{source_path.name}”：{exc}") from exc

        try:
            if getattr(document, "needs_pass", False):
                raise PdfProcessingError(f"PDF 文件“{source_path.name}”受密码保护，暂不支持导入。")

            page_count = int(document.page_count)
            if page_count <= 0:
                raise PdfProcessingError(f"PDF 文件“{source_path.name}”不包含可导入页面。")

            destination.mkdir(parents=True, exist_ok=True)
            scale = self.dpi / 72
            matrix = pymupdf.Matrix(scale, scale)
            processed_pages: list[ProcessedPage] = []

            for page_index in range(page_count):
                page_number = page_index + 1
                image_path = destination / f"page_{page_number:04d}.png"
                image_exists = image_path.exists()
                if image_exists and not reuse_existing:
                    raise PdfProcessingError(
                        f"第 {page_number} 页的目标图片已存在，为避免覆盖已停止导入：{image_path}"
                    )
                if image_exists and (
                    not image_path.is_file() or image_path.stat().st_size == 0
                ):
                    raise PdfProcessingError(
                        f"第 {page_number} 页已有图片不完整，程序不会自动覆盖：{image_path}"
                    )

                try:
                    page = document.load_page(page_index)
                    if not image_exists:
                        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                        pixmap.save(str(image_path))
                        if not image_path.is_file() or image_path.stat().st_size == 0:
                            raise PdfProcessingError(
                                f"第 {page_number} 页的 PNG 图片未能完整写入：{image_path}"
                            )
                    extracted_text = (page.get_text("text") or "").strip()
                except Exception as exc:
                    LOGGER.exception(
                        "处理 PDF 页面失败：file=%s page=%s", source_path, page_number
                    )
                    processed_pages.append(
                        ProcessedPage(
                            page_number=page_number,
                            image_path=image_path,
                            extracted_text="",
                            needs_review=True,
                            effective_text_length=0,
                            processing_error=f"第 {page_number} 页处理失败：{exc}",
                        )
                    )
                    if on_progress is not None:
                        on_progress(page_number, page_count)
                    continue

                effective_length = self.effective_text_length(extracted_text)
                processed_pages.append(
                    ProcessedPage(
                        page_number=page_number,
                        image_path=image_path,
                        extracted_text=extracted_text,
                        needs_review=effective_length < self.minimum_text_length,
                        effective_text_length=effective_length,
                    )
                )
                if on_progress is not None:
                    on_progress(page_number, page_count)

            return processed_pages
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

__all__ = ["PDFService", "PdfProcessingError", "PdfService", "ProcessedPage"]
