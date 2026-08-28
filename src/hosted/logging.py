"""Process-owned Hosted logging with a closed vocabulary, never raw messages."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from src.hosted.storage_validation import reject_links, require_regular_file


class HostedLogEvent(Enum):
    STARTED = "hosted_started"
    STOPPED = "hosted_stopped"
    INTERRUPTED = "hosted_interrupted"
    STARTUP_FAILED = "hosted_startup_failed"
    SHUTDOWN_FAILED = "hosted_shutdown_failed"
    RUNTIME_WARNING = "hosted_runtime_warning"
    RUNTIME_ERROR = "hosted_runtime_error"


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never call getMessage()/formatException(): args, traceback, logger name,
        # arbitrary extra fields and even custom __str__ can contain private data.
        event = record.msg if isinstance(record.msg, HostedLogEvent) else (
            HostedLogEvent.RUNTIME_ERROR if record.levelno >= logging.ERROR
            else HostedLogEvent.RUNTIME_WARNING
        )
        return event.value


class _SafeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "uvicorn.access" and (
            isinstance(record.msg, HostedLogEvent) or record.levelno >= logging.WARNING
        )


def _safe_handler(handler: logging.Handler) -> logging.Handler:
    handler.setFormatter(_SafeFormatter())
    handler.addFilter(_SafeFilter())
    return handler


@contextmanager
def configure_hosted_logging() -> Iterator[list[logging.Handler]]:
    """Temporarily own all existing logger sinks, including jieba/Uvicorn.

    Explicit server startup only. Restore Local/test logging on exit. File sink
    is attached after WP4 storage bootstrap. No raw exception hook is installed.
    """
    root = logging.getLogger()
    loggers = [root] + [
        item for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    ]
    saved = [(item, item.handlers[:], item.level, item.propagate) for item in loggers]
    previous_errors = logging.raiseExceptions
    handlers = [_safe_handler(logging.StreamHandler(sys.stderr))]
    try:
        logging.raiseExceptions = False
        for item in loggers:
            item.handlers = []
            item.propagate = True
        root.handlers = handlers
        root.setLevel(logging.INFO)
        yield handlers
    finally:
        for handler in handlers:
            handler.close()
        for item, old_handlers, level, propagate in saved:
            item.handlers, item.propagate = old_handlers, propagate
            item.setLevel(level)
        logging.raiseExceptions = previous_errors


def attach_hosted_log_file(path: Path, handlers: list[logging.Handler]) -> None:
    """Use only the validated WP4 log location; reject aliases before opening."""
    reject_links(path)
    if path.exists():
        require_regular_file(path)
    handlers.append(_safe_handler(logging.FileHandler(path, encoding="utf-8")))


def log_event(event: HostedLogEvent) -> None:
    logging.getLogger(__name__).info(event)
