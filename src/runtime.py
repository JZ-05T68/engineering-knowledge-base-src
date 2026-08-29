"""Shared application services for Streamlit pages."""

from __future__ import annotations

import logging
import sys
import threading
from datetime import UTC, datetime
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.ai.coverage_service import PageEmbeddingCoverageService
from src.ai.experience_model_service import ExperienceModelService
from src.ai.hybrid_search import HybridSearchService
from src.ai.page_indexer import EMBEDDING_CONFIG_VERSION, EMBEDDING_DIMENSIONS
from src.ai.provider import (
    AiCallRecord,
    AIUnavailableError,
    AuditedAIProvider,
    EmbeddingProvider,
    build_production_audited_provider,
    require_production_audited_provider,
)
from src.ai.qwen_client import QwenProvider, urllib_transport
from src.ai.vector_recall import (
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
)
from src.ai_ledger_service import AILedgerService
from src.backup_service import BackupService
from src.batch_service import PageBatchService
from src.classification_metadata import ClassificationMetadataService
from src.config import Settings, runtime_settings
from src.database import Database
from src.deletion_recovery import reconcile_quarantine
from src.diagnostic_service import DiagnosticService
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.knowledge_search_service import KnowledgeSearchService
from src.models import QuarantineReconciliation
from src.pdf_service import PdfService
from src.rapidocr_engine import RapidOcrEngine
from src.search_service import SearchService


def configure_logging(log_path: Path) -> None:
    """Configure a bounded UTF-8 rotating local log file once per process."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    resolved_path = log_path.resolve()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved_path:
            return

    file_handler = RotatingFileHandler(
        resolved_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(file_handler)

    def log_uncaught(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: object,
    ) -> None:
        logging.getLogger("uncaught").critical(
            "未捕获异常", exc_info=(exception_type, exception, traceback)
        )

    sys.excepthook = log_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda args: log_uncaught(
            args.exc_type, args.exc_value, args.exc_traceback
        )


@lru_cache(maxsize=1)
def application_settings() -> Settings:
    """Return validated settings and create required local directories.

    Resolves through ``runtime_settings``: a process started as the staging
    instance (``EKB_STAGING_INSTANCE=1``) is fully isolated under the
    staging root; any other process uses the formal guarded settings.
    """

    settings = runtime_settings()
    settings.ensure_directories()
    configure_logging(settings.log_path)
    logging.getLogger(__name__).info(
        "工程知识库启动：version=%s address=%s:%s",
        settings.app_version,
        settings.host,
        settings.port,
    )
    return settings


@lru_cache(maxsize=1)
def application_database() -> Database:
    """Return the process-wide initialized SQLite database."""

    settings = application_settings()
    return Database(settings.database_path)


@lru_cache(maxsize=1)
def application_ai_provider() -> AuditedAIProvider | None:
    """Return the optional audited AI provider, or ``None`` when AI is disabled.

    AI is an optional capability, never a startup dependency: manual mode,
    a missing API key, or an unknown provider all yield ``None``, and no
    existing service receives or requires this provider. When enabled, the
    vendor adapter is wrapped in :class:`AuditedAIProvider`, so every real
    completion/embedding call is recorded in the local ``ai_calls`` ledger
    and evaluated against the configured token budgets before any network
    request is sent. Retry policy comes from ``Settings.ai_max_extra_attempts``
    (bounded to 0..2) instead of a hard-coded value.

    Construction performs no network I/O. The durable ledger and budget guard
    resolve the local database lazily on first use, so building the provider
    never initializes the database or OCR engine.
    """

    settings = application_settings()
    if settings.ai_mode != "api" or settings.ai_provider != "qwen":
        return None
    api_key = settings.ai_api_key.get_secret_value()
    if not api_key:
        return None
    qwen_provider = QwenProvider(
        api_key=api_key,
        llm_model=settings.ai_llm_model,
        llm_model_hard=settings.ai_llm_model_hard,
        embedding_model=settings.ai_embedding_model,
        rerank_model=settings.ai_rerank_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_extra_attempts=settings.ai_max_extra_attempts,
        transport=urllib_transport,
    )
    provider = build_production_audited_provider(
        qwen_provider,
        default_model=settings.ai_llm_model,
        default_embedding_model=settings.ai_embedding_model,
        source_feature="application",
        ledger=_LazyDatabaseAiCallLedger(),
        budget_guard=_LazyTokenBudgetGuard(settings),
    )
    return require_production_audited_provider(provider)


class _LazyDatabaseAiCallLedger:
    """Ledger sink that opens the local database only when a call is recorded."""

    def record(self, call: AiCallRecord) -> None:
        application_database().insert_ai_call(call)


class _LazyTokenBudgetGuard:
    """Token budget gate evaluated against the local ``ai_calls`` ledger.

    Budgets are expressed in tokens and read from settings; ``0`` means
    unlimited. The database is resolved lazily so constructing the AI
    provider never touches it. An exceeded budget raises
    ``AIUnavailableError`` before any network request is sent.
    """

    def __init__(self, settings: Settings) -> None:
        self._daily_budget = settings.ai_daily_token_budget
        self._monthly_budget = settings.ai_monthly_token_budget

    def ensure_allowed(self, capability: str) -> None:
        if self._daily_budget <= 0 and self._monthly_budget <= 0:
            return
        now = datetime.now(UTC)
        if self._daily_budget > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            used = application_database().total_ai_tokens_since(
                day_start.isoformat(timespec="microseconds")
            )
            if used >= self._daily_budget:
                raise AIUnavailableError(
                    f"AI 调用被日预算限制拒绝：今日已用 {used} tokens，"
                    f"上限 {self._daily_budget} tokens。"
                )
        if self._monthly_budget > 0:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            used = application_database().total_ai_tokens_since(
                month_start.isoformat(timespec="microseconds")
            )
            if used >= self._monthly_budget:
                raise AIUnavailableError(
                    f"AI 调用被月预算限制拒绝：本月已用 {used} tokens，"
                    f"上限 {self._monthly_budget} tokens。"
                )


@lru_cache(maxsize=1)
def application_hybrid_search_service() -> HybridSearchService:
    """Return the process-wide hybrid search service (lexical + optional vector).

    The lexical side is the natural-language-normalizing ``SearchService`` —
    never the raw ``Database`` — so free-form queries such as
    ``定时器预分频器`` become real FTS5 OR terms instead of an empty literal
    gate. The vector side is a ``PersistentVectorRecallSource`` over the same
    database, assembled only when an embedding provider is configured; without
    AI the service degrades to lexical-only (``vector=None``), identical to
    today's offline search. Construction is cheap and performs no network I/O:
    no provider is initialized in manual mode, and an API key is never
    required for the application to start.
    """

    settings = application_settings()
    database = application_database()
    lexical = SearchService(database)
    provider = application_ai_provider()
    if provider is not None:
        provider = require_production_audited_provider(provider)
    vector = None
    if isinstance(provider, EmbeddingProvider):
        vector = PersistentVectorRecallSource(
            query_embedding=provider,
            embeddings=database,
            fingerprints=SearchableContentFingerprintSource(database),
            model=settings.ai_embedding_model,
            dimensions=EMBEDDING_DIMENSIONS,
            config_version=EMBEDDING_CONFIG_VERSION,
        )
    return HybridSearchService(lexical=lexical, hydration=database, vector=vector)


@lru_cache(maxsize=1)
def application_coverage_service() -> PageEmbeddingCoverageService:
    """Return the process-wide read-only embedding coverage service.

    Coverage classification is zero-cost and zero-side-effect: it never
    constructs an embedding provider, never touches the network or an API key,
    and never writes to the database. The model defaults to the single
    ``Settings.ai_embedding_model`` source read inside the coverage service.
    """

    return PageEmbeddingCoverageService(database=application_database())


@lru_cache(maxsize=1)
def application_ocr_engine() -> RapidOcrEngine:
    """Return the shared lazy local OCR engine adapter.

    Construction is cheap: no third-party OCR package is imported and no
    model is loaded here. The heavy initialization happens only on the
    first actual ``recognize`` call, and the single cached instance is
    reused by the document service for the whole process.
    """

    return RapidOcrEngine()


@lru_cache(maxsize=1)
def application_document_service() -> DocumentService:
    """Return the document import and Markdown editing service."""

    settings = application_settings()
    pdf_service = PdfService(
        minimum_text_length=settings.minimum_text_length,
        dpi=settings.pdf_render_dpi,
    )
    return DocumentService(
        database=application_database(),
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        pdf_service=pdf_service,
        ocr_engine=application_ocr_engine(),
    )


@lru_cache(maxsize=1)
def application_document_deletion_service() -> DocumentDeletionService:
    """Return the process-wide staged document deletion service."""

    settings = application_settings()
    return DocumentDeletionService(
        database=application_database(),
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        data_dir=settings.data_dir,
        app_version=settings.app_version,
    )


@lru_cache(maxsize=1)
def application_startup_reconciliation() -> QuarantineReconciliation | None:
    """Settle unfinished deletion quarantines once per process, fail closed.

    Interrupted deletions whose files can be provably restored or destroyed
    are settled automatically; anything ambiguous is preserved untouched and
    reported. An unexpected failure of the reconciliation itself is logged
    as critical and never blocks application startup.
    """

    settings = application_settings()
    try:
        report = reconcile_quarantine(
            database=application_database(),
            data_dir=settings.data_dir,
            raw_dir=settings.raw_dir,
            pages_dir=settings.pages_dir,
            markdown_dir=settings.markdown_dir,
        )
    except Exception:
        logging.getLogger(__name__).critical(
            "删除隔离区启动对账失败，已跳过（未改动任何数据）", exc_info=True
        )
        return None
    for operation in report.operations:
        log = logging.getLogger(__name__).info
        if operation.status == "attention":
            log = logging.getLogger(__name__).warning
        log(
            "删除隔离区对账：operation=%s status=%s %s",
            operation.operation_id,
            operation.status,
            operation.detail,
        )
    return report


def run_quarantine_reconciliation() -> QuarantineReconciliation:
    """Run a fresh quarantine reconciliation pass (system maintenance page)."""

    settings = application_settings()
    return reconcile_quarantine(
        database=application_database(),
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
    )


@lru_cache(maxsize=1)
def application_evidence_basket_service() -> EvidenceBasketService:
    """Return the process-wide durable evidence basket service."""

    return EvidenceBasketService(application_database())


@lru_cache(maxsize=1)
def application_knowledge_object_service() -> KnowledgeObjectService:
    """Return the process-wide knowledge-object service (schema v9)."""

    return KnowledgeObjectService(application_database())


@lru_cache(maxsize=1)
def application_knowledge_memory_service() -> KnowledgeMemoryService:
    """Return the process-wide knowledge-memory service (schema v9)."""

    return KnowledgeMemoryService(application_database())


@lru_cache(maxsize=1)
def application_experience_model_service() -> ExperienceModelService:
    """Return the process-wide experience-model service (v0.5.3 Phase 4).

    The service receives the shared AI provider (which may be ``None`` when AI
    is disabled); callers fall back to the deterministic mock provider at the
    UI layer. No network request happens at construction time.
    """

    provider = application_ai_provider()
    if provider is not None:
        provider = require_production_audited_provider(provider)
    return ExperienceModelService(provider)


@lru_cache(maxsize=1)
def application_ai_ledger_service() -> AILedgerService:
    """Return the process-wide read-only AI call ledger service (Phase 5).

    The factory performs no network request and no AI call; it only wires the
    local database.
    """

    return AILedgerService(application_database())


@lru_cache(maxsize=1)
def application_knowledge_search_service() -> KnowledgeSearchService:
    """Return the process-wide offline knowledge search service (Phase 3D).

    Knowledge-scope search is strictly offline: no AI provider, no network and
    no embedding path. The service is resolved lazily by the search page so a
    page-scope search never constructs it.
    """

    return KnowledgeSearchService(application_database())


def application_page_batch_service() -> PageBatchService:
    """Build the stateless batch wrapper around the process-wide database."""

    return PageBatchService(application_database())


def application_classification_metadata_service() -> ClassificationMetadataService:
    """Build a fresh classification reader with no cross-rerun cache state."""

    return ClassificationMetadataService(application_database())


@lru_cache(maxsize=1)
def application_backup_service() -> BackupService:
    """Return the verified local backup service for formal application paths."""

    settings = application_settings()
    return BackupService(
        app_version=settings.app_version,
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        host=settings.host,
        port=settings.port,
        minimum_text_length=settings.minimum_text_length,
        pdf_render_dpi=settings.pdf_render_dpi,
    )


def application_diagnostic_service() -> DiagnosticService:
    """Build a fresh read-only diagnostics service for current formal paths."""

    settings = application_settings()
    return DiagnosticService(
        app_version=settings.app_version,
        project_root=Path(__file__).resolve().parents[1],
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        logs_dir=settings.logs_dir,
        log_path=settings.log_path,
        host=settings.host,
        port=settings.port,
    )
