"""Read-only v12/demo structure validation; never proof of public-content consent."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from src.config import PROJECT_ROOT
from src.migrations import SCHEMA_VERSION


class StorageFailure(StrEnum):
    CONFIGURATION = "storage_configuration_invalid"
    ARTIFACT = "demo_artifact_invalid"
    DIGEST = "demo_digest_mismatch"
    SCHEMA = "storage_schema_incompatible"
    IDENTITY = "storage_identity_mismatch"
    INTEGRITY = "storage_integrity_invalid"
    SANITATION = "demo_structure_invalid"
    POLICY = "sqlite_runtime_policy_invalid"
    INITIALIZATION = "storage_initialization_failed"
    WAL = "storage_wal_unavailable"


class HostedStorageError(RuntimeError):
    """Closed safe startup error: never include a path, SQL, corpus or exception."""

    def __init__(self, code: StorageFailure) -> None:
        self.code = code
        super().__init__(f"Hosted 存储不可用（{code.value}）。")


# Column inventory from the existing v1-v12 migrations, including FTS5 shadow
# tables. No DDL or second migration engine. Unknown/missing storage is rejected.
V12_COLUMNS = {
    "ai_calls": (
        "id call_uuid capability model prompt_sha256 input_chars status error_class "
        "retry_count latency_ms prompt_tokens completion_tokens total_tokens "
        "finish_reason source_feature target_refs created_at"
    ).split(),
    "ai_outputs": (
        "id output_uuid call_uuid model context_package_sha256 output_sha256 "
        "output_kind source_feature target_refs recheck_path created_at"
    ).split(),
    "document_tags": ("document_id tag_id created_at").split(),
    "documents": (
        "id title filename source_path sha256 page_count created_at updated_at "
        "import_status processed_page_count text_page_count review_page_count "
        "import_error imported_at"
    ).split(),
    "evidence_baskets": ("id name created_at updated_at").split(),
    "evidence_items": (
        "id basket_id document_id page_id document_title filename page_number "
        "review_status projects_json tags_json evidence_type evidence_text text_kind "
        "context context_kind user_note region_image_sha256 region_image_width "
        "region_image_height region_x0 region_y0 region_x1 region_y1 "
        "source_text_sha256 source_locator selection_sha256 confirmation_status "
        "confirmed_at added_at position"
    ).split(),
    "import_records": (
        "id filename title sha256 status document_id total_pages processed_pages "
        "text_pages review_pages failed_pages error_message started_at finished_at"
    ).split(),
    "knowledge_base_meta": ("id kb_uuid created_at").split(),
    "knowledge_memory_entries": (
        "id kind title content root_cause lesson knowledge_object_id document_id "
        "page_id status created_at updated_at search_title search_content "
        "search_root_cause search_lesson content_revision outcome context_conditions"
    ).split(),
    "knowledge_memory_search": (
        "search_title search_content search_root_cause search_lesson"
    ).split(),
    "knowledge_memory_search_config": ("k v").split(),
    "knowledge_memory_search_data": ("id block").split(),
    "knowledge_memory_search_docsize": ("id sz").split(),
    "knowledge_memory_search_idx": ("segid term pgno").split(),
    "knowledge_object_revisions": (
        "id knowledge_object_id object_local_id_snapshot object_stable_id_snapshot "
        "object_title_snapshot object_kind_snapshot revision_number event_type "
        "before_title after_title before_content after_content before_lifecycle "
        "after_lifecycle before_confirmation after_confirmation superseded_by_before "
        "superseded_by_after source_ref payload_version detail created_at"
    ).split(),
    "knowledge_object_search": ("search_title search_content").split(),
    "knowledge_object_search_config": ("k v").split(),
    "knowledge_object_search_data": ("id block").split(),
    "knowledge_object_search_docsize": ("id sz").split(),
    "knowledge_object_search_idx": ("segid term pgno").split(),
    "knowledge_object_sources": (
        "id knowledge_object_id source_type source_id source_note created_at "
        "source_fingerprint fingerprint_version captured_at"
    ).split(),
    "knowledge_objects": (
        "id kind authorship epistemic_basis title content importance lifecycle "
        "superseded_by_ko_id confirmation_status confirmed_at confirmed_revision "
        "current_revision created_at updated_at search_title search_content"
    ).split(),
    "knowledge_project_links": ("id project_id target_type target_id created_at").split(),
    "knowledge_relations": (
        "id source_ko_id target_ko_id relation_type description created_at"
    ).split(),
    "note_display_preferences": (
        "id color_primary color_secondary color_normal updated_at"
    ).split(),
    "notes": (
        "id note_type document_id page_id personal_note source_kind "
        "source_page_text_sha256 source_excerpt_snapshot selection_start "
        "selection_end user_excerpt region_image_sha256 region_image_width "
        "region_image_height region_x0 region_y0 region_x1 region_y1 created_at "
        "updated_at importance"
    ).split(),
    "page_embeddings": (
        "id page_id source_text_sha256 model dimensions config_version vector created_at updated_at"
    ).split(),
    "page_search": ("search_extracted_text search_ocr_text search_markdown_content").split(),
    "page_search_config": ("k v").split(),
    "page_search_data": ("id block").split(),
    "page_search_docsize": ("id sz").split(),
    "page_search_idx": ("segid term pgno").split(),
    "page_tags": ("page_id tag_id created_at").split(),
    "pages": (
        "id document_id page_number image_path extracted_text ocr_text "
        "markdown_content markdown_path status review_status processing_error "
        "search_extracted_text search_ocr_text search_markdown_content created_at "
        "updated_at note_updated_at reviewed_at last_viewed_at"
    ).split(),
    "project_documents": ("project_id document_id created_at").split(),
    "project_pages": ("project_id page_id created_at").split(),
    "projects": ("id name normalized_name description status created_at updated_at").split(),
    "schema_migrations": ("version applied_at").split(),
    "sqlite_sequence": ("name seq").split(),
    "tags": ("id name normalized_name created_at").split(),
}
PATH_COLUMNS = {
    "documents": ("source_path",),
    "pages": ("image_path", "markdown_path"),
    "ai_outputs": ("recheck_path",),
}
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(str(path) + suffix) for suffix in SIDECAR_SUFFIXES)


def reject_links(path: Path) -> None:
    """Reject symlinks/junctions in every existing component before opening."""
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise HostedStorageError(StorageFailure.ARTIFACT)
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        # Python 3.11 has no Path.is_junction(); Windows lstat still exposes
        # reparse points. Reject those too, including in ancestor directories.
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise HostedStorageError(StorageFailure.ARTIFACT)


def require_regular_file(path: Path) -> None:
    reject_links(path)
    info = path.stat()
    # Also reject hardlink aliases, including aliases of private production DBs.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HostedStorageError(StorageFailure.ARTIFACT)


def validate_artifact_location(path: Path) -> None:
    """Allow packaging areas, but reject known private Local roots before hash/open."""
    resolved = path.resolve()
    private_roots = tuple(PROJECT_ROOT / name for name in ("data", "backups", "logs", "runtime"))
    if any(resolved.is_relative_to(root.resolve()) for root in private_roots):
        raise HostedStorageError(StorageFailure.ARTIFACT)
    require_regular_file(path)
    if path.suffix.lower() != ".db" or any(
        item.exists() or item.is_symlink() for item in sidecars(path)
    ):
        raise HostedStorageError(StorageFailure.ARTIFACT)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def readonly_database(path: Path, *, artifact: bool = False) -> Iterator[sqlite3.Connection]:
    """No new sidecars: immutable checkpointed input, or an existing WAL pair.

    immutable is NEVER used with live WAL. Startup runs before serving requests;
    the formal runtime observer holds its read connection from explicit bootstrap.
    """
    require_regular_file(path)
    wal, shm, journal = sidecars(path)
    present = (wal.exists(), shm.exists())
    if journal.exists() or journal.is_symlink():
        raise HostedStorageError(StorageFailure.WAL)
    if artifact and any(present) or any(present) and not all(present):
        raise HostedStorageError(StorageFailure.WAL)
    for item in (wal, shm):
        if item.exists() or item.is_symlink():
            require_regular_file(item)
    uri = path.as_uri() + ("?mode=ro" if any(present) else "?mode=ro&immutable=1")
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        yield connection
    finally:
        connection.close()


def validate_identity(connection: sqlite3.Connection, expected_uuid: str) -> None:
    if SCHEMA_VERSION != 12:
        raise HostedStorageError(StorageFailure.SCHEMA)
    versions = tuple(
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    )
    if versions != tuple(range(1, 13)) or any(type(value) is not int for value in versions):
        raise HostedStorageError(StorageFailure.SCHEMA)
    rows = connection.execute("SELECT id, kb_uuid FROM knowledge_base_meta").fetchall()
    if rows != [(1, expected_uuid)] or str(UUID(expected_uuid)) != expected_uuid:
        raise HostedStorageError(StorageFailure.IDENTITY)


def validate_schema(connection: sqlite3.Connection) -> None:
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    if names != set(V12_COLUMNS):
        raise HostedStorageError(StorageFailure.SCHEMA)
    for name, columns in V12_COLUMNS.items():
        actual = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
        if actual != columns:
            raise HostedStorageError(StorageFailure.SCHEMA)
    # Only the nine existing FTS synchronization triggers, no extra views/triggers.
    triggers = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")
    }
    expected = {
        prefix + suffix
        for prefix in ("pages_fts_", "knowledge_objects_fts_", "knowledge_memory_fts_")
        for suffix in ("insert", "delete", "update")
    }
    if (
        triggers != expected
        or connection.execute("SELECT 1 FROM sqlite_schema WHERE type='view' LIMIT 1").fetchone()
    ):
        raise HostedStorageError(StorageFailure.SCHEMA)


def _demo_path(value: object) -> bool:
    if value is None or value == "":
        return True
    return (
        isinstance(value, str)
        and re.fullmatch(r"demo://[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", value) is not None
        and all(part not in {".", ".."} for part in value[7:].split("/"))
    )


def validate_structural_sanitization(connection: sqlite3.Connection, *, seed: bool) -> None:
    """Check filesystem-bearing/operational fields, never scan knowledge prose."""
    for table, columns in PATH_COLUMNS.items():
        for column in columns:
            if any(
                not _demo_path(row[0])
                for row in connection.execute(f'SELECT "{column}" FROM "{table}"')
            ):
                raise HostedStorageError(StorageFailure.SANITATION)
    for table in ("documents", "evidence_items"):
        for (value,) in connection.execute(f"SELECT filename FROM {table}"):
            if (
                not isinstance(value, str)
                or any(char in value for char in "/\\:")
                or any(ord(char) < 32 for char in value)
            ):
                raise HostedStorageError(StorageFailure.SANITATION)
    # Actual v12 has no deletion/quarantine/backup/restore table. Their manifests
    # are Local files. import_records and persisted processing errors are Local state.
    if connection.execute("SELECT 1 FROM import_records LIMIT 1").fetchone():
        raise HostedStorageError(StorageFailure.SANITATION)
    for table, column in (("documents", "import_error"), ("pages", "processing_error")):
        if connection.execute(f"SELECT 1 FROM {table} WHERE {column} != '' LIMIT 1").fetchone():
            raise HostedStorageError(StorageFailure.SANITATION)
    if seed:
        for table in ("ai_calls", "ai_outputs"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise HostedStorageError(StorageFailure.SANITATION)


def validate_database_contents(
    connection: sqlite3.Connection, expected_uuid: str, *, seed: bool
) -> None:
    validate_identity(connection, expected_uuid)
    validate_schema(connection)
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise HostedStorageError(StorageFailure.INTEGRITY)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise HostedStorageError(StorageFailure.INTEGRITY)
    validate_structural_sanitization(connection, seed=seed)


def validate_seed_artifact(path: Path, expected_sha256: str, expected_uuid: str) -> None:
    try:
        validate_artifact_location(path)
        if sha256_file(path) != expected_sha256:
            raise HostedStorageError(StorageFailure.DIGEST)
        with readonly_database(path, artifact=True) as connection:
            validate_database_contents(connection, expected_uuid, seed=True)
        validate_artifact_location(path)
        if sha256_file(path) != expected_sha256:
            raise HostedStorageError(StorageFailure.DIGEST)
    except HostedStorageError:
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        raise HostedStorageError(StorageFailure.ARTIFACT) from None


def validate_runtime_database(path: Path, expected_uuid: str) -> None:
    """Runtime audit rows are legal; seed digest is not a runtime identity."""
    try:
        with readonly_database(path) as connection:
            validate_database_contents(connection, expected_uuid, seed=False)
    except HostedStorageError:
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        raise HostedStorageError(StorageFailure.INTEGRITY) from None
