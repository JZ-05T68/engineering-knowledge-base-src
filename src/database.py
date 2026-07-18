"""SQLite persistence, migration, organization, and FTS5 search."""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import jieba

from src.migrations import SCHEMA_VERSION, MigrationError, migrate_database
from src.models import (
    DashboardStats,
    Document,
    ImportRecord,
    ImportStatus,
    Page,
    PageStatus,
    Project,
    ReviewProgress,
    SearchFacetCounts,
    SearchField,
    SearchFilters,
    SearchResult,
    SearchSort,
    Tag,
)

LOGGER = logging.getLogger(__name__)
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{64}$")
_QUOTED_TERM: Final[re.Pattern[str]] = re.compile(r'"([^"]+)"')
_UNSET: Final[object] = object()
_SEARCH_FIELD_ORDER: Final[tuple[SearchField, ...]] = (
    SearchField.MARKDOWN,
    SearchField.OCR_TEXT,
    SearchField.EXTRACTED_TEXT,
    SearchField.DOCUMENT_TITLE,
    SearchField.FILENAME,
    SearchField.TAG,
    SearchField.PROJECT,
)
_PROCESSING_STATUSES: Final[set[str]] = {
    "text_extracted",
    "ocr_completed",
    "pending_review",
    "manually_reviewed",
    "failed",
}
_LEGACY_PAGE_STATUS_MAP: Final[dict[str, PageStatus]] = {
    "ready": PageStatus.PENDING,
    "text_extracted": PageStatus.PENDING,
    "ocr_completed": PageStatus.PENDING,
    "pending_review": PageStatus.PENDING,
    "manually_reviewed": PageStatus.REVIEWED,
}


class DatabaseError(RuntimeError):
    """Base exception for a local database operation that cannot be completed."""


class DuplicateDocumentError(DatabaseError):
    """Raised when a PDF with the same SHA-256 has already been imported."""


class DuplicateNameError(DatabaseError):
    """Raised when a normalized tag or project name already exists."""


class RecordNotFoundError(DatabaseError):
    """Raised when an expected document, page, tag, or project does not exist."""


class Database:
    """Typed data-access layer around one local SQLite database."""

    SCHEMA_VERSION: Final[int] = SCHEMA_VERSION

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_backup_path: Path | None = None
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            LOGGER.exception("数据库操作失败：%s", self.database_path)
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Back up and migrate the schema without deleting existing local data."""

        try:
            self.last_backup_path = migrate_database(self.database_path)
        except MigrationError:
            raise
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise DatabaseError("当前 SQLite 构建不支持 FTS5 全文检索") from exc
            raise DatabaseError("无法初始化本地数据库") from exc

    # Documents and pages -------------------------------------------------
    def create_document(
        self,
        *,
        title: str,
        filename: str,
        source_path: Path | str,
        sha256: str,
        page_count: int = 0,
        import_status: ImportStatus | str = ImportStatus.PROCESSING,
    ) -> Document:
        """Persist a PDF document, rejecting duplicate SHA-256 hashes."""

        normalized_title = title.strip()
        normalized_filename = filename.strip()
        normalized_hash = sha256.strip().lower()
        if not normalized_title:
            raise ValueError("文档标题不能为空")
        if not normalized_filename:
            raise ValueError("文件名不能为空")
        if not _SHA256_PATTERN.fullmatch(normalized_hash):
            raise ValueError("SHA-256 必须是 64 位十六进制字符")
        if page_count < 0:
            raise ValueError("页数不能为负数")
        status = _coerce_import_status(import_status)
        timestamp = _utc_now()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO documents(
                        title, filename, source_path, sha256, page_count,
                        created_at, updated_at, import_status, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_title,
                        normalized_filename,
                        str(Path(source_path)),
                        normalized_hash,
                        page_count,
                        timestamp,
                        timestamp,
                        status.value,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM documents WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            if "documents.sha256" in str(exc):
                raise DuplicateDocumentError("该文件已经导入（SHA-256 重复）") from exc
            raise DatabaseError("无法保存文档元数据") from exc
        return _document_from_row(row)

    def get_document(self, document_id: int) -> Document | None:
        """Return one document by primary key, or ``None`` when absent."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        return _document_from_row(row) if row is not None else None

    def get_document_by_sha256(self, sha256: str) -> Document | None:
        """Look up an already imported document by its SHA-256 digest."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE sha256 = ?", (sha256.strip().lower(),)
            ).fetchone()
        return _document_from_row(row) if row is not None else None

    def list_documents(
        self,
        *,
        sort_by: str = "imported_desc",
        tag_ids: Sequence[int] = (),
        project_ids: Sequence[int] = (),
        import_status: ImportStatus | str | None = None,
    ) -> list[Document]:
        """List documents with stable local filters and sorting."""

        order_by = {
            "name_asc": "d.title COLLATE NOCASE ASC, d.id ASC",
            "name_desc": "d.title COLLATE NOCASE DESC, d.id DESC",
            "imported_asc": "d.imported_at ASC, d.id ASC",
            "imported_desc": "d.imported_at DESC, d.id DESC",
            "updated_desc": "d.updated_at DESC, d.id DESC",
        }.get(sort_by, "d.imported_at DESC, d.id DESC")
        conditions: list[str] = []
        parameters: list[object] = []
        if import_status is not None:
            conditions.append("d.import_status = ?")
            parameters.append(_coerce_import_status(import_status).value)
        if tag_ids:
            placeholders = ",".join("?" for _ in tag_ids)
            conditions.append(
                f"""d.id IN (
                    SELECT document_id FROM document_tags
                    WHERE tag_id IN ({placeholders})
                    GROUP BY document_id HAVING COUNT(DISTINCT tag_id) = ?
                )"""
            )
            parameters.extend(int(value) for value in tag_ids)
            parameters.append(len(set(tag_ids)))
        if project_ids:
            placeholders = ",".join("?" for _ in project_ids)
            conditions.append(
                f"""d.id IN (
                    SELECT document_id FROM project_documents
                    WHERE project_id IN ({placeholders})
                    GROUP BY document_id HAVING COUNT(DISTINCT project_id) = ?
                )"""
            )
            parameters.extend(int(value) for value in project_ids)
            parameters.append(len(set(project_ids)))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT d.* FROM documents AS d {where} ORDER BY {order_by}", parameters
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    def update_document_import(
        self,
        document_id: int,
        *,
        status: ImportStatus | str,
        page_count: int,
        processed_pages: int,
        text_pages: int,
        review_pages: int,
        error_message: str = "",
    ) -> Document:
        """Atomically update document import status and result statistics."""

        values = (page_count, processed_pages, text_pages, review_pages)
        if any(value < 0 for value in values):
            raise ValueError("导入统计不能为负数")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE documents SET
                    page_count = ?, processed_page_count = ?, text_page_count = ?,
                    review_page_count = ?, import_status = ?, import_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    page_count,
                    processed_pages,
                    text_pages,
                    review_pages,
                    _coerce_import_status(status).value,
                    error_message[:2000],
                    _utc_now(),
                    document_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"文档不存在：{document_id}")
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        return _document_from_row(row)

    def update_document_page_count(self, document_id: int, page_count: int) -> Document:
        """Compatibility helper for v0.0.1 callers."""

        document = self.get_document(document_id)
        if document is None:
            raise RecordNotFoundError(f"文档不存在：{document_id}")
        return self.update_document_import(
            document_id,
            status=ImportStatus.COMPLETED,
            page_count=page_count,
            processed_pages=page_count,
            text_pages=document.text_page_count,
            review_pages=document.review_page_count,
            error_message=document.import_error,
        )

    def delete_document(self, document_id: int) -> None:
        """Delete document metadata and cascading associations in one transaction."""

        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"文档不存在：{document_id}")

    def create_page(
        self,
        *,
        document_id: int,
        page_number: int,
        image_path: Path | str,
        extracted_text: str = "",
        ocr_text: str = "",
        status: PageStatus | str = PageStatus.PENDING,
        processing_status: str | None = None,
        processing_error: str = "",
        markdown_content: str = "",
        markdown_path: Path | str | None = None,
    ) -> Page:
        """Persist one rendered page and all locally available text."""

        if page_number < 1:
            raise ValueError("页码必须从 1 开始")
        normalized_status = _coerce_page_status(status)
        if markdown_content.strip() and normalized_status not in {
            PageStatus.REVIEWED,
            PageStatus.SKIPPED,
        }:
            normalized_status = PageStatus.DRAFT
        normalized_processing_status = _coerce_processing_status(
            processing_status,
            extracted_text=extracted_text,
            ocr_text=ocr_text,
            processing_error=processing_error,
            review_status=normalized_status,
        )
        timestamp = _utc_now()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO pages(
                        document_id, page_number, image_path, extracted_text,
                        ocr_text, markdown_content, markdown_path, status, review_status,
                        processing_error, search_extracted_text, search_ocr_text,
                        search_markdown_content, created_at, updated_at, note_updated_at,
                        reviewed_at, last_viewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        page_number,
                        str(Path(image_path)),
                        extracted_text,
                        ocr_text,
                        markdown_content,
                        _optional_path(markdown_path),
                        normalized_processing_status,
                        normalized_status.value,
                        processing_error[:2000],
                        _tokenize_for_fts(extracted_text),
                        _tokenize_for_fts(ocr_text),
                        _tokenize_for_fts(markdown_content),
                        timestamp,
                        timestamp,
                        timestamp if markdown_content.strip() else None,
                        timestamp if normalized_status is PageStatus.REVIEWED else None,
                        None,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM pages WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"无法保存文档 {document_id} 的第 {page_number} 页") from exc
        return _page_from_row(row)

    def get_page(self, page_id: int) -> Page | None:
        """Return one page by primary key, or ``None`` when absent."""

        with self._connection() as connection:
            row = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return _page_from_row(row) if row is not None else None

    def get_page_by_number(self, document_id: int, page_number: int) -> Page | None:
        """Return a page using its document and one-based page number."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pages WHERE document_id = ? AND page_number = ?",
                (document_id, page_number),
            ).fetchone()
        return _page_from_row(row) if row is not None else None

    def list_pages(self, document_id: int) -> list[Page]:
        """List every page of a document in source order."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE document_id = ? ORDER BY page_number",
                (document_id,),
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def list_review_pages(self, document_id: int | None = None) -> list[Page]:
        """List the default review queue, optionally restricted to a document."""

        parameters: list[object] = [
            PageStatus.PENDING.value,
            PageStatus.DRAFT.value,
            PageStatus.FAILED.value,
        ]
        where = "review_status IN (?, ?, ?)"
        if document_id is not None:
            where += " AND document_id = ?"
            parameters.append(document_id)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM pages WHERE {where} ORDER BY document_id, page_number",
                parameters,
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def list_pending_pages(self, document_id: int | None = None) -> list[Page]:
        """Compatibility alias for pages in the default manual-review queue."""

        return self.list_review_pages(document_id)

    def get_adjacent_review_page(
        self,
        page_id: int,
        direction: str,
        document_id: int | None = None,
    ) -> Page | None:
        """Return the previous or next page in the stable default review queue."""

        if direction not in {"previous", "next"}:
            raise ValueError("待复核导航方向必须是 previous 或 next")
        current = self.get_page(page_id)
        if current is None:
            raise RecordNotFoundError(f"页面不存在：{page_id}")
        statuses = (
            PageStatus.PENDING.value,
            PageStatus.DRAFT.value,
            PageStatus.FAILED.value,
        )
        if document_id is not None:
            if current.document_id != document_id:
                return None
            comparison = "<" if direction == "previous" else ">"
            order = "DESC" if direction == "previous" else "ASC"
            query = f"""
                SELECT * FROM pages
                WHERE review_status IN (?, ?, ?)
                    AND document_id = ? AND page_number {comparison} ?
                ORDER BY page_number {order} LIMIT 1
            """
            parameters: tuple[object, ...] = (
                *statuses,
                document_id,
                current.page_number,
            )
        else:
            comparison = "<" if direction == "previous" else ">"
            order = "DESC" if direction == "previous" else "ASC"
            query = f"""
                SELECT * FROM pages
                WHERE review_status IN (?, ?, ?) AND (
                    document_id {comparison} ? OR
                    (document_id = ? AND page_number {comparison} ?)
                )
                ORDER BY document_id {order}, page_number {order} LIMIT 1
            """
            parameters = (
                *statuses,
                current.document_id,
                current.document_id,
                current.page_number,
            )
        with self._connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        return _page_from_row(row) if row is not None else None

    def list_pages_by_tag(self, tag_id: int) -> list[Page]:
        """List pages directly associated with one tag."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT p.* FROM pages AS p
                JOIN page_tags AS pt ON pt.page_id = p.id
                WHERE pt.tag_id = ? ORDER BY p.document_id, p.page_number
                """,
                (tag_id,),
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def update_page(
        self,
        page_id: int,
        *,
        extracted_text: str | None = None,
        ocr_text: str | None = None,
        markdown_content: str | None = None,
        markdown_path: Path | str | None | object = _UNSET,
        status: PageStatus | str | None = None,
        processing_status: str | None = None,
        processing_error: str | None = None,
        image_path: Path | str | None = None,
    ) -> Page:
        """Update selected page fields while its FTS triggers stay synchronized."""

        assignments: list[str] = []
        values: list[object] = []
        searchable_fields = (
            ("extracted_text", "search_extracted_text", extracted_text),
            ("ocr_text", "search_ocr_text", ocr_text),
            ("markdown_content", "search_markdown_content", markdown_content),
        )
        for field, search_field, value in searchable_fields:
            if value is not None:
                assignments.extend((f"{field} = ?", f"{search_field} = ?"))
                values.extend((value, _tokenize_for_fts(value)))
                if field == "markdown_content":
                    assignments.append("note_updated_at = ?")
                    values.append(_utc_now())
        if markdown_path is not _UNSET:
            assignments.append("markdown_path = ?")
            values.append(_optional_path(markdown_path))
        if status is not None:
            normalized_status = _coerce_page_status(status)
            assignments.extend(("review_status = ?", "reviewed_at = ?"))
            values.extend(
                (
                    normalized_status.value,
                    _utc_now() if normalized_status is PageStatus.REVIEWED else None,
                )
            )
        if processing_status is not None:
            assignments.append("status = ?")
            values.append(_validate_processing_status(processing_status))
        if processing_error is not None:
            assignments.append("processing_error = ?")
            values.append(processing_error[:2000])
        if image_path is not None:
            assignments.append("image_path = ?")
            values.append(str(Path(image_path)))
        if not assignments:
            page = self.get_page(page_id)
            if page is None:
                raise RecordNotFoundError(f"页面不存在：{page_id}")
            return page
        assignments.append("updated_at = ?")
        values.extend((_utc_now(), page_id))
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE pages SET {', '.join(assignments)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"页面不存在：{page_id}")
            row = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return _page_from_row(row)

    def update_page_markdown(
        self,
        page_id: int,
        markdown_content: str,
        markdown_path: Path | str | None,
        *,
        review_status: PageStatus | str = PageStatus.DRAFT,
    ) -> Page:
        """Save page Markdown with an explicit manual-review state."""

        normalized_status = _coerce_page_status(review_status)
        current = self.get_page(page_id)
        if current is None:
            raise RecordNotFoundError(f"页面不存在：{page_id}")
        normalized_path = (
            Path(markdown_path) if markdown_path is not None else None
        )
        if (
            current.markdown_content == markdown_content
            and current.markdown_path == normalized_path
            and current.status is normalized_status
        ):
            return current

        return self.update_page(
            page_id,
            markdown_content=markdown_content,
            markdown_path=markdown_path,
            status=normalized_status,
        )

    def mark_page_viewed(self, page_id: int) -> Page:
        """Record a page visit without changing its content modification time."""

        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE pages SET last_viewed_at = ? WHERE id = ?",
                (_utc_now(), page_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"页面不存在：{page_id}")
            row = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return _page_from_row(row)

    # Import records ------------------------------------------------------
    def create_import_record(self, filename: str, title: str, sha256: str) -> ImportRecord:
        """Create a pending import attempt before document processing starts."""

        timestamp = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO import_records(filename, title, sha256, status, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (filename, title, sha256, ImportStatus.PENDING.value, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM import_records WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _import_record_from_row(row)

    def update_import_record(
        self,
        record_id: int,
        *,
        status: ImportStatus | str,
        document_id: int | None = None,
        total_pages: int = 0,
        processed_pages: int = 0,
        text_pages: int = 0,
        review_pages: int = 0,
        failed_pages: int = 0,
        error_message: str = "",
    ) -> ImportRecord:
        """Update one import attempt and set its finish time for terminal states."""

        normalized_status = _coerce_import_status(status)
        finished_at = (
            _utc_now()
            if normalized_status
            in {ImportStatus.COMPLETED, ImportStatus.FAILED, ImportStatus.PARTIALLY_COMPLETED}
            else None
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE import_records SET
                    status = ?, document_id = COALESCE(?, document_id), total_pages = ?,
                    processed_pages = ?, text_pages = ?, review_pages = ?, failed_pages = ?,
                    error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    normalized_status.value,
                    document_id,
                    total_pages,
                    processed_pages,
                    text_pages,
                    review_pages,
                    failed_pages,
                    error_message[:2000],
                    finished_at,
                    record_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"导入记录不存在：{record_id}")
            row = connection.execute(
                "SELECT * FROM import_records WHERE id = ?", (record_id,)
            ).fetchone()
        return _import_record_from_row(row)

    def list_import_records(self, limit: int = 100) -> list[ImportRecord]:
        """List recent import attempts, newest first."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM import_records ORDER BY started_at DESC, id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [_import_record_from_row(row) for row in rows]

    # Tags ---------------------------------------------------------------
    def create_tag(self, name: str) -> Tag:
        """Create a normalized tag or return the existing same-name tag."""

        display_name, normalized_name = _normalize_name(name, "标签")
        timestamp = _utc_now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM tags WHERE normalized_name = ?", (normalized_name,)
            ).fetchone()
            if existing is not None:
                return _tag_from_row(existing)
            cursor = connection.execute(
                "INSERT INTO tags(name, normalized_name, created_at) VALUES (?, ?, ?)",
                (display_name, normalized_name, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM tags WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _tag_from_row(row)

    def list_tags(self) -> list[Tag]:
        """List tags with dynamically calculated document and page usage counts."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT t.*, (
                    (SELECT COUNT(*) FROM document_tags dt WHERE dt.tag_id = t.id) +
                    (SELECT COUNT(*) FROM page_tags pt WHERE pt.tag_id = t.id)
                ) AS usage_count
                FROM tags AS t ORDER BY t.name COLLATE NOCASE
                """
            ).fetchall()
        return [_tag_from_row(row) for row in rows]

    def delete_tag(self, tag_id: int) -> None:
        """Delete only a tag and its associations, never related materials."""

        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"标签不存在：{tag_id}")

    def get_document_tags(self, document_id: int) -> list[Tag]:
        return self._tags_for("document_tags", "document_id", document_id)

    def get_page_tags(self, page_id: int) -> list[Tag]:
        return self._tags_for("page_tags", "page_id", page_id)

    def _tags_for(self, table: str, key: str, value: int) -> list[Tag]:
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT t.* FROM tags t JOIN {table} x ON x.tag_id = t.id
                WHERE x.{key} = ? ORDER BY t.name COLLATE NOCASE""",
                (value,),
            ).fetchall()
        return [_tag_from_row(row) for row in rows]

    def set_document_tags(self, document_id: int, tag_ids: Sequence[int]) -> None:
        self._set_associations("document_tags", "document_id", document_id, "tag_id", tag_ids)

    def set_page_tags(self, page_id: int, tag_ids: Sequence[int]) -> None:
        self._set_associations("page_tags", "page_id", page_id, "tag_id", tag_ids)

    # Projects -----------------------------------------------------------
    def create_project(
        self, name: str, description: str = "", status: str = "active"
    ) -> Project:
        """Create a project with a meaningful unique normalized name."""

        display_name, normalized_name = _normalize_name(name, "项目")
        timestamp = _utc_now()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO projects(
                        name, normalized_name, description, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        display_name,
                        normalized_name,
                        description.strip(),
                        status.strip() or "active",
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DuplicateNameError("已存在同名项目") from exc
        return _project_from_row(row)

    def update_project(
        self, project_id: int, *, name: str, description: str, status: str
    ) -> Project:
        """Update project metadata without changing any source material."""

        display_name, normalized_name = _normalize_name(name, "项目")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE projects SET name = ?, normalized_name = ?, description = ?,
                        status = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        display_name,
                        normalized_name,
                        description.strip(),
                        status.strip() or "active",
                        _utc_now(),
                        project_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise RecordNotFoundError(f"项目不存在：{project_id}")
                row = connection.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DuplicateNameError("已存在同名项目") from exc
        return _project_from_row(row)

    def list_projects(self) -> list[Project]:
        """List projects with dynamic material counts."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT pr.*,
                    (SELECT COUNT(*) FROM project_documents pd
                        WHERE pd.project_id = pr.id) AS document_count,
                    (SELECT COUNT(*) FROM project_pages pp
                        WHERE pp.project_id = pr.id) AS page_count
                FROM projects pr ORDER BY pr.updated_at DESC, pr.id DESC
                """
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def delete_project(self, project_id: int) -> None:
        """Delete only a project and associations, never documents or pages."""

        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"项目不存在：{project_id}")

    def get_document_projects(self, document_id: int) -> list[Project]:
        return self._projects_for("project_documents", "document_id", document_id)

    def get_page_projects(self, page_id: int) -> list[Project]:
        return self._projects_for("project_pages", "page_id", page_id)

    def _projects_for(self, table: str, key: str, value: int) -> list[Project]:
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT pr.* FROM projects pr JOIN {table} x ON x.project_id = pr.id
                WHERE x.{key} = ? ORDER BY pr.name COLLATE NOCASE""",
                (value,),
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def set_document_projects(self, document_id: int, project_ids: Sequence[int]) -> None:
        self._set_associations(
            "project_documents", "document_id", document_id, "project_id", project_ids
        )

    def set_page_projects(self, page_id: int, project_ids: Sequence[int]) -> None:
        self._set_associations(
            "project_pages", "page_id", page_id, "project_id", project_ids
        )

    def list_project_documents(self, project_id: int) -> list[Document]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT d.* FROM documents d JOIN project_documents pd
                ON pd.document_id = d.id WHERE pd.project_id = ?
                ORDER BY d.title COLLATE NOCASE""",
                (project_id,),
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    def list_project_pages(self, project_id: int) -> list[Page]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT p.* FROM pages p JOIN project_pages pp ON pp.page_id = p.id
                WHERE pp.project_id = ? ORDER BY p.document_id, p.page_number""",
                (project_id,),
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def _set_associations(
        self,
        table: str,
        owner_column: str,
        owner_id: int,
        target_column: str,
        target_ids: Sequence[int],
    ) -> None:
        unique_ids = sorted({int(value) for value in target_ids})
        timestamp = _utc_now()
        with self._connection() as connection:
            connection.execute(f"DELETE FROM {table} WHERE {owner_column} = ?", (owner_id,))
            connection.executemany(
                f"INSERT INTO {table}({owner_column}, {target_column}, created_at) "
                "VALUES (?, ?, ?)",
                [(owner_id, target_id, timestamp) for target_id in unique_ids],
            )

    # Dashboard and search -----------------------------------------------
    def dashboard_stats(self) -> DashboardStats:
        """Return dashboard counts in one local query."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents) AS documents,
                    (SELECT COUNT(*) FROM pages) AS pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE length(trim(markdown_content)) > 0) AS noted_pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status IN ('pending', 'draft', 'failed')) AS review_pages,
                    (SELECT COUNT(*) FROM tags) AS tags,
                    (SELECT COUNT(*) FROM projects) AS projects,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status = 'pending') AS pending_pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status = 'draft') AS draft_pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status = 'reviewed') AS reviewed_pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status = 'skipped') AS skipped_pages,
                    (SELECT COUNT(*) FROM pages
                        WHERE review_status = 'failed') AS failed_pages
                """
            ).fetchone()
        return DashboardStats(**{key: int(row[key]) for key in row.keys()})

    def review_progress(self, document_id: int | None = None) -> ReviewProgress:
        """Return live workflow progress globally or for one document."""

        where = "WHERE document_id = ?" if document_id is not None else ""
        parameters: tuple[object, ...] = (document_id,) if document_id is not None else ()
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN review_status = 'draft' THEN 1 ELSE 0 END) AS draft,
                    SUM(CASE WHEN review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed,
                    SUM(CASE WHEN review_status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN review_status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM pages {where}
                """,
                parameters,
            ).fetchone()
        counts = {key: int(row[key] or 0) for key in row.keys()}
        processed = counts["reviewed"] + counts["skipped"]
        remaining = counts["pending"] + counts["draft"] + counts["failed"]
        return ReviewProgress(processed=processed, remaining=remaining, **counts)

    def recent_edited_pages(self, limit: int = 5) -> list[Page]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM pages WHERE length(trim(markdown_content)) > 0
                ORDER BY COALESCE(note_updated_at, updated_at) DESC LIMIT ?""",
                (max(1, min(limit, 20)),),
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        terms: Sequence[str] | None = None,
        filters: SearchFilters | None = None,
        sort_by: SearchSort | str = SearchSort.RELEVANCE,
    ) -> list[SearchResult]:
        """Search all local fields with parameterized, composable filters.

        FTS5 supplies ranking while literal field checks identify every matching
        source. Multiple tags and projects are intentionally combined with AND.
        """

        normalized_query = query.strip()
        if not normalized_query or limit <= 0:
            return []
        safe_limit = min(limit, 100)
        literal_terms = tuple(
            dict.fromkeys(
                term.casefold().strip()
                for term in (terms or _QUOTED_TERM.findall(normalized_query))
                if term.strip()
            )
        )
        if not literal_terms:
            return []
        active_filters = filters or SearchFilters()
        match_fields = active_filters.match_fields or tuple(SearchField)
        match_clause, match_parameters = _search_match_clause(
            match_fields, literal_terms
        )
        filter_clauses, filter_parameters = _search_filter_clauses(active_filters)
        where_clauses = [f"({match_clause})", *filter_clauses]
        relevance_expression, relevance_parameters = _relevance_expression(literal_terms)
        order_by = _search_order_by(sort_by)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    WITH content_matches AS (
                        SELECT rowid, bm25(page_search, 1.0, 0.9, 1.1) AS search_rank
                        FROM page_search WHERE page_search MATCH ?
                    )
                    SELECT p.*, d.title AS document_title, d.filename,
                        d.source_path AS document_source_path, d.sha256 AS document_sha256,
                        d.updated_at AS document_updated_at, cm.search_rank,
                        {relevance_expression} AS relevance_score
                    FROM pages p
                    JOIN documents d ON d.id = p.document_id
                    LEFT JOIN content_matches cm ON cm.rowid = p.id
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY {order_by}
                    LIMIT ?
                    """,
                    (
                        normalized_query,
                        *relevance_parameters,
                        *match_parameters,
                        *filter_parameters,
                        safe_limit,
                    ),
                ).fetchall()
                results: list[SearchResult] = []
                for row in rows:
                    page_id = int(row["id"])
                    tags = tuple(
                        tag.name for tag in self._tags_for_connection(connection, page_id)
                    )
                    projects = tuple(
                        project.name
                        for project in self._projects_for_connection(connection, page_id)
                    )
                    matched_fields = _matching_fields(
                        row, literal_terms, tags, projects
                    )
                    content = _matched_content(row, matched_fields, tags, projects)
                    rank = row["relevance_score"]
                    results.append(
                        SearchResult(
                            page_id=page_id,
                            document_id=int(row["document_id"]),
                            document_title=str(row["document_title"]),
                            filename=str(row["filename"]),
                            page_number=int(row["page_number"]),
                            image_path=Path(row["image_path"]),
                            content=content,
                            snippet="",
                            rank=float(rank),
                            status=PageStatus(row["review_status"]),
                            match_type="、".join(
                                field.label for field in matched_fields
                            )
                            or "页面内容",
                            tags=tags,
                            projects=projects,
                            match_fields=matched_fields,
                            document_source_path=Path(row["document_source_path"]),
                            document_sha256=str(row["document_sha256"]),
                            extracted_text=str(row["extracted_text"]),
                            ocr_text=str(row["ocr_text"]),
                            markdown_content=str(row["markdown_content"]),
                            updated_at=_parse_datetime(str(row["updated_at"])),
                        )
                    )
                return results
        except sqlite3.OperationalError:
            LOGGER.warning("忽略无效的 FTS5 检索表达式：%r", query, exc_info=True)
            return []

    def search_facet_counts(
        self,
        *,
        terms: Sequence[str] = (),
        filters: SearchFilters | None = None,
    ) -> SearchFacetCounts:
        """Count all facets in one query using the complete current conditions.

        Facets intentionally include their own active condition. Thus selecting
        project A makes every displayed count describe pages that already satisfy
        project A, which keeps counts directly comparable to the visible result set.
        """

        literal_terms = tuple(
            dict.fromkeys(term.casefold().strip() for term in terms if term.strip())
        )
        active_filters = filters or SearchFilters()
        clauses, parameters = _search_filter_clauses(active_filters)
        if literal_terms:
            fields = active_filters.match_fields or tuple(SearchField)
            match_clause, match_parameters = _search_match_clause(fields, literal_terms)
            clauses.insert(0, f"({match_clause})")
            parameters = [*match_parameters, *parameters]
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH filtered(page_id, document_id, review_status) AS (
                    SELECT p.id, p.document_id, p.review_status
                    FROM pages p
                    JOIN documents d ON d.id = p.document_id
                    {where}
                ),
                status_values(value) AS (
                    VALUES ('pending'), ('draft'), ('reviewed'), ('skipped'), ('failed')
                ),
                effective_tags(page_id, tag_id) AS (
                    SELECT f.page_id, dt.tag_id
                    FROM filtered f
                    JOIN document_tags dt ON dt.document_id = f.document_id
                    UNION
                    SELECT f.page_id, pt.tag_id
                    FROM filtered f
                    JOIN page_tags pt ON pt.page_id = f.page_id
                ),
                effective_projects(page_id, project_id) AS (
                    SELECT f.page_id, pd.project_id
                    FROM filtered f
                    JOIN project_documents pd ON pd.document_id = f.document_id
                    UNION
                    SELECT f.page_id, pp.project_id
                    FROM filtered f
                    JOIN project_pages pp ON pp.page_id = f.page_id
                )
                SELECT 'total' AS facet, '' AS facet_key, COUNT(*) AS result_count
                FROM filtered
                UNION ALL
                SELECT 'status', sv.value, COUNT(f.page_id)
                FROM status_values sv
                LEFT JOIN filtered f ON f.review_status = sv.value
                GROUP BY sv.value
                UNION ALL
                SELECT 'tag', CAST(t.id AS TEXT), COUNT(DISTINCT et.page_id)
                FROM tags t
                LEFT JOIN effective_tags et ON et.tag_id = t.id
                GROUP BY t.id
                UNION ALL
                SELECT 'project', CAST(pr.id AS TEXT), COUNT(DISTINCT ep.page_id)
                FROM projects pr
                LEFT JOIN effective_projects ep ON ep.project_id = pr.id
                GROUP BY pr.id
                """,
                parameters,
            ).fetchall()
        total = 0
        statuses = {status: 0 for status in PageStatus}
        tags: dict[int, int] = {}
        projects: dict[int, int] = {}
        for row in rows:
            facet = str(row["facet"])
            count = int(row["result_count"])
            if facet == "total":
                total = count
            elif facet == "status":
                statuses[PageStatus(str(row["facet_key"]))] = count
            elif facet == "tag":
                tags[int(row["facet_key"])] = count
            elif facet == "project":
                projects[int(row["facet_key"])] = count
        return SearchFacetCounts(
            total=total,
            statuses=statuses,
            projects=projects,
            tags=tags,
        )

    @staticmethod
    def _tags_for_connection(connection: sqlite3.Connection, page_id: int) -> list[Tag]:
        rows = connection.execute(
            """
            SELECT DISTINCT t.* FROM tags t
            JOIN pages p ON p.id = ?
            LEFT JOIN document_tags dt ON dt.tag_id = t.id AND dt.document_id = p.document_id
            LEFT JOIN page_tags pt ON pt.tag_id = t.id AND pt.page_id = p.id
            WHERE dt.document_id IS NOT NULL OR pt.page_id IS NOT NULL
            ORDER BY t.name COLLATE NOCASE
            """,
            (page_id,),
        ).fetchall()
        return [_tag_from_row(row) for row in rows]

    @staticmethod
    def _projects_for_connection(
        connection: sqlite3.Connection, page_id: int
    ) -> list[Project]:
        rows = connection.execute(
            """
            SELECT DISTINCT pr.* FROM projects pr
            JOIN pages p ON p.id = ?
            LEFT JOIN project_documents pd
                ON pd.project_id = pr.id AND pd.document_id = p.document_id
            LEFT JOIN project_pages pp ON pp.project_id = pr.id AND pp.page_id = p.id
            WHERE pd.document_id IS NOT NULL OR pp.page_id IS NOT NULL
            ORDER BY pr.name COLLATE NOCASE
            """,
            (page_id,),
        ).fetchall()
        return [_project_from_row(row) for row in rows]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _coerce_page_status(status: PageStatus | str) -> PageStatus:
    try:
        if isinstance(status, PageStatus):
            return status
        if status in _LEGACY_PAGE_STATUS_MAP:
            return _LEGACY_PAGE_STATUS_MAP[status]
        return PageStatus(status)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PageStatus)
        raise ValueError(f"页面状态必须是：{allowed}") from exc


def _validate_processing_status(status: str) -> str:
    normalized = status.strip()
    if normalized not in _PROCESSING_STATUSES:
        allowed = ", ".join(sorted(_PROCESSING_STATUSES))
        raise ValueError(f"页面处理状态必须是：{allowed}")
    return normalized


def _coerce_processing_status(
    status: str | None,
    *,
    extracted_text: str,
    ocr_text: str,
    processing_error: str,
    review_status: PageStatus,
) -> str:
    if status is not None:
        return _validate_processing_status(status)
    if processing_error or review_status is PageStatus.FAILED:
        return "failed"
    if ocr_text.strip():
        return "ocr_completed"
    if extracted_text.strip():
        return "text_extracted"
    return "pending_review"


def _coerce_import_status(status: ImportStatus | str) -> ImportStatus:
    try:
        return status if isinstance(status, ImportStatus) else ImportStatus(status)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ImportStatus)
        raise ValueError(f"导入状态必须是：{allowed}") from exc


def _optional_path(value: Path | str | None | object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Path, str)):
        return str(Path(value))
    raise TypeError("路径必须是 pathlib.Path、字符串或 None")


def _tokenize_for_fts(content: str) -> str:
    """Add spaces between jieba tokens for reliable Chinese FTS5 lookup."""

    if not content.strip():
        return ""
    tokens = (token.strip().lower() for token in jieba.cut_for_search(content) if token.strip())
    return " ".join(token for token in tokens if any(char.isalnum() for char in token))


def _normalize_name(value: str, kind: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display:
        raise ValueError(f"{kind}名称不能为空")
    if len(display) > 100:
        raise ValueError(f"{kind}名称不能超过 100 个字符")
    return display, display.casefold()


def _document_from_row(row: sqlite3.Row) -> Document:
    imported_at = _parse_datetime(row["imported_at"])
    return Document(
        id=int(row["id"]),
        title=str(row["title"]),
        filename=str(row["filename"]),
        source_path=Path(row["source_path"]),
        sha256=str(row["sha256"]),
        page_count=int(row["page_count"]),
        created_at=_parse_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_parse_datetime(row["updated_at"]),  # type: ignore[arg-type]
        import_status=ImportStatus(row["import_status"]),
        processed_page_count=int(row["processed_page_count"]),
        text_page_count=int(row["text_page_count"]),
        review_page_count=int(row["review_page_count"]),
        import_error=str(row["import_error"]),
        imported_at=imported_at,
    )


def _page_from_row(row: sqlite3.Row) -> Page:
    markdown_path = row["markdown_path"]
    return Page(
        id=int(row["id"]),
        document_id=int(row["document_id"]),
        page_number=int(row["page_number"]),
        image_path=Path(row["image_path"]),
        extracted_text=str(row["extracted_text"]),
        ocr_text=str(row["ocr_text"]),
        markdown_content=str(row["markdown_content"]),
        markdown_path=Path(markdown_path) if markdown_path is not None else None,
        status=PageStatus(row["review_status"]),
        processing_error=str(row["processing_error"]),
        created_at=_parse_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_parse_datetime(row["updated_at"]),  # type: ignore[arg-type]
        note_updated_at=_parse_datetime(row["note_updated_at"]),
        reviewed_at=_parse_datetime(row["reviewed_at"]),
        last_viewed_at=_parse_datetime(row["last_viewed_at"]),
        processing_status=str(row["status"]),
    )


def _tag_from_row(row: sqlite3.Row) -> Tag:
    keys = row.keys()
    return Tag(
        id=int(row["id"]),
        name=str(row["name"]),
        created_at=_parse_datetime(row["created_at"]),  # type: ignore[arg-type]
        usage_count=int(row["usage_count"]) if "usage_count" in keys else 0,
    )


def _project_from_row(row: sqlite3.Row) -> Project:
    keys = row.keys()
    return Project(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        status=str(row["status"]),
        created_at=_parse_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_parse_datetime(row["updated_at"]),  # type: ignore[arg-type]
        document_count=int(row["document_count"]) if "document_count" in keys else 0,
        page_count=int(row["page_count"]) if "page_count" in keys else 0,
    )


def _import_record_from_row(row: sqlite3.Row) -> ImportRecord:
    return ImportRecord(
        id=int(row["id"]),
        filename=str(row["filename"]),
        title=str(row["title"]),
        sha256=str(row["sha256"]),
        status=ImportStatus(row["status"]),
        document_id=int(row["document_id"]) if row["document_id"] is not None else None,
        total_pages=int(row["total_pages"]),
        processed_pages=int(row["processed_pages"]),
        text_pages=int(row["text_pages"]),
        review_pages=int(row["review_pages"]),
        failed_pages=int(row["failed_pages"]),
        error_message=str(row["error_message"]),
        started_at=_parse_datetime(row["started_at"]),  # type: ignore[arg-type]
        finished_at=_parse_datetime(row["finished_at"]),
    )


def _search_match_clause(
    fields: Sequence[SearchField], terms: Sequence[str]
) -> tuple[str, list[object]]:
    """Build a literal match expression from whitelisted field fragments."""

    clauses: list[str] = []
    parameters: list[object] = []
    field_expressions = {
        SearchField.EXTRACTED_TEXT: "lower(p.extracted_text) LIKE ? ESCAPE '\\'",
        SearchField.OCR_TEXT: "lower(p.ocr_text) LIKE ? ESCAPE '\\'",
        SearchField.MARKDOWN: "lower(p.markdown_content) LIKE ? ESCAPE '\\'",
        SearchField.DOCUMENT_TITLE: "lower(d.title) LIKE ? ESCAPE '\\'",
        SearchField.FILENAME: "lower(d.filename) LIKE ? ESCAPE '\\'",
    }
    for raw_field in dict.fromkeys(fields):
        field = SearchField(raw_field)
        patterns = [_like_pattern(term) for term in terms]
        if field in field_expressions:
            clauses.append(
                "(" + " OR ".join(field_expressions[field] for _ in patterns) + ")"
            )
            parameters.extend(patterns)
        elif field is SearchField.TAG:
            term_clause = " OR ".join(
                "lower(t.name) LIKE ? ESCAPE '\\'" for _ in patterns
            )
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM tags t
                    LEFT JOIN document_tags dt ON dt.tag_id = t.id
                    LEFT JOIN page_tags pt ON pt.tag_id = t.id
                    WHERE ({term_clause}) AND
                        (dt.document_id = d.id OR pt.page_id = p.id)
                )"""
            )
            parameters.extend(patterns)
        elif field is SearchField.PROJECT:
            term_clause = " OR ".join(
                "lower(pr.name) LIKE ? ESCAPE '\\'" for _ in patterns
            )
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM projects pr
                    LEFT JOIN project_documents pd ON pd.project_id = pr.id
                    LEFT JOIN project_pages pp ON pp.project_id = pr.id
                    WHERE ({term_clause}) AND
                        (pd.document_id = d.id OR pp.page_id = p.id)
                )"""
            )
            parameters.extend(patterns)
    return " OR ".join(clauses) or "0", parameters


def _search_filter_clauses(filters: SearchFilters) -> tuple[list[str], list[object]]:
    """Build filter SQL using placeholders only; tag/project values use AND."""

    clauses: list[str] = []
    parameters: list[object] = []
    document_ids = _positive_ids(filters.document_ids)
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        clauses.append(f"p.document_id IN ({placeholders})")
        parameters.extend(document_ids)
    statuses = tuple(dict.fromkeys(PageStatus(value).value for value in filters.statuses))
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"p.review_status IN ({placeholders})")
        parameters.extend(statuses)
    for tag_id in _positive_ids(filters.tag_ids):
        clauses.append(
            """EXISTS (
                SELECT 1 FROM tags filter_tag
                LEFT JOIN document_tags filter_dt
                    ON filter_dt.tag_id = filter_tag.id
                LEFT JOIN page_tags filter_pt
                    ON filter_pt.tag_id = filter_tag.id
                WHERE filter_tag.id = ? AND
                    (filter_dt.document_id = p.document_id OR filter_pt.page_id = p.id)
            )"""
        )
        parameters.append(tag_id)
    for project_id in _positive_ids(filters.project_ids):
        clauses.append(
            """EXISTS (
                SELECT 1 FROM projects filter_project
                LEFT JOIN project_documents filter_pd
                    ON filter_pd.project_id = filter_project.id
                LEFT JOIN project_pages filter_pp
                    ON filter_pp.project_id = filter_project.id
                WHERE filter_project.id = ? AND
                    (filter_pd.document_id = p.document_id OR filter_pp.page_id = p.id)
            )"""
        )
        parameters.append(project_id)
    return clauses, parameters


def _search_order_by(sort_by: SearchSort | str) -> str:
    try:
        normalized = SearchSort(sort_by)
    except ValueError:
        normalized = SearchSort.RELEVANCE
    return {
        SearchSort.RELEVANCE: (
            "relevance_score ASC, p.document_id, p.page_number"
        ),
        SearchSort.DOCUMENT_PAGE: (
            "d.title COLLATE NOCASE ASC, p.page_number ASC, p.id ASC"
        ),
        SearchSort.VIEWED_DESC: (
            "p.last_viewed_at IS NULL, p.last_viewed_at DESC, "
            "p.document_id, p.page_number"
        ),
        SearchSort.UPDATED_DESC: (
            "p.updated_at DESC, p.document_id, p.page_number"
        ),
    }[normalized]


def _relevance_expression(terms: Sequence[str]) -> tuple[str, list[object]]:
    """Build an explainable metadata/content boost independent of the UI."""

    weighted_fields = (
        (SearchField.DOCUMENT_TITLE, 30.0),
        (SearchField.FILENAME, 18.0),
        (SearchField.TAG, 14.0),
        (SearchField.PROJECT, 12.0),
        (SearchField.MARKDOWN, 7.0),
        (SearchField.OCR_TEXT, 5.0),
        (SearchField.EXTRACTED_TEXT, 4.0),
    )
    parts = ["COALESCE(cm.search_rank, 0.0)"]
    parameters: list[object] = []
    for field, weight in weighted_fields:
        clause, field_parameters = _search_match_clause((field,), terms)
        parts.append(f"CASE WHEN ({clause}) THEN -{weight} ELSE 0.0 END")
        parameters.extend(field_parameters)
    if terms:
        phrase_clause, phrase_parameters = _search_match_clause(
            tuple(SearchField), (terms[0],)
        )
        parts.append(f"CASE WHEN ({phrase_clause}) THEN -10.0 ELSE 0.0 END")
        parameters.extend(phrase_parameters)
    return " + ".join(parts), parameters


def _matching_fields(
    row: sqlite3.Row,
    terms: Sequence[str],
    tags: Sequence[str],
    projects: Sequence[str],
) -> tuple[SearchField, ...]:
    values = {
        SearchField.MARKDOWN: str(row["markdown_content"]),
        SearchField.OCR_TEXT: str(row["ocr_text"]),
        SearchField.EXTRACTED_TEXT: str(row["extracted_text"]),
        SearchField.DOCUMENT_TITLE: str(row["document_title"]),
        SearchField.FILENAME: str(row["filename"]),
        SearchField.TAG: "\n".join(tags),
        SearchField.PROJECT: "\n".join(projects),
    }
    return tuple(
        field
        for field in _SEARCH_FIELD_ORDER
        if any(term in values[field].casefold() for term in terms)
    )


def _matched_content(
    row: sqlite3.Row,
    fields: Sequence[SearchField],
    tags: Sequence[str],
    projects: Sequence[str],
) -> str:
    values = {
        SearchField.MARKDOWN: str(row["markdown_content"]),
        SearchField.OCR_TEXT: str(row["ocr_text"]),
        SearchField.EXTRACTED_TEXT: str(row["extracted_text"]),
        SearchField.DOCUMENT_TITLE: str(row["document_title"]),
        SearchField.FILENAME: str(row["filename"]),
        SearchField.TAG: "、".join(tags),
        SearchField.PROJECT: "、".join(projects),
    }
    for field in fields:
        if values[field].strip():
            return values[field]
    return (
        str(row["markdown_content"]).strip()
        or str(row["ocr_text"]).strip()
        or str(row["extracted_text"]).strip()
        or str(row["document_title"]).strip()
        or str(row["filename"]).strip()
    )


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _positive_ids(values: Sequence[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(value) for value in values if int(value) > 0))


__all__ = [
    "Database",
    "DatabaseError",
    "DuplicateDocumentError",
    "DuplicateNameError",
    "RecordNotFoundError",
]
