"""Typed domain models shared by persistence, services, and Streamlit pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING


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


class SearchScope(StrEnum):
    """Which corpus one search state targets (Phase 3D).

    ``PAGE`` is the legacy default and is deliberately omitted from URL
    serialization, so every pre-3D URL keeps its exact page-scope meaning.
    ``KNOWLEDGE`` targets the offline personal-knowledge FTS scope.
    """

    PAGE = "page"
    KNOWLEDGE = "knowledge"

    @property
    def label(self) -> str:
        """Return the Chinese label shown by the scope switcher."""

        return {
            self.PAGE: "页面资料",
            self.KNOWLEDGE: "个人知识",
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


class KnowledgeLifecycle(StrEnum):
    """Lifecycle state of one knowledge object (schema v10, Phase 2B)."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.ACTIVE: "现行",
            self.SUPERSEDED: "已替代",
            self.ARCHIVED: "已归档",
        }[self]


class KnowledgeConfirmationStatus(StrEnum):
    """User confirmation state of the object's content (schema v10)."""

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.UNCONFIRMED: "未确认",
            self.CONFIRMED: "已确认",
        }[self]


class KnowledgeAuthorship(StrEnum):
    """Who is accountable for the final content of a knowledge object.

    The single-user local system only writes ``USER`` in v0.5.2. ``AI`` is a
    reserved future value kept in the enum so the database CHECK constraint
    never needs a table rebuild; the service layer rejects AI-authored writes.
    """

    USER = "user"
    AI = "ai"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.USER: "用户",
            self.AI: "AI",
        }[self]


class KnowledgeEpistemicBasis(StrEnum):
    """Mutually exclusive epistemic ground of one knowledge object.

    The presence of source links never derives this value: ``source_derived``
    is a user declaration, and a personal judgment may still carry evidence
    links. ``unknown_legacy`` is reserved for migrated v9 rows whose ground
    cannot be reconstructed honestly.
    """

    SOURCE_DERIVED = "source_derived"
    PERSONAL_EXPERIENCE = "personal_experience"
    PERSONAL_JUDGMENT = "personal_judgment"
    DIRECT_OBSERVATION = "direct_observation"
    DECISION_RECORD = "decision_record"
    PROBLEM_DEFINITION = "problem_definition"
    UNKNOWN_LEGACY = "unknown_legacy"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the local UI."""
        return {
            self.SOURCE_DERIVED: "来源提炼",
            self.PERSONAL_EXPERIENCE: "个人经历",
            self.PERSONAL_JUDGMENT: "个人判断",
            self.DIRECT_OBSERVATION: "直接观察",
            self.DECISION_RECORD: "决策记录",
            self.PROBLEM_DEFINITION: "问题定义",
            self.UNKNOWN_LEGACY: "历史数据（依据未知）",
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
    """The three user-authored personal-memory kinds (schema v10).

    System change records are no longer stored in this table; they live in the
    append-only ``knowledge_object_revisions`` table instead (ADR-05).
    """

    PROBLEM_SOLVING = "problem_solving"
    EXPERIENCE = "experience"
    DECISION = "decision"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in memory lists."""
        return {
            self.PROBLEM_SOLVING: "问题解决",
            self.EXPERIENCE: "经验",
            self.DECISION: "决策",
        }[self]


class KnowledgeMemoryStatus(StrEnum):
    """Lifecycle state of one personal memory entry (schema v10)."""

    ACTIVE = "active"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in memory lists."""
        return {
            self.ACTIVE: "现行",
            self.ARCHIVED: "已归档",
        }[self]


class KnowledgeRevisionEventType(StrEnum):
    """Event types stored in the append-only knowledge revision table."""

    LEGACY_BASELINE = "legacy_baseline"
    LEGACY_EVENT = "legacy_event"
    CREATED = "created"
    CONTENT_UPDATED = "content_updated"
    CONFIRMATION_CHANGED = "confirmation_changed"
    LIFECYCLE_CHANGED = "lifecycle_changed"
    SUPERSESSION_CHANGED = "supersession_changed"
    SOURCE_LINKED = "source_linked"
    SOURCE_UNLINKED = "source_unlinked"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in revision lists."""
        return {
            self.LEGACY_BASELINE: "迁移基线",
            self.LEGACY_EVENT: "历史变更（迁移）",
            self.CREATED: "创建",
            self.CONTENT_UPDATED: "内容更新",
            self.CONFIRMATION_CHANGED: "确认变更",
            self.LIFECYCLE_CHANGED: "生命周期变更",
            self.SUPERSESSION_CHANGED: "替代关系变更",
            self.SOURCE_LINKED: "关联来源",
            self.SOURCE_UNLINKED: "解除来源",
        }[self]


class KnowledgeSourceStatus(StrEnum):
    """Read-time status of one knowledge-object source link (ADR-03).

    ``VALID``/``CHANGED``/``MISSING``/``UNKNOWN`` are computed on every read by
    comparing the freshly recomputed canonical fingerprint with the snapshot
    stored on the link. The read path never writes back.
    """

    VALID = "valid"
    CHANGED = "changed"
    MISSING = "missing"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """Return the Chinese label shown beside source links."""
        return {
            self.VALID: "来源有效",
            self.CHANGED: "来源已变化",
            self.MISSING: "来源不存在",
            self.UNKNOWN: "来源状态未知",
        }[self]


class KnowledgeSourceAggregateState(StrEnum):
    """Object-level aggregate of all source links (ADR-03 truth table)."""

    UNSOURCED = "unsourced"
    VALID = "valid"
    PARTIALLY_VALID = "partially_valid"
    CHANGED = "changed"
    MISSING = "missing"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            self.UNSOURCED: "无来源",
            self.VALID: "来源有效",
            self.PARTIALLY_VALID: "来源部分有效",
            self.CHANGED: "来源已变化",
            self.MISSING: "来源缺失",
            self.UNKNOWN: "来源状态未知",
        }[self]


def aggregate_source_state(
    valid: int, changed: int, missing: int, unknown: int
) -> KnowledgeSourceAggregateState:
    """Apply the six deterministic ADR-03 aggregate rules."""

    if valid + changed + missing + unknown == 0:
        return KnowledgeSourceAggregateState.UNSOURCED
    if valid > 0 and changed + missing + unknown == 0:
        return KnowledgeSourceAggregateState.VALID
    if valid > 0:
        return KnowledgeSourceAggregateState.PARTIALLY_VALID
    if changed > 0:
        return KnowledgeSourceAggregateState.CHANGED
    if missing > 0:
        return KnowledgeSourceAggregateState.MISSING
    return KnowledgeSourceAggregateState.UNKNOWN


@dataclass(frozen=True, slots=True)
class KnowledgeSourceAggregate:
    """Read-time aggregate health of all sources of one knowledge object."""

    state: KnowledgeSourceAggregateState
    valid_count: int
    changed_count: int
    missing_count: int
    unknown_count: int
    evidence_unconfirmed_count: int

    @property
    def evidence_sufficient(self) -> bool:
        """Derived boolean: aggregate VALID and every evidence source confirmed."""

        return (
            self.state is KnowledgeSourceAggregateState.VALID
            and self.evidence_unconfirmed_count == 0
        )


@dataclass(frozen=True, slots=True)
class KnowledgeObject:
    """One durable, source-linked knowledge asset (schema v10, Phase 2B)."""

    id: int
    kind: KnowledgeObjectKind
    authorship: KnowledgeAuthorship
    epistemic_basis: KnowledgeEpistemicBasis
    title: str
    content: str
    importance: NoteImportance
    lifecycle: KnowledgeLifecycle
    superseded_by_ko_id: int | None
    confirmation_status: KnowledgeConfirmationStatus
    confirmed_at: datetime | None
    confirmed_revision: int | None
    current_revision: int
    created_at: datetime
    updated_at: datetime

    @property
    def confirmation_is_current(self) -> bool:
        """Whether the user confirmation still covers the latest revision."""

        return (
            self.confirmation_status is KnowledgeConfirmationStatus.CONFIRMED
            and self.confirmed_revision == self.current_revision
        )

    @property
    def confirmation_is_stale(self) -> bool:
        """Whether content changed after the user last confirmed it."""

        return (
            self.confirmation_status is KnowledgeConfirmationStatus.CONFIRMED
            and self.confirmed_revision is not None
            and self.confirmed_revision < self.current_revision
        )


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
    source_fingerprint: str | None = None
    fingerprint_version: int = 1
    captured_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeObjectSourceView:
    """A source link plus its freshly recomputed fingerprint status."""

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
    """One user-authored personal memory entry (schema v10, v12 extended).

    ``content_revision`` is the lightweight per-entry version counter added in
    v12: it starts at 1 and increments on content edits, giving future agents
    a citable version number without a full revision table. ``outcome`` and
    ``context_conditions`` are the Experience Model ground fields (V53-ADR-03).
    """

    id: int
    kind: KnowledgeMemoryEntryKind
    title: str
    content: str
    root_cause: str
    lesson: str
    knowledge_object_id: int | None
    document_id: int | None
    page_id: int | None
    status: KnowledgeMemoryStatus
    created_at: datetime
    updated_at: datetime
    content_revision: int = 1
    outcome: str = ""
    context_conditions: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeProjectLink:
    """One project-to-knowledge association row (schema v12)."""

    id: int
    project_id: int
    target_type: str
    target_id: int
    created_at: datetime


class KnowledgeSearchResultType(StrEnum):
    """Which knowledge entity produced one offline knowledge-search result."""

    KNOWLEDGE_OBJECT = "knowledge_object"
    KNOWLEDGE_MEMORY = "knowledge_memory"

    @property
    def label(self) -> str:
        """Return the Chinese label shown in the search UI."""
        return {
            self.KNOWLEDGE_OBJECT: "知识对象",
            self.KNOWLEDGE_MEMORY: "知识记忆",
        }[self]


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """One citation-ready result from the offline knowledge search (Phase 3C).

    Deliberately independent from the page-scope ``SearchResult``: knowledge
    recall uses FTS5 MATCH while page recall uses the LIKE gate, and the two
    scopes carry different provenance anchors. The fields here are the stable
    anchor surface for Phase 4 export and Phase 5 Prompt Builder / agent
    citation; no page-search semantics are reused or modified.
    """

    result_type: KnowledgeSearchResultType
    id: int
    stable_id: str
    title: str
    content: str
    snippet: str = ""
    status: str = ""
    status_label: str = ""
    kind: str = ""
    kind_label: str = ""
    updated_at: datetime | None = None
    knowledge_object_id: int | None = None
    document_id: int | None = None
    page_id: int | None = None
    source_anchors: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeRevision:
    """One append-only knowledge object revision event (schema v10)."""

    id: int
    knowledge_object_id: int | None
    object_local_id_snapshot: int | None
    object_stable_id_snapshot: str | None
    object_title_snapshot: str
    object_kind_snapshot: str
    revision_number: int
    event_type: KnowledgeRevisionEventType
    before_title: str | None
    after_title: str | None
    before_content: str | None
    after_content: str | None
    before_lifecycle: str | None
    after_lifecycle: str | None
    before_confirmation: str | None
    after_confirmation: str | None
    superseded_by_before: int | None
    superseded_by_after: int | None
    source_ref: str | None
    payload_version: int
    detail: str
    created_at: datetime


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


KNOWLEDGE_OBJECT_STABLE_TYPE = "knowledge_object"
KNOWLEDGE_MEMORY_STABLE_TYPE = "knowledge_memory"
KNOWLEDGE_RELATION_STABLE_TYPE = "knowledge_relation"
KNOWLEDGE_SOURCE_STABLE_TYPE = "knowledge_source"
KNOWLEDGE_REVISION_STABLE_TYPE = "knowledge_revision"
PAGE_STABLE_TYPE = "page"
EVIDENCE_STABLE_TYPE = "evidence"

_KNOWN_STABLE_TYPES = frozenset(
    {
        KNOWLEDGE_OBJECT_STABLE_TYPE,
        KNOWLEDGE_MEMORY_STABLE_TYPE,
        KNOWLEDGE_RELATION_STABLE_TYPE,
        KNOWLEDGE_SOURCE_STABLE_TYPE,
        KNOWLEDGE_REVISION_STABLE_TYPE,
        PAGE_STABLE_TYPE,
        EVIDENCE_STABLE_TYPE,
    }
)


def build_stable_id(kb_uuid: str, object_type: str, local_id: int) -> str:
    """Return the canonical stable ID ``<kb_uuid>:<object_type>:<local_id>``.

    Pure function shared by the database layer, services, UI and the future
    export pipeline. ``object_type`` must be one of the ``*_STABLE_TYPE``
    constants above; ``local_id`` is the integer primary key inside this
    knowledge base and must be a positive integer. The function reads no
    network or machine identity: every input is supplied by the caller.
    """

    if object_type not in _KNOWN_STABLE_TYPES:
        raise ValueError(f"非法稳定 ID 对象类型：{object_type}")
    if isinstance(local_id, bool):
        raise ValueError("local_id 必须是正整数，不能是布尔值")
    try:
        local_id_int = int(local_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("local_id 必须是正整数") from exc
    if local_id_int < 1:
        raise ValueError("local_id 必须是正整数")
    return f"{kb_uuid}:{object_type}:{local_id_int}"


# --------------------------------------------------------------------------
# Context Engineering contracts (v0.5.3 Phase 2-B, V53-ADR-01/02)
# --------------------------------------------------------------------------


class ContextItemType(StrEnum):
    """The four entity kinds that may be projected into a ContextItem."""

    PAGE = "page"
    KNOWLEDGE_OBJECT = "knowledge_object"
    KNOWLEDGE_MEMORY = "knowledge_memory"
    EVIDENCE = "evidence"

    @property
    def label(self) -> str:
        return {
            self.PAGE: "页面",
            self.KNOWLEDGE_OBJECT: "知识对象",
            self.KNOWLEDGE_MEMORY: "知识记忆",
            self.EVIDENCE: "证据",
        }[self]


class ContextAnchorType(StrEnum):
    """Anchor categories carried by a ContextItem source anchor."""

    DOCUMENT = "document"
    PAGE = "page"
    NOTE = "note"
    EVIDENCE = "evidence"
    SELECTION = "selection"
    IMAGE_REGION = "image_region"

    @property
    def label(self) -> str:
        return {
            self.DOCUMENT: "文档",
            self.PAGE: "页面",
            self.NOTE: "结构化笔记",
            self.EVIDENCE: "证据条目",
            self.SELECTION: "文字选区",
            self.IMAGE_REGION: "图片区域",
        }[self]


class ContextFingerprintState(StrEnum):
    """Read-time fingerprint state of one source anchor."""

    VALID = "valid"
    CHANGED = "changed"
    MISSING = "missing"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

    @property
    def label(self) -> str:
        return {
            self.VALID: "来源有效",
            self.CHANGED: "来源已变化",
            self.MISSING: "来源不存在",
            self.UNKNOWN: "来源状态未知",
            self.NOT_APPLICABLE: "不适用",
        }[self]


@dataclass(frozen=True, slots=True)
class ContextSourceAnchor:
    """One traceable, back-to-source anchor of a ContextItem."""

    anchor_type: str
    anchor_id: int | None
    anchor_label: str
    fingerprint_state: str = ContextFingerprintState.NOT_APPLICABLE.value


@dataclass(frozen=True, slots=True)
class ContextRelationRef:
    """One already-stored direct relation, never inferred or created."""

    relation_type: str
    relation_label: str
    direction: str
    target_stable_id: str


@dataclass(frozen=True, slots=True)
class ContextItem:
    """Read-only projection of one local knowledge entity for RAG context.

    ContextItem is not a new knowledge entity and is never persisted. It is
    the single consumption shape understood by the KnowledgeContextPackager
    and, later, by the v0.6.x agent input boundary.
    """

    type: ContextItemType
    local_id: int
    stable_id: str
    title: str
    content: str
    kind: str | None
    kind_label: str | None
    status: str
    status_label: str
    importance: str | None
    updated_at: datetime | None
    revision_ref: str | None
    source_anchors: tuple[ContextSourceAnchor, ...]
    relation_refs: tuple[ContextRelationRef, ...]


# --------------------------------------------------------------------------
# Audited AI output (v0.5.3 Phase 3)
# --------------------------------------------------------------------------

if TYPE_CHECKING:
    from src.ai.provider import CompletionUsage


@dataclass(frozen=True, slots=True)
class AuditedAIOutput:
    """One traceable, citation-grounded AI answer over a context package.

    This is an in-memory service result, not a persisted knowledge entity.
    ``context_stable_ids`` lists the knowledge actually used, ``excluded``
    records what the packager excluded and why, and ``warnings`` carries the
    package's explicit risk notices. ``confidence`` is deliberately not
    fabricated: it stays ``None`` unless a future provider reports one.
    """

    output_id: str
    query: str
    context_package_id: str
    provider: str
    model: str
    generated_at: str
    answer: str
    citations: tuple[tuple[str, str], ...]
    answer_citations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    token_usage: CompletionUsage | None = None
    context_stable_ids: tuple[str, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()
    confidence: str | None = None


@dataclass(frozen=True, slots=True)
class ExperienceCandidate:
    """Structured AI-organised experience candidate (read-only, not persisted).

    Every field that cannot be confirmed from the selected context stays
    empty; the model is explicitly forbidden from fabricating content.
    """

    title: str
    problem: str = ""
    context: str = ""
    action: str = ""
    result: str = ""
    root_cause: str = ""
    lesson: str = ""
    applicability: str = ""
    limitations: str = ""
    citations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditedExperienceOutput:
    """One audited, structured experience-candidate generation result.

    This is an in-memory service result. It is never persisted and never
    becomes a KnowledgeMemoryEntry / KnowledgeObject without an explicit,
    separate user action. ``audit_call_id`` is ``None`` here because the
    durable call uuid lives in the ``ai_calls`` ledger and is retrievable via
    ``source_feature='experience_model'`` plus ``target_refs``.
    """

    output_id: str
    task: str
    context_package_id: str
    provider: str
    model: str
    audit_call_id: str | None
    generated_at: str
    candidate: ExperienceCandidate
    warnings: tuple[str, ...]
    is_mock: bool


# --------------------------------------------------------------------------
# Read-only AI call ledger projections (v0.5.3 Phase 5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AICallLedgerEntry:
    """One read-only AI call ledger row.

    ``provider`` stays ``None`` because schema v12 does not store a provider
    column; the runtime wires exactly one vendor (Qwen) and the UI renders
    that documented constant instead of guessing. ``finished_at`` is absent
    from v12 and stays ``None``. ``is_real_call`` is always ``True`` for a
    persisted row: Mock/offline demonstrations never write the ledger.
    """

    call_id: int
    call_uuid: str
    capability: str
    source_feature: str
    provider: str | None
    model: str
    status: str
    created_at: str
    finished_at: str | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    target_refs: tuple[str, ...]
    target_refs_parse_error: bool
    unavailable_target_refs: tuple[str, ...] = ()
    error_class: str | None = None
    error_summary: str = ""
    is_real_call: bool = True
    retry_count: int = 0
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AICallLedgerQuery:
    """Whitelisted filter/sort/pagination input for the ledger query."""

    source_feature: str | None = None
    capability: str | None = None
    status: str | None = None
    provider: str | None = None
    model: str | None = None
    since_iso: str | None = None
    until_iso: str | None = None
    sort: str = "created_at_desc"
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True, slots=True)
class AICallLedgerPage:
    """One stable page of ledger entries."""

    entries: tuple[AICallLedgerEntry, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class AICallLedgerStats:
    """Limited aggregates; token sums count only non-null reliable values."""

    total_calls: int
    success_count: int
    error_count: int
    rejected_count: int
    total_tokens: int | None
    by_source_feature: tuple[tuple[str, int], ...]
