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
    SearchResult,
    Tag,
)

LOGGER = logging.getLogger(__name__)
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{64}$")
_QUOTED_TERM: Final[re.Pattern[str]] = re.compile(r'"([^"]+)"')
_UNSET: Final[object] = object()
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

        return self.update_page(
            page_id,
            markdown_content=markdown_content,
            markdown_path=markdown_path,
            status=review_status,
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

    def recent_edited_pages(self, limit: int = 5) -> list[Page]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM pages WHERE length(trim(markdown_content)) > 0
                ORDER BY COALESCE(note_updated_at, updated_at) DESC LIMIT ?""",
                (max(1, min(limit, 20)),),
            ).fetchall()
        return [_page_from_row(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Search page text plus document, tag, and project metadata locally."""

        normalized_query = query.strip()
        if not normalized_query or limit <= 0:
            return []
        safe_limit = min(limit, 100)
        terms = _QUOTED_TERM.findall(normalized_query) or normalized_query.split()
        terms = [term.casefold() for term in terms if term.strip()]
        if not terms:
            return []
        try:
            with self._connection() as connection:
                content_rows = connection.execute(
                    """
                    SELECT p.*, d.title AS document_title, d.filename,
                        bm25(page_search, 1.0, 1.1, 1.2) AS rank
                    FROM page_search
                    JOIN pages p ON p.id = page_search.rowid
                    JOIN documents d ON d.id = p.document_id
                    WHERE page_search MATCH ?
                    ORDER BY rank, p.document_id, p.page_number LIMIT ?
                    """,
                    (normalized_query, safe_limit * 2),
                ).fetchall()
                metadata_rows = self._metadata_search_rows(connection, terms, safe_limit * 2)
                results: list[SearchResult] = []
                seen: set[int] = set()
                for row in [*content_rows, *metadata_rows]:
                    page_id = int(row["id"])
                    if page_id in seen:
                        continue
                    seen.add(page_id)
                    tags = tuple(tag.name for tag in self._tags_for_connection(connection, page_id))
                    projects = tuple(
                        project.name
                        for project in self._projects_for_connection(connection, page_id)
                    )
                    match_type = _match_type(row, terms, tags, projects)
                    content = (
                        str(row["markdown_content"]).strip()
                        or str(row["ocr_text"]).strip()
                        or str(row["extracted_text"]).strip()
                        or " · ".join(
                            value
                            for value in (
                                str(row["document_title"]),
                                str(row["filename"]),
                                *tags,
                                *projects,
                            )
                            if value
                        )
                    )
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
                            rank=float(row["rank"]),
                            status=PageStatus(row["review_status"]),
                            match_type=match_type,
                            tags=tags,
                            projects=projects,
                        )
                    )
                    if len(results) >= safe_limit:
                        break
                return results
        except sqlite3.OperationalError:
            LOGGER.warning("忽略无效的 FTS5 检索表达式：%r", query, exc_info=True)
            return []

    def _metadata_search_rows(
        self, connection: sqlite3.Connection, terms: Sequence[str], limit: int
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        parameters: list[object] = []
        for term in terms:
            pattern = f"%{term}%"
            clauses.append(
                """(
                    lower(d.title) LIKE ? OR lower(d.filename) LIKE ? OR
                    EXISTS (
                        SELECT 1 FROM tags t
                        LEFT JOIN document_tags dt ON dt.tag_id = t.id
                        LEFT JOIN page_tags pt ON pt.tag_id = t.id
                        WHERE lower(t.name) LIKE ? AND
                            (dt.document_id = d.id OR pt.page_id = p.id)
                    ) OR EXISTS (
                        SELECT 1 FROM projects pr
                        LEFT JOIN project_documents pd ON pd.project_id = pr.id
                        LEFT JOIN project_pages pp ON pp.project_id = pr.id
                        WHERE lower(pr.name) LIKE ? AND
                            (pd.document_id = d.id OR pp.page_id = p.id)
                    )
                )"""
            )
            parameters.extend((pattern, pattern, pattern, pattern))
        parameters.append(limit)
        return connection.execute(
            f"""
            SELECT p.*, d.title AS document_title, d.filename, 100.0 AS rank
            FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE {' OR '.join(clauses)}
            ORDER BY d.updated_at DESC, p.page_number LIMIT ?
            """,
            parameters,
        ).fetchall()

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


def _match_type(
    row: sqlite3.Row, terms: Sequence[str], tags: Sequence[str], projects: Sequence[str]
) -> str:
    fields = (
        ("Markdown 笔记", str(row["markdown_content"])),
        ("OCR 文本", str(row["ocr_text"])),
        ("页面提取文本", str(row["extracted_text"])),
        ("文档标题", str(row["document_title"])),
        ("原始文件名", str(row["filename"])),
        ("标签", " ".join(tags)),
        ("项目", " ".join(projects)),
    )
    for label, value in fields:
        folded = value.casefold()
        if any(term in folded for term in terms):
            return label
    return "页面内容"


__all__ = [
    "Database",
    "DatabaseError",
    "DuplicateDocumentError",
    "DuplicateNameError",
    "RecordNotFoundError",
]
