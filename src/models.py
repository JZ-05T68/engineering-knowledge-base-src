"""Typed domain models shared by persistence, services, and Streamlit pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ImportStatus(StrEnum):
    """Lifecycle state for a PDF import attempt or stored document."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIALLY_COMPLETED = "partially_completed"


class PageStatus(StrEnum):
    """Processing and review state of an imported PDF page."""

    TEXT_EXTRACTED = "text_extracted"
    OCR_COMPLETED = "ocr_completed"
    PENDING_REVIEW = "pending_review"
    MANUALLY_REVIEWED = "manually_reviewed"
    FAILED = "failed"

    # v0.0.1 source compatibility for callers and existing tests.
    READY = "text_extracted"
    PENDING = "pending_review"

    @property
    def label(self) -> str:
        """Return the concise Chinese label shown in the local UI."""

        return {
            self.TEXT_EXTRACTED: "已提取文本",
            self.OCR_COMPLETED: "OCR 完成",
            self.PENDING_REVIEW: "待人工复核",
            self.MANUALLY_REVIEWED: "已人工整理",
            self.FAILED: "处理失败",
        }[self]


@dataclass(frozen=True, slots=True)
class Document:
    """Metadata and import statistics for one locally stored source PDF."""

    id: int
    title: str
    filename: str
    source_path: Path
    sha256: str
    page_count: int
    created_at: datetime
    updated_at: datetime
    import_status: ImportStatus = ImportStatus.COMPLETED
    processed_page_count: int = 0
    text_page_count: int = 0
    review_page_count: int = 0
    import_error: str = ""
    imported_at: datetime | None = None

    @property
    def status_label(self) -> str:
        """Return a Chinese import status label."""

        return {
            ImportStatus.PENDING: "等待导入",
            ImportStatus.PROCESSING: "正在导入",
            ImportStatus.COMPLETED: "导入完成",
            ImportStatus.FAILED: "导入失败",
            ImportStatus.PARTIALLY_COMPLETED: "部分完成",
        }[self.import_status]


@dataclass(frozen=True, slots=True)
class Page:
    """Metadata and searchable content for one document page."""

    id: int
    document_id: int
    page_number: int
    image_path: Path
    extracted_text: str
    ocr_text: str
    markdown_content: str
    markdown_path: Path | None
    status: PageStatus
    processing_error: str
    created_at: datetime
    updated_at: datetime

    @property
    def searchable_content(self) -> str:
        """Prefer reviewed Markdown, then OCR, then extracted PDF text."""

        return (
            self.markdown_content.strip()
            or self.ocr_text.strip()
            or self.extracted_text.strip()
        )

    @property
    def has_note(self) -> bool:
        """Whether this page has a non-empty user-authored note."""

        return bool(self.markdown_content.strip())


@dataclass(frozen=True, slots=True)
class Tag:
    """Reusable label associated with documents and pages."""

    id: int
    name: str
    created_at: datetime
    usage_count: int = 0


@dataclass(frozen=True, slots=True)
class Project:
    """Local organizational container for related engineering materials."""

    id: int
    name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    document_count: int = 0
    page_count: int = 0


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """Durable audit record for one PDF import attempt."""

    id: int
    filename: str
    title: str
    sha256: str
    status: ImportStatus
    document_id: int | None
    total_pages: int
    processed_pages: int
    text_pages: int
    review_pages: int
    failed_pages: int
    error_message: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A citation-ready full-text or metadata search result."""

    page_id: int
    document_id: int
    document_title: str
    filename: str
    page_number: int
    image_path: Path
    content: str
    snippet: str
    rank: float
    status: PageStatus
    match_type: str = "页面内容"
    tags: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DashboardStats:
    """Small set of counts used by the v0.0.2 home page."""

    documents: int
    pages: int
    noted_pages: int
    review_pages: int
    tags: int
    projects: int
