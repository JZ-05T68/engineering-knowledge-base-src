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
    """Manual review state of an imported PDF page."""

    PENDING = "pending"
    DRAFT = "draft"
    REVIEWED = "reviewed"
    SKIPPED = "skipped"
    FAILED = "failed"

    # Source-level aliases retained for v0.0.1/v0.0.2 callers. Persisted legacy
    # values are translated by schema v3 and ``_coerce_page_status``.
    READY = "pending"
    TEXT_EXTRACTED = "pending"
    OCR_COMPLETED = "pending"
    PENDING_REVIEW = "pending"
    MANUALLY_REVIEWED = "reviewed"

    @property
    def label(self) -> str:
        """Return the concise Chinese label shown in the local UI."""

        return {
            self.PENDING: "待处理",
            self.DRAFT: "草稿待复核",
            self.REVIEWED: "人工复核完成",
            self.SKIPPED: "暂不整理",
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
    note_updated_at: datetime | None = None
    reviewed_at: datetime | None = None
    last_viewed_at: datetime | None = None
    processing_status: str = "pending_review"

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
    """Local dashboard counts, including the complete review-state breakdown."""

    documents: int
    pages: int
    noted_pages: int
    review_pages: int
    tags: int
    projects: int
    pending_pages: int = 0
    draft_pages: int = 0
    reviewed_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0

    @property
    def processed_pages(self) -> int:
        """Pages whose manual-review workflow is complete or intentionally deferred."""

        return self.reviewed_pages + self.skipped_pages

    @property
    def remaining_pages(self) -> int:
        """Pages still present in the default review queue."""

        return self.pending_pages + self.draft_pages + self.failed_pages


@dataclass(frozen=True, slots=True)
class ReviewProgress:
    """Manual-review progress for the whole library or one document."""

    processed: int
    total: int
    remaining: int
    pending: int
    draft: int
    reviewed: int
    skipped: int
    failed: int
