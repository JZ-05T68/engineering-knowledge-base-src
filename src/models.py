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


class SearchField(StrEnum):
    """Searchable local field used for source-aware filtering and display."""

    EXTRACTED_TEXT = "extracted_text"
    OCR_TEXT = "ocr_text"
    MARKDOWN = "markdown"
    DOCUMENT_TITLE = "document_title"
    FILENAME = "filename"
    TAG = "tag"
    PROJECT = "project"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in search filters and result cards."""

        return {
            self.EXTRACTED_TEXT: "页面提取文本",
            self.OCR_TEXT: "OCR 文本",
            self.MARKDOWN: "用户 Markdown 笔记",
            self.DOCUMENT_TITLE: "文档标题",
            self.FILENAME: "原始文件名",
            self.TAG: "标签",
            self.PROJECT: "项目",
        }[self]


class SearchSort(StrEnum):
    """Supported, SQL-whitelisted search result orders."""

    RELEVANCE = "relevance"
    DOCUMENT_PAGE = "document_page"
    VIEWED_DESC = "viewed_desc"
    UPDATED_DESC = "updated_desc"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the search UI."""

        return {
            self.RELEVANCE: "相关度",
            self.DOCUMENT_PAGE: "文档与页码",
            self.VIEWED_DESC: "最近查看",
            self.UPDATED_DESC: "最近修改",
        }[self]


class ReviewQueueSort(StrEnum):
    """Supported, SQL-whitelisted orders for the manual-review queue."""

    DOCUMENT_PAGE = "document_page"


class SearchViewMode(StrEnum):
    """Supported search-result layouts stored in the URL state."""

    PAGE = "page"
    DOCUMENT = "document"

    @property
    def label(self) -> str:
        """Return the concise Chinese label shown by the view switcher."""

        return {
            self.PAGE: "按页面显示",
            self.DOCUMENT: "按文档分组显示",
        }[self]


class EvidenceTextKind(StrEnum):
    """Trust classification for text stored in an evidence item."""

    ORIGINAL = "original_material"
    USER_EXCERPT = "user_excerpt"

    @property
    def label(self) -> str:
        """Return a clear Chinese label without overstating source verification."""

        return {
            self.ORIGINAL: "已匹配原始材料",
            self.USER_EXCERPT: "用户摘录（未经原文匹配确认）",
        }[self]


class EvidenceContextKind(StrEnum):
    """Provenance of context stored alongside an evidence selection."""

    SYSTEM_GENERATED = "system_generated"
    USER_PROVIDED = "user_provided"

    @property
    def label(self) -> str:
        """Return the provenance label used in evidence exports."""

        return {
            self.SYSTEM_GENERATED: "系统生成的上下文 / 摘要",
            self.USER_PROVIDED: "用户提供的上下文",
        }[self]


class NoteType(StrEnum):
    """The four structured note scopes frozen for v0.3.0."""

    DOCUMENT = "document"
    PAGE = "page"
    TEXT_SELECTION = "text_selection"
    IMAGE_REGION = "image_region"

    @property
    def label(self) -> str:
        """Return the Chinese type label used in note lists."""

        return {
            self.DOCUMENT: "文档级笔记",
            self.PAGE: "页面级笔记",
            self.TEXT_SELECTION: "文字选区笔记",
            self.IMAGE_REGION: "图片区域笔记",
        }[self]


class NoteSourceStatus(StrEnum):
    """Freshness of a note's text or image anchor, recomputed on every read."""

    VALID = "valid"
    CHANGED = "changed"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    UNREADABLE = "unreadable"

    @property
    def label(self) -> str:
        """Return the Chinese status label shown beside anchored notes."""

        return {
            self.VALID: "锚点有效",
            self.CHANGED: "来源已变化，无法重新定位",
            self.MISSING: "来源不存在",
            self.UNAVAILABLE: "来源暂时无法读取",
            self.UNREADABLE: "页面图像无法读取",
        }[self]


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Composable search filters; multiple tags and projects use AND semantics."""

    document_ids: tuple[int, ...] = ()
    project_ids: tuple[int, ...] = ()
    tag_ids: tuple[int, ...] = ()
    statuses: tuple[PageStatus, ...] = ()
    match_fields: tuple[SearchField, ...] = ()
    has_note: bool = False
    evidence_basket_id: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewQueueQuery:
    """Normalized queue scope; ``batch_number`` is explicitly one-based."""

    document_id: int | None
    statuses: tuple[PageStatus, ...]
    sort: ReviewQueueSort
    batch_size: int
    batch_number: int


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
class ReviewQueuePage:
    """One bounded, one-based page of the manual-review queue."""

    pages: tuple[Page, ...]
    total_pages: int
    batch_size: int
    batch_number: int
    total_batches: int
    requested_batch_number: int
    corrected: bool
    query: ReviewQueueQuery

    @property
    def visible_page_ids(self) -> tuple[int, ...]:
        """Return stable IDs in exactly the database result order."""

        return tuple(page.id for page in self.pages)


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
    match_fields: tuple[SearchField, ...] = ()
    document_source_path: Path | None = None
    document_sha256: str = ""
    extracted_text: str = ""
    ocr_text: str = ""
    markdown_content: str = ""
    updated_at: datetime | None = None
    match_count: int = 0
    snippets: tuple[SearchSnippet, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchSnippet:
    """One source-labelled, plain-text excerpt for a search result."""

    field: SearchField
    text: str
    match_count: int


@dataclass(frozen=True, slots=True)
class SearchFacetCounts:
    """Context-aware counts for the result set and selectable facet values."""

    total: int
    statuses: dict[PageStatus, int]
    documents: dict[int, int]
    projects: dict[int, int]
    tags: dict[int, int]


@dataclass(frozen=True, slots=True)
class EvidenceBasket:
    """A durable, locally stored collection of page-level evidence."""

    id: int
    name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One ordered, source-linked evidence selection in a basket."""

    id: int
    basket_id: int
    document_id: int
    page_id: int
    document_title: str
    filename: str
    page_number: int
    review_status: PageStatus
    projects: tuple[str, ...]
    tags: tuple[str, ...]
    evidence_text: str
    text_kind: EvidenceTextKind
    context: str
    context_kind: EvidenceContextKind
    user_note: str
    source_text_sha256: str
    source_locator: str
    added_at: datetime
    position: int
    document_source_path: Path | None = None
    image_path: Path | None = None
    document_sha256: str = ""
    from_ocr_text: bool = False


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


@dataclass(frozen=True, slots=True)
class Note:
    """One structured note row; anchor fields are populated per ``note_type``."""

    id: int
    note_type: NoteType
    document_id: int | None
    page_id: int | None
    personal_note: str
    created_at: datetime
    updated_at: datetime
    source_kind: str | None = None
    source_page_text_sha256: str | None = None
    source_excerpt_snapshot: str | None = None
    selection_start: int | None = None
    selection_end: int | None = None
    user_excerpt: str | None = None
    region_image_sha256: str | None = None
    region_image_width: int | None = None
    region_image_height: int | None = None
    region_x0: int | None = None
    region_y0: int | None = None
    region_x1: int | None = None
    region_y1: int | None = None


@dataclass(frozen=True, slots=True)
class NoteView:
    """A note plus the freshly recomputed status of its anchor.

    ``source_status`` is only set for anchored types (text selections and
    image regions); document and page notes always carry ``None``.
    """

    note: Note
    source_status: NoteSourceStatus | None = None


@dataclass(frozen=True, slots=True)
class TextSourcePreview:
    """Read-only view of the canonical text source for selection notes."""

    source_kind: str
    source_text: str

    @property
    def label(self) -> str:
        """Return the fixed Chinese label for the resolved source kind."""

        return {
            "pdf_text": "来源：PDF 文本层",
            "ocr_text": "来源：OCR 初稿",
        }[self.source_kind]
