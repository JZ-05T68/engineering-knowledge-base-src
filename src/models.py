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


class SearchMode(StrEnum):
    """Supported search execution modes stored in the URL state.

    ``KEYWORD`` is the default and never triggers embedding. ``HYBRID`` is an
    explicit user opt-in that may trigger one query embedding per explicit
    search action; it is only honoured when every ``SearchFilters`` field is
    empty/default and ``sort`` is ``RELEVANCE``, otherwise it silently falls
    back to ``KEYWORD`` execution.
    """

    KEYWORD = "keyword"
    HYBRID = "hybrid"

    @property
    def label(self) -> str:
        """Return the Chinese label shown by the search-mode switcher."""

        return {
            self.KEYWORD: "关键词检索",
            self.HYBRID: "AI 混合检索",
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


class EvidenceType(StrEnum):
    """The three evidence anchor shapes introduced by schema v7 (v0.4.0)."""

    PAGE = "page"
    TEXT_SELECTION = "text_selection"
    IMAGE_REGION = "image_region"

    @property
    def label(self) -> str:
        """Return the Chinese type label used in evidence lists and exports."""

        return {
            self.PAGE: "整页证据",
            self.TEXT_SELECTION: "文字选区证据",
            self.IMAGE_REGION: "图片区域证据",
        }[self]


class EvidenceConfirmationStatus(StrEnum):
    """Manual confirmation state of one evidence item (schema v7)."""

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"

    @property
    def label(self) -> str:
        """Return the Chinese confirmation label shown beside evidence items."""

        return {
            self.UNCONFIRMED: "未确认",
            self.CONFIRMED: "已确认",
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


class NoteImportance(StrEnum):
    """The three semantic importance levels frozen for v0.3.1.

    These are business semantics persisted in the database — never colors.
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    NORMAL = "normal"

    @property
    def label(self) -> str:
        """Return the Chinese level label shown on badges."""

        return {
            self.PRIMARY: "重点",
            self.SECONDARY: "次重点",
            self.NORMAL: "一般",
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
    evidence_type: EvidenceType = EvidenceType.TEXT_SELECTION
    confirmation_status: EvidenceConfirmationStatus = EvidenceConfirmationStatus.UNCONFIRMED
    confirmed_at: datetime | None = None
    region_image_sha256: str | None = None
    region_image_width: int | None = None
    region_image_height: int | None = None
    region_x0: int | None = None
    region_y0: int | None = None
    region_x1: int | None = None
    region_y1: int | None = None


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
    importance: str = "normal"


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


@dataclass(frozen=True, slots=True)
class ImageSourcePreview:
    """Read-only identity facts of the stored page PNG for region notes."""

    path: Path
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NoteListItem:
    """One note row enriched for the standalone list page (single JOIN).

    ``source_status`` is computed inline for text selections (no extra file
    reads); image-region identity stays lazy and is checked only when the
    user explicitly opens a region preview.
    """

    note: Note
    document_id: int | None
    document_title: str | None
    page_number: int | None
    source_status: NoteSourceStatus | None = None


@dataclass(frozen=True, slots=True)
class NoteDisplayPreferences:
    """User-configurable badge background colors (presentation only).

    Colors are canonical lowercase ``#rrggbb``; they never carry business
    semantics. Foreground text color is derived by the UI, not stored.
    """

    color_primary: str = "#c0392b"
    color_secondary: str = "#2563eb"
    color_normal: str = "#000000"
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocumentDeletionFile:
    """One database-recorded file targeted by a document deletion.

    ``size_bytes`` is ``None`` when the record exists but the file itself is
    missing from disk; such files are reported, never silently skipped.
    """

    path: Path
    kind: str
    exists: bool
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class AggregationAxisImpact:
    """One organization axis whose aggregation view contains a document's knowledge."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class DocumentAggregationImpact:
    """Which project/tag aggregation views actually contain a document's knowledge.

    An axis is listed only when deleting the document would really change its
    aggregation view — that is, the document has at least one note or
    evidence entry that reaches the axis under the effective association
    semantics. A bare association with no knowledge entries is not an impact.
    """

    projects: tuple[AggregationAxisImpact, ...] = ()
    tags: tuple[AggregationAxisImpact, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentDeletionPreview:
    """Read-only impact summary of deleting one imported document.

    Counts cover exactly the rows removed by the schema v5 cascades plus the
    FTS cleanup trigger, and since v0.5.2 the polymorphic knowledge-object
    source links explicitly removed before the cascading delete.
    ``import_record_count`` is reported separately because those rows survive
    with ``document_id`` set to NULL (``ON DELETE SET NULL``). Projects and
    tags are shared entities and are never part of a deletion; files of other
    documents are never listed.
    """

    document_id: int
    document_title: str
    page_count: int
    document_note_count: int
    page_note_count: int
    text_selection_note_count: int
    image_region_note_count: int
    evidence_item_count: int
    search_record_count: int
    association_count: int
    import_record_count: int
    files: tuple[DocumentDeletionFile, ...]
    total_size_bytes: int
    missing_files: tuple[Path, ...]
    path_anomalies: tuple[str, ...]
    aggregation_impact: DocumentAggregationImpact = DocumentAggregationImpact()
    knowledge_object_source_count: int = 0

    @property
    def note_count(self) -> int:
        """Total structured notes of all four types for this document."""

        return (
            self.document_note_count
            + self.page_note_count
            + self.text_selection_note_count
            + self.image_region_note_count
        )

    @property
    def pdf_file_count(self) -> int:
        """Number of recorded raw-PDF files (always zero or one)."""

        return sum(file.kind == "pdf" for file in self.files)

    @property
    def page_image_count(self) -> int:
        """Number of recorded page-PNG files."""

        return sum(file.kind == "page_image" for file in self.files)

    @property
    def markdown_file_count(self) -> int:
        """Number of recorded page-Markdown files."""

        return sum(file.kind == "markdown" for file in self.files)


@dataclass(frozen=True, slots=True)
class DocumentDeletionResult:
    """Outcome of one completed document deletion.

    ``deleted`` is only ever ``True`` after the database transaction has
    committed and every recorded file has been removed or quarantined.
    A committed deletion whose quarantine cleanup failed still reports
    ``deleted=True`` together with explicit ``cleanup_warnings`` — the
    deletion is real, the residue is visible, and nothing fakes success.
    """

    document_id: int
    document_title: str
    preview: DocumentDeletionPreview
    deleted: bool
    cleanup_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuarantineOperationReport:
    """Outcome of reconciling one unfinished deletion quarantine operation.

    ``status`` is one of ``restored`` (the deletion never committed and the
    files were moved back), ``completed`` (the deletion had committed and
    the quarantine was destroyed), or ``attention`` (fail-closed: nothing
    was deleted or overwritten and a human must inspect the directory).
    ``document_id`` is ``None`` when the manifest could not be read.
    """

    operation_id: str
    quarantine_path: Path
    document_id: int | None
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class QuarantineReconciliation:
    """Aggregate result of one deletion-quarantine reconciliation pass."""

    operations: tuple[QuarantineOperationReport, ...] = ()

    @property
    def has_attention(self) -> bool:
        """Whether any operation needs manual inspection."""

        return any(operation.status == "attention" for operation in self.operations)


class AggregationSourceKind(StrEnum):
    """Which underlying entity one aggregation entry comes from."""

    NOTE = "note"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class AggregationItem:
    """One traceable knowledge entry in a cross-document aggregation view.

    The unified shape exists for browsing and stable sorting only; it never
    pretends notes and evidence are the same thing. ``note_type`` and
    ``importance`` are ``None`` for evidence entries (evidence carries no
    importance and none is fabricated), while ``user_note``/``basket_id``
    are only meaningful for evidence. ``tags``/``projects`` hold the
    effective association names (page-direct plus document-inherited), the
    same semantics the page search layer already uses.
    """

    source_kind: AggregationSourceKind
    source_id: int
    document_id: int
    document_title: str
    page_id: int | None
    page_number: int | None
    note_type: NoteType | None
    importance: NoteImportance | None
    content: str
    user_note: str
    basket_id: int | None
    tags: tuple[str, ...]
    projects: tuple[str, ...]
    sort_timestamp: datetime


@dataclass(frozen=True, slots=True)
class AggregationResult:
    """One page of aggregation items plus totals for the active filters."""

    items: tuple[AggregationItem, ...]
    total_count: int
    note_count: int
    evidence_count: int
    limit: int
    offset: int
    document_count: int = 0
    primary_count: int = 0
    secondary_count: int = 0
    normal_count: int = 0


@dataclass(frozen=True, slots=True)
class PageEmbedding:
    """Persisted embedding artifact for one page under one model configuration.

    The current record for a page is uniquely identified by
    ``(page_id, model, dimensions, config_version)``; ``source_text_sha256``
    is the freshness fingerprint of the text the vector was computed from.
    This is a pure persistence record: it carries no similarity score,
    query, rank, citation, or LLM output.
    """

    id: int
    page_id: int
    source_text_sha256: str
    model: str
    dimensions: int
    config_version: int
    vector: tuple[float, ...]
    created_at: datetime
    updated_at: datetime


class KnowledgeObjectKind(StrEnum):
    """The six knowledge-object types frozen for v0.5.2.

    These are durable business semantics persisted in the database — never
    colors, never icons, never UI presentation details.
    """

    CONCEPT = "concept"
    FACT = "fact"
    PRINCIPLE = "principle"
    EXPERIENCE = "experience"
    PROBLEM = "problem"
    DECISION = "decision"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.CONCEPT: "概念",
            self.FACT: "事实",
            self.PRINCIPLE: "原理",
            self.EXPERIENCE: "经验",
            self.PROBLEM: "问题",
            self.DECISION: "决策",
        }[self]


class KnowledgeObjectStatus(StrEnum):
    """Lifecycle state of one knowledge object (schema v9, v0.5.2)."""

    DRAFT = "draft"
    REVIEWED = "reviewed"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.DRAFT: "草稿",
            self.REVIEWED: "已复核",
            self.ARCHIVED: "已归档",
        }[self]


class KnowledgeObjectSourceType(StrEnum):
    """Which local entity one knowledge-object source link points to."""

    DOCUMENT = "document"
    PAGE = "page"
    NOTE = "note"
    EVIDENCE = "evidence"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in source lists."""
        return {
            self.DOCUMENT: "文档",
            self.PAGE: "页面",
            self.NOTE: "结构化笔记",
            self.EVIDENCE: "证据条目",
        }[self]


class KnowledgeRelationType(StrEnum):
    """Typed, directed relationships between two knowledge objects."""

    RELATES_TO = "relates_to"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXAMPLE_OF = "example_of"
    REQUIRES = "requires"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in relation lists."""
        return {
            self.RELATES_TO: "相关",
            self.DERIVED_FROM: "派生自",
            self.SUPPORTS: "支持",
            self.CONTRADICTS: "相矛盾",
            self.EXAMPLE_OF: "是…的实例",
            self.REQUIRES: "依赖",
        }[self]


class KnowledgeMemoryEntryKind(StrEnum):
    """The four memory entry kinds frozen for v0.5.2.

    ``KNOWLEDGE_CHANGE`` is written automatically by the knowledge-object
    service as an append-only log; the other three are user-authored.
    """

    PROBLEM_SOLVING = "problem_solving"
    EXPERIENCE = "experience"
    DECISION = "decision"
    KNOWLEDGE_CHANGE = "knowledge_change"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in memory lists."""
        return {
            self.PROBLEM_SOLVING: "问题解决",
            self.EXPERIENCE: "经验",
            self.DECISION: "决策",
            self.KNOWLEDGE_CHANGE: "知识变更",
        }[self]


class KnowledgeSourceStatus(StrEnum):
    """Existence status of one knowledge-object source link.

    Only existence is checked for source links: unlike note anchors there is
    no stored snapshot to compare, so a missing target is ``MISSING`` and
    anything resolvable is ``VALID``.
    """

    VALID = "valid"
    MISSING = "missing"

    @property
    def label(self) -> str:
        """Return the Chinese label shown beside source links."""
        return {
            self.VALID: "来源有效",
            self.MISSING: "来源不存在",
        }[self]


@dataclass(frozen=True, slots=True)
class KnowledgeObject:
    """One durable, source-linked knowledge asset (schema v9, v0.5.2)."""

    id: int
    kind: KnowledgeObjectKind
    title: str
    content: str
    importance: NoteImportance
    status: KnowledgeObjectStatus
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeObjectSource:
    """One source-traceability link from a knowledge object to a local entity.

    ``source_type`` and ``source_id`` address one of ``documents``,
    ``pages``, ``notes`` or ``evidence_items``. SQLite cannot declare a
    polymorphic foreign key, so target existence is validated by the service
    layer on write and re-checked on read.
    """

    id: int
    knowledge_object_id: int
    source_type: KnowledgeObjectSourceType
    source_id: int
    source_note: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeObjectSourceView:
    """A source link plus its freshly recomputed existence status."""

    source: KnowledgeObjectSource
    status: KnowledgeSourceStatus


@dataclass(frozen=True, slots=True)
class KnowledgeRelation:
    """One typed, directed relation between two knowledge objects."""

    id: int
    source_ko_id: int
    target_ko_id: int
    relation_type: KnowledgeRelationType
    description: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeMemoryEntry:
    """One durable memory entry (user-authored or auto knowledge change)."""

    id: int
    kind: KnowledgeMemoryEntryKind
    title: str
    content: str
    root_cause: str
    lesson: str
    knowledge_object_id: int | None
    document_id: int | None
    page_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeObjectView:
    """One knowledge object with its source links and relations attached.

    ``outgoing_relations`` are relations where this object is the source;
    ``incoming_relations`` are relations pointing at it. Read-only view built
    by the service; never persisted directly.
    """

    knowledge_object: KnowledgeObject
    sources: tuple[KnowledgeObjectSourceView, ...]
    outgoing_relations: tuple[KnowledgeRelation, ...]
    incoming_relations: tuple[KnowledgeRelation, ...]
