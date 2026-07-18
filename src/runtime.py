"""Shared application services for Streamlit pages."""

from __future__ import annotations

import logging
import sys
import threading
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.backup_service import BackupService
from src.config import Settings, get_settings
from src.database import Database
from src.diagnostic_service import DiagnosticService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.pdf_service import PdfService


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
    """Return validated settings and create required local directories."""

    settings = get_settings()
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
    )


@lru_cache(maxsize=1)
def application_evidence_basket_service() -> EvidenceBasketService:
    """Return the process-wide durable evidence basket service."""

    return EvidenceBasketService(application_database())


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
