"""Explicit Hosted SQLite bootstrap; no HTTP, Local runtime, provider or auto-reset."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock, get_ident
from typing import TYPE_CHECKING

from src.hosted.storage_validation import (
    HostedStorageError,
    StorageFailure,
    reject_links,
    require_regular_file,
    sha256_file,
    sidecars,
    validate_identity,
    validate_runtime_database,
    validate_seed_artifact,
)
from src.hosted_config import HostedSettings, validate_hosted_paths, validate_hosted_startup
from src.runtime_profile import RuntimeProfile, require_runtime_profile

if TYPE_CHECKING:
    from src.database import Database
    from src.hosted.readiness import ReadinessReason

LOGGER = logging.getLogger(__name__)
HOSTED_PROCESS_COUNT = 1
HOSTED_WORKER_COUNT = 1


def validate_hosted_sqlite_runtime_policy(
    *, process_count: int = HOSTED_PROCESS_COUNT, worker_count: int = HOSTED_WORKER_COUNT
) -> None:
    """Validate declared topology, not an unreliable OS process-count heuristic."""
    if (
        type(process_count) is not int
        or process_count != 1
        or type(worker_count) is not int
        or worker_count != 1
    ):
        raise HostedStorageError(StorageFailure.POLICY)


def _validate_layout(settings: HostedSettings) -> None:
    validate_hosted_paths(settings)
    for path in (
        settings.data_root,
        settings.database_dir,
        settings.logs_dir,
        settings.database_path,
        settings.log_path,
    ):
        reject_links(path)
    for path in sidecars(settings.database_path):
        reject_links(path)
        if path.exists():
            require_regular_file(path)
    if sidecars(settings.database_path)[2].exists():
        raise HostedStorageError(StorageFailure.WAL)
    if settings.database_path.exists():
        require_regular_file(settings.database_path)
    elif any(path.exists() for path in sidecars(settings.database_path)):
        raise HostedStorageError(StorageFailure.WAL)


@dataclass(slots=True)
class HostedStorage:
    """Internal DB handle plus a bootstrap-owned read-only readiness connection.

    The observer is opened during startup, keeping initialized WAL/SHM available.
    Reads see committed WAL, never immutable snapshots of an active runtime.
    close() belongs to shutdown, not an HTTP request. No write service is composed.
    """

    settings: HostedSettings = field(repr=False)
    database: Database = field(repr=False)
    kb_uuid: str
    _observer: sqlite3.Connection = field(repr=False)
    _identity: tuple[int, int] = field(repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def database_path(self) -> Path:
        return self.settings.database_path

    def readiness_reason(self, path: Path) -> ReadinessReason | None:
        from src.hosted.readiness import ReadinessReason

        with self._lock:
            try:
                if self._closed or path != self.database_path:
                    raise HostedStorageError(StorageFailure.INITIALIZATION)
                _validate_layout(self.settings)
                validate_hosted_sqlite_runtime_policy()
                info = path.stat()
                if (info.st_dev, info.st_ino) != self._identity:
                    raise HostedStorageError(StorageFailure.IDENTITY)
                if not all(item.is_file() for item in sidecars(path)[:2]):
                    raise HostedStorageError(StorageFailure.WAL)
                validate_identity(self._observer, self.kb_uuid)
                if self._observer.execute("PRAGMA journal_mode").fetchone() != ("wal",):
                    raise HostedStorageError(StorageFailure.WAL)
            except Exception:
                return ReadinessReason.STORAGE_INVALID
        return None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._observer.close()
                self._closed = True


def _copy_artifact(artifact: Path, directory: Path) -> Path:
    """Copy bounded chunks into an exclusively created same-filesystem temp DB."""
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix=".ekb-seed-", suffix=".db", dir=directory, delete=False
        ) as destination:
            temporary = Path(destination.name)
            with artifact.open("rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _publish_seed(temporary: Path, target: Path) -> None:
    # link() atomically fails if the target exists on both NTFS and POSIX.
    # No os.replace, overwrite fallback or cross-volume rename assumption.
    os.link(temporary, target)


class _MigrationDiagnostics(logging.Filter):
    """Suppress legacy raw migration logs only on this bootstrap thread.

    The outer boundary emits a closed failure code instead. Local migration
    semantics/logging and other threads are untouched; no server logger setup.
    """

    def __init__(self) -> None:
        super().__init__()
        self.thread_id = get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread != self.thread_id


def _cleanup_unexpected_backup(directory: Path) -> None:
    """Only a newly created migration backup directory, never pre-existing data."""
    if not directory.exists():
        return
    reject_links(directory)
    files = list(directory.iterdir())
    for path in files:
        if path.parent != directory or path.suffix != ".db":
            raise HostedStorageError(StorageFailure.INITIALIZATION)
        require_regular_file(path)
    for path in files:
        path.unlink()
    directory.rmdir()


def _initialize_runtime(settings: HostedSettings) -> HostedStorage:
    # Deferred import: app/readiness imports never import/construct Database.
    from src.database import Database

    target = settings.database_path
    backup_dir = settings.database_dir / "backups"
    if backup_dir.exists() or backup_dir.is_symlink():
        raise HostedStorageError(StorageFailure.INITIALIZATION)
    migration_logger = logging.getLogger("src.migrations")
    diagnostics = _MigrationDiagnostics()
    migration_logger.addFilter(diagnostics)
    observer: sqlite3.Connection | None = None
    try:
        # Both seed and existing branches passed exact v13 before this constructor.
        database = Database(target)
        if database.last_backup_path is not None or backup_dir.exists():
            raise HostedStorageError(StorageFailure.INITIALIZATION)
        _validate_layout(settings)
        # Explicit bootstrap may initialize sidecars; readiness itself never does.
        observer = sqlite3.connect(
            target.as_uri() + "?mode=ro", uri=True, timeout=1, check_same_thread=False
        )
        if observer.execute("PRAGMA journal_mode").fetchone() != ("wal",):
            raise HostedStorageError(StorageFailure.WAL)
        validate_runtime_database(target, settings.demo_kb_uuid)
        _validate_layout(settings)
        info = target.stat()
        storage = HostedStorage(
            settings, database, settings.demo_kb_uuid, observer, (info.st_dev, info.st_ino)
        )
        if storage.readiness_reason(target) is not None:
            raise HostedStorageError(StorageFailure.INITIALIZATION)
        return storage
    except Exception:
        if observer is not None:
            observer.close()
        _cleanup_unexpected_backup(backup_dir)
        raise
    finally:
        migration_logger.removeFilter(diagnostics)


def bootstrap_hosted_storage(
    settings: HostedSettings,
    *,
    process_count: int = HOSTED_PROCESS_COUNT,
    worker_count: int = HOSTED_WORKER_COUNT,
) -> HostedStorage:
    """Validate -> controlled seed/reuse -> exact v13 -> existing init -> verify WAL.

    Existing targets are never reset/deleted. Only this call's fresh seed is
    removed on failed initialization. The input artifact is never migrated.
    """
    temporary: Path | None = None
    published_identity: tuple[int, int] | None = None
    target = settings.database_path
    try:
        require_runtime_profile(RuntimeProfile.HOSTED)
        validate_hosted_sqlite_runtime_policy(
            process_count=process_count, worker_count=worker_count
        )
        if settings.demo_kb_uuid is None:
            raise HostedStorageError(StorageFailure.CONFIGURATION)
        _validate_layout(settings)
        validate_hosted_startup(settings)
        for directory in (settings.database_dir, settings.logs_dir):
            directory.mkdir(exist_ok=True)
        _validate_layout(settings)
        validate_hosted_startup(settings)
        if target.exists():
            validate_runtime_database(target, settings.demo_kb_uuid)
        else:
            artifact, digest = settings.demo_db_artifact, settings.demo_db_sha256
            if artifact is None or digest is None:
                raise HostedStorageError(StorageFailure.CONFIGURATION)
            validate_seed_artifact(artifact, digest, settings.demo_kb_uuid)
            temporary = _copy_artifact(artifact, settings.database_dir)
            validate_seed_artifact(temporary, digest, settings.demo_kb_uuid)
            validate_seed_artifact(artifact, digest, settings.demo_kb_uuid)
            _validate_layout(settings)
            # Publication is non-overwriting even if another caller won a race.
            info = temporary.stat()
            _publish_seed(temporary, target)
            published_identity = (info.st_dev, info.st_ino)
            temporary.unlink()
            temporary = None
            if sha256_file(target) != digest:
                raise HostedStorageError(StorageFailure.DIGEST)
            validate_runtime_database(target, settings.demo_kb_uuid)
        return _initialize_runtime(settings)
    except Exception as exc:
        # A pre-existing target is never removed, including initialization failure.
        code = exc.code if isinstance(exc, HostedStorageError) else StorageFailure.INITIALIZATION
        try:
            if published_identity is not None and target.exists():
                info = target.stat()
                if (info.st_dev, info.st_ino) == published_identity:
                    # Publication briefly has two names for our inode. Remove the
                    # owned temp first if its initial unlink was interrupted.
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                        temporary = None
                    _validate_layout(settings)
                    for path in (*sidecars(target), target):
                        path.unlink(missing_ok=True)
        except Exception:
            # Cleanup failure must also be closed/sanitized; never mask it as PASS.
            code = StorageFailure.INITIALIZATION
        LOGGER.error("Hosted storage bootstrap failed: code=%s", code.value)
        raise HostedStorageError(code) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise HostedStorageError(StorageFailure.INITIALIZATION) from None
