"""Structured notes service (v0.3.0).

Implements creation, reading, updating and deletion for the four frozen note
types (document / page / text selection / image region) on top of the schema
v5 ``notes`` table. Follows the established project discipline: connections
and transactions go through ``Database._connection()``, timestamps use the
shared UTC ISO format, and all user-facing errors are explicit Chinese
messages — never raw SQL and never fake success.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from src.database import Database, DatabaseError
from src.models import (
    ImageSourcePreview,
    Note,
    NoteListItem,
    NoteSourceStatus,
    NoteType,
    NoteView,
    TextSourcePreview,
)
from src.note_geometry import normalize_original_rect

if TYPE_CHECKING:
    from collections.abc import Iterable

LOGGER = logging.getLogger(__name__)

MAX_NOTE_TEXT = 20000
LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100

NOTE_COLUMNS = (
    "id", "note_type", "document_id", "page_id", "personal_note",
    "source_kind", "source_page_text_sha256", "source_excerpt_snapshot",
    "selection_start", "selection_end", "user_excerpt",
    "region_image_sha256", "region_image_width", "region_image_height",
    "region_x0", "region_y0", "region_x1", "region_y1",
    "created_at", "updated_at",
)


class NoteError(DatabaseError):
    """Base class for structured-notes failures."""


class NoteNotFoundError(NoteError):
    """The requested note does not exist."""


class NoteDocumentNotFoundError(NoteError):
    """The target document does not exist."""


class NotePageNotFoundError(NoteError):
    """The target page does not exist."""


class NoteTypeMismatchError(NoteError):
    """The operation does not apply to this note's type."""


class NoteValidationError(NoteError):
    """Caller-supplied content failed validation."""


class TextSourceUnavailableError(NoteError):
    """The page has no usable text layer for selection notes."""


class ExcerptNotFoundError(NoteError):
    """The excerpt does not appear in the page's source text."""


class DuplicateExcerptError(NoteError):
    """The excerpt appears more than once; the position is ambiguous."""


class PageImageMissingError(NoteError):
    """The page PNG file does not exist."""


class PageImageUnreadableError(NoteError):
    """The page PNG file exists but cannot be decoded."""


class InvalidImageRegionError(NoteError):
    """The requested image region is empty or otherwise invalid."""


class NoteWriteError(NoteError):
    """The database write failed or did not take effect."""


class NoteService:
    """Domain service for the four structured note types."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ reads

    def get_note(
        self, note_id: int, *, image_cache: dict[str, _PageImageInfo] | None = None
    ) -> NoteView:
        """Return one note with its freshly recomputed anchor status."""

        with self._database._connection() as connection:
            row = connection.execute(
                f"SELECT {', '.join(NOTE_COLUMNS)} FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
        if row is None:
            raise NoteNotFoundError(f"笔记不存在：{note_id}")
        return self._view(_note_from_row(row), image_cache)

    def list_document_notes(self, document_id: int) -> list[NoteView]:
        """Return notes attached directly to the document (not its pages)."""

        return self._list_where(
            "note_type = ? AND document_id = ?",
            (NoteType.DOCUMENT.value, document_id),
        )

    def list_page_notes(
        self, page_id: int, *, image_cache: dict[str, _PageImageInfo] | None = None
    ) -> list[NoteView]:
        """Return all notes anchored to one page, newest update first.

        ``image_cache`` optionally memoizes PNG measurements for the duration
        of a single render so multiple region notes on one page do not re-read
        and re-hash the same file. The cache is caller-scoped only.
        """

        return self._list_where("page_id = ?", (page_id,), image_cache=image_cache)

    def list_notes(
        self,
        *,
        document_id: int | None = None,
        page_id: int | None = None,
        note_type: NoteType | str | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> list[NoteView]:
        """Paginated note listing for the future standalone notes page.

        A ``document_id`` filter covers both the document's own notes and the
        notes anchored to its pages (via an explicit JOIN on pages, never a
        redundant column). No full-text search, importance or tag filters.
        """

        self._validate_pagination(limit, offset)
        where, parameters = self._list_filters(document_id, page_id, note_type)
        sql = (
            f"SELECT {', '.join(f'notes.{c}' for c in NOTE_COLUMNS)} FROM notes "
            f"LEFT JOIN pages ON notes.page_id = pages.id {where} "
            "ORDER BY notes.updated_at DESC, notes.id DESC LIMIT ? OFFSET ?"
        )
        with self._database._connection() as connection:
            rows = connection.execute(sql, (*parameters, limit, offset)).fetchall()
        return [self._view(_note_from_row(row)) for row in rows]

    def get_text_selection_source_preview(self, page_id: int) -> TextSourcePreview:
        """Read-only preview of the canonical source text for one page.

        Never modifies page or note data; raises TextSourceUnavailableError
        when the page has neither extracted nor OCR text.
        """

        page = self._require_page(page_id)
        source_kind, source_text = self._select_source_text(
            page.extracted_text, page.ocr_text
        )
        return TextSourcePreview(source_kind=source_kind, source_text=source_text)

    def get_image_region_source_preview(
        self, page_id: int, *, image_cache: dict[str, _PageImageInfo] | None = None
    ) -> ImageSourcePreview:
        """Read-only identity facts (path, size, SHA-256) of the page PNG.

        Never modifies page or note data; raises PageImageMissingError or
        PageImageUnreadableError when the PNG cannot be measured.
        """

        page = self._require_page(page_id)
        image = self._read_page_image_cached(page.image_path, image_cache)
        return ImageSourcePreview(
            path=page.image_path,
            width=image.width,
            height=image.height,
            sha256=image.sha256,
        )

    # ------------------------------------------------------------- list page

    def count_notes(
        self,
        *,
        document_id: int | None = None,
        page_id: int | None = None,
        note_type: NoteType | str | None = None,
    ) -> int:
        """Count notes with exactly the same filter semantics as list_notes."""

        where, parameters = self._list_filters(document_id, page_id, note_type)
        sql = (
            "SELECT COUNT(*) FROM notes "
            "LEFT JOIN pages ON notes.page_id = pages.id "
            f"{where}"
        )
        with self._database._connection() as connection:
            return int(connection.execute(sql, parameters).fetchone()[0])

    def list_note_summaries(
        self,
        *,
        document_id: int | None = None,
        page_id: int | None = None,
        note_type: NoteType | str | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> list[NoteListItem]:
        """Paginated note listing with document titles and page numbers.

        One JOIN provides ownership (never a redundant notes column) and the
        page text columns needed to compute text-selection status inline, so
        rendering a page of cards issues no per-note queries. Image-region
        identity is deliberately not checked here (lazy preview only).
        """

        self._validate_pagination(limit, offset)
        where, parameters = self._list_filters(document_id, page_id, note_type)
        sql = (
            f"SELECT {', '.join(f'notes.{c}' for c in NOTE_COLUMNS)}, "
            "documents.title AS document_title, "
            "documents.id AS joined_document_id, "
            "pages.page_number AS page_number, "
            "pages.extracted_text AS page_extracted_text, "
            "pages.ocr_text AS page_ocr_text "
            "FROM notes "
            "LEFT JOIN pages ON notes.page_id = pages.id "
            "LEFT JOIN documents "
            "ON documents.id = COALESCE(notes.document_id, pages.document_id) "
            f"{where} "
            "ORDER BY notes.updated_at DESC, notes.id DESC LIMIT ? OFFSET ?"
        )
        with self._database._connection() as connection:
            rows = connection.execute(sql, (*parameters, limit, offset)).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def list_note_document_options(self) -> list[tuple[int, str]]:
        """Documents that currently own at least one note (id, title)."""

        sql = (
            "SELECT DISTINCT documents.id, documents.title FROM notes "
            "LEFT JOIN pages ON notes.page_id = pages.id "
            "JOIN documents "
            "ON documents.id = COALESCE(notes.document_id, pages.document_id) "
            "ORDER BY documents.title COLLATE NOCASE, documents.id"
        )
        with self._database._connection() as connection:
            return [(int(row[0]), str(row[1])) for row in connection.execute(sql)]

    def _list_filters(
        self,
        document_id: int | None,
        page_id: int | None,
        note_type: NoteType | str | None,
    ) -> tuple[str, tuple[object, ...]]:
        conditions: list[str] = []
        parameters: list[object] = []
        if document_id is not None:
            conditions.append("(notes.document_id = ? OR pages.document_id = ?)")
            parameters.extend((document_id, document_id))
        if page_id is not None:
            conditions.append("notes.page_id = ?")
            parameters.append(page_id)
        if note_type is not None:
            try:
                resolved = NoteType(note_type)
            except ValueError as exc:
                raise NoteValidationError(f"未知笔记类型：{note_type}") from exc
            conditions.append("notes.note_type = ?")
            parameters.append(resolved.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, tuple(parameters)

    def _validate_pagination(self, limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= LIST_LIMIT_MAX
        ):
            raise NoteValidationError(f"limit 必须是 1～{LIST_LIMIT_MAX} 的整数")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise NoteValidationError("offset 必须是非负整数")

    def _summary_from_row(self, row: sqlite3.Row) -> NoteListItem:
        note = _note_from_row(row)
        status: NoteSourceStatus | None = None
        if note.note_type is NoteType.TEXT_SELECTION:
            source = (
                row["page_extracted_text"]
                if note.source_kind == "pdf_text"
                else row["page_ocr_text"]
            )
            if row["page_number"] is None:
                status = NoteSourceStatus.MISSING
            elif not (source or "").strip():
                status = NoteSourceStatus.MISSING
            elif _sha256(source) == note.source_page_text_sha256:
                status = NoteSourceStatus.VALID
            else:
                status = NoteSourceStatus.CHANGED
        title = row["document_title"]
        joined_document_id = row["joined_document_id"]
        return NoteListItem(
            note=note,
            document_id=(
                int(joined_document_id) if joined_document_id is not None else None
            ),
            document_title=str(title) if title is not None else None,
            page_number=(
                int(row["page_number"]) if row["page_number"] is not None else None
            ),
            source_status=status,
        )

    # ---------------------------------------------------------------- creates

    def create_document_note(self, document_id: int, personal_note: str) -> NoteView:
        """Attach a document-level note; multiples per document are allowed."""

        if self._database.get_document(document_id) is None:
            raise NoteDocumentNotFoundError(f"文档不存在：{document_id}")
        note = self._validate_personal_note(personal_note)
        timestamp = _utc_now()
        note_id = self._insert_note(
            (
                NoteType.DOCUMENT.value, document_id, None, note,
                None, None, None, None, None, None,
                None, None, None, None, None, None, None,
                timestamp, timestamp,
            )
        )
        return self.get_note(note_id)

    def create_page_note(self, page_id: int, personal_note: str) -> NoteView:
        """Attach a page-level note; multiples per page are allowed."""

        self._require_page(page_id)
        note = self._validate_personal_note(personal_note)
        timestamp = _utc_now()
        note_id = self._insert_note(
            (
                NoteType.PAGE.value, None, page_id, note,
                None, None, None, None, None, None,
                None, None, None, None, None, None, None,
                timestamp, timestamp,
            )
        )
        return self.get_note(note_id)

    def create_text_selection_note(
        self,
        page_id: int,
        source_excerpt: str,
        personal_note: str,
        user_excerpt: str | None = None,
    ) -> NoteView:
        """Anchor a note to a unique exact-match excerpt of the page source text.

        The source is exactly one column (pdf_text preferred, ocr_text as
        fallback); the excerpt must match exactly once — no normalization, no
        fuzzy matching and no guessing among duplicates.
        """

        page = self._require_page(page_id)
        note = self._validate_personal_note(personal_note)
        excerpt = self._validate_excerpt("原文选区", source_excerpt)
        resolved_excerpt = (
            self._validate_excerpt("用户摘录", user_excerpt)
            if user_excerpt is not None
            else None
        )
        source_kind, source_text = self._select_source_text(page.extracted_text, page.ocr_text)
        start, end = self._locate_unique(source_text, excerpt)
        timestamp = _utc_now()
        note_id = self._insert_note(
            (
                NoteType.TEXT_SELECTION.value, None, page_id, note,
                source_kind, _sha256(source_text), excerpt, start, end,
                resolved_excerpt if resolved_excerpt is not None else excerpt,
                None, None, None, None, None, None, None,
                timestamp, timestamp,
            )
        )
        return self.get_note(note_id)

    def create_image_region_note(
        self,
        page_id: int,
        x0: object,
        y0: object,
        x1: object,
        y1: object,
        personal_note: str,
    ) -> NoteView:
        """Anchor a note to a rectangle of the stored page PNG.

        Width, height and SHA-256 are always read from the real file by the
        service; callers only supply coordinates and the note text.
        """

        page = self._require_page(page_id)
        note = self._validate_personal_note(personal_note)
        image = self._read_page_image(page.image_path)
        rect = self._normalize_region(x0, y0, x1, y1, image)
        timestamp = _utc_now()
        note_id = self._insert_note(
            (
                NoteType.IMAGE_REGION.value, None, page_id, note,
                None, None, None, None, None, None,
                image.sha256, image.width, image.height,
                rect["x0"], rect["y0"], rect["x1"], rect["y1"],
                timestamp, timestamp,
            )
        )
        return self.get_note(note_id)

    # ---------------------------------------------------------------- updates

    def update_document_note(self, note_id: int, personal_note: str) -> NoteView:
        """Update only the text of a document-level note."""

        return self._update_personal(note_id, NoteType.DOCUMENT, personal_note)

    def update_page_note(self, note_id: int, personal_note: str) -> NoteView:
        """Update only the text of a page-level note."""

        return self._update_personal(note_id, NoteType.PAGE, personal_note)

    def update_image_region_note(self, note_id: int, personal_note: str) -> NoteView:
        """Update only the text of an image-region note."""

        return self._update_personal(note_id, NoteType.IMAGE_REGION, personal_note)

    def update_text_selection_content(
        self,
        note_id: int,
        *,
        user_excerpt: str | None = None,
        personal_note: str | None = None,
    ) -> NoteView:
        """Update the user excerpt and/or personal note of a selection note.

        The original anchor (source kind, hash, snapshot and offsets) is never
        touched by this operation.
        """

        self._require_typed_note(note_id, NoteType.TEXT_SELECTION)
        if user_excerpt is None and personal_note is None:
            raise NoteValidationError("没有需要修改的内容")
        assignments: list[str] = []
        parameters: list[object] = []
        if user_excerpt is not None:
            assignments.append("user_excerpt = ?")
            parameters.append(self._validate_excerpt("用户摘录", user_excerpt))
        if personal_note is not None:
            assignments.append("personal_note = ?")
            parameters.append(self._validate_personal_note(personal_note))
        assignments.append("updated_at = ?")
        parameters.append(_utc_now())
        self._apply_update(note_id, assignments, parameters)
        return self.get_note(note_id)

    def rebind_text_selection(self, note_id: int, source_excerpt: str) -> NoteView:
        """Atomically re-anchor a selection note to a new unique excerpt.

        One transaction updates source kind, source hash, snapshot, offsets,
        user excerpt (reset to the new snapshot) and updated_at. The personal
        note, id and created_at are preserved; any failure rolls back fully.
        """

        note = self._require_typed_note(note_id, NoteType.TEXT_SELECTION)
        page = self._require_page(note.page_id)  # type: ignore[arg-type]
        excerpt = self._validate_excerpt("原文选区", source_excerpt)
        source_kind, source_text = self._select_source_text(page.extracted_text, page.ocr_text)
        start, end = self._locate_unique(source_text, excerpt)
        self._apply_update(
            note_id,
            [
                "source_kind = ?", "source_page_text_sha256 = ?",
                "source_excerpt_snapshot = ?", "selection_start = ?",
                "selection_end = ?", "user_excerpt = ?", "updated_at = ?",
            ],
            (
                source_kind, _sha256(source_text), excerpt, start, end,
                excerpt, _utc_now(),
            ),
        )
        return self.get_note(note_id)

    def preview_text_selection_rebind(
        self, note_id: int, source_excerpt: str
    ) -> dict[str, str]:
        """Read-only old/new summary for the UI rebind confirmation dialog."""

        note = self._require_typed_note(note_id, NoteType.TEXT_SELECTION)
        page = self._require_page(note.page_id)  # type: ignore[arg-type]
        excerpt = self._validate_excerpt("原文选区", source_excerpt)
        source_kind, source_text = self._select_source_text(page.extracted_text, page.ocr_text)
        start, end = self._locate_unique(source_text, excerpt)
        return {
            "old_source_kind": note.source_kind or "",
            "old_snapshot": note.source_excerpt_snapshot or "",
            "new_source_kind": source_kind,
            "new_snapshot": excerpt,
            "selection_start": start,
            "selection_end": end,
        }

    def rebind_image_region(
        self,
        note_id: int,
        x0: object,
        y0: object,
        x1: object,
        y1: object,
    ) -> NoteView:
        """Atomically re-frame an image-region note against the current PNG.

        One transaction updates image hash, dimensions, the four coordinates
        and updated_at. The personal note, id and created_at are preserved;
        any failure rolls back fully.
        """

        note = self._require_typed_note(note_id, NoteType.IMAGE_REGION)
        page = self._require_page(note.page_id)  # type: ignore[arg-type]
        image = self._read_page_image(page.image_path)
        rect = self._normalize_region(x0, y0, x1, y1, image)
        self._apply_update(
            note_id,
            [
                "region_image_sha256 = ?", "region_image_width = ?",
                "region_image_height = ?", "region_x0 = ?", "region_y0 = ?",
                "region_x1 = ?", "region_y1 = ?", "updated_at = ?",
            ],
            (
                image.sha256, image.width, image.height,
                rect["x0"], rect["y0"], rect["x1"], rect["y1"], _utc_now(),
            ),
        )
        return self.get_note(note_id)

    # ---------------------------------------------------------------- deletes

    def delete_note(self, note_id: int) -> None:
        """Hard-delete one note row in a single transaction.

        Never touches documents, pages, PDFs, PNGs, text layers, Markdown,
        evidence items or sibling notes. A write that does not affect exactly
        one row is an error, not a silent success.
        """

        self.get_note(note_id)  # raises NoteNotFoundError when absent
        try:
            with self._database._connection() as connection:
                cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        except sqlite3.Error as exc:
            LOGGER.exception("删除笔记失败：%s", note_id)
            raise NoteWriteError("删除笔记失败，请重试") from exc
        if cursor.rowcount != 1:
            raise NoteWriteError("删除笔记未生效，请重试")

    # ------------------------------------------------------------- internals

    def _list_where(
        self,
        clause: str,
        parameters: Iterable[object],
        image_cache: dict[str, _PageImageInfo] | None = None,
    ) -> list[NoteView]:
        sql = (
            f"SELECT {', '.join(NOTE_COLUMNS)} FROM notes WHERE {clause} "
            "ORDER BY updated_at DESC, id DESC"
        )
        with self._database._connection() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [self._view(_note_from_row(row), image_cache) for row in rows]

    def _view(
        self, note: Note, image_cache: dict[str, _PageImageInfo] | None = None
    ) -> NoteView:
        if note.note_type is NoteType.TEXT_SELECTION:
            return NoteView(note=note, source_status=self._text_selection_status(note))
        if note.note_type is NoteType.IMAGE_REGION:
            return NoteView(
                note=note, source_status=self._image_region_status(note, image_cache)
            )
        return NoteView(note=note)

    def _require_page(self, page_id: int):
        page = self._database.get_page(page_id)
        if page is None:
            raise NotePageNotFoundError(f"页面不存在：{page_id}")
        return page

    def _require_typed_note(self, note_id: int, expected: NoteType) -> Note:
        note = self.get_note(note_id).note
        if note.note_type is not expected:
            raise NoteTypeMismatchError(
                f"该操作不适用于{expected.label}：笔记 {note_id} 是{note.note_type.label}"
            )
        return note

    def _update_personal(
        self, note_id: int, expected: NoteType, personal_note: str
    ) -> NoteView:
        self._require_typed_note(note_id, expected)
        note = self._validate_personal_note(personal_note)
        self._apply_update(
            note_id, ["personal_note = ?", "updated_at = ?"], (note, _utc_now())
        )
        return self.get_note(note_id)

    def _insert_note(self, values: tuple) -> int:
        columns = (
            "note_type", "document_id", "page_id", "personal_note",
            "source_kind", "source_page_text_sha256", "source_excerpt_snapshot",
            "selection_start", "selection_end", "user_excerpt",
            "region_image_sha256", "region_image_width", "region_image_height",
            "region_x0", "region_y0", "region_x1", "region_y1",
            "created_at", "updated_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        try:
            with self._database._connection() as connection:
                cursor = connection.execute(
                    f"INSERT INTO notes({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                note_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            LOGGER.exception("保存笔记失败")
            raise NoteWriteError("保存笔记失败，请重试") from exc
        if note_id <= 0:
            raise NoteWriteError("保存笔记未生效，请重试")
        return note_id

    def _apply_update(
        self, note_id: int, assignments: list[str], parameters: Iterable[object]
    ) -> None:
        sql = f"UPDATE notes SET {', '.join(assignments)} WHERE id = ?"
        try:
            with self._database._connection() as connection:
                cursor = connection.execute(sql, (*parameters, note_id))
        except sqlite3.Error as exc:
            LOGGER.exception("更新笔记失败：%s", note_id)
            raise NoteWriteError("更新笔记失败，请重试") from exc
        if cursor.rowcount != 1:
            raise NoteWriteError("更新笔记未生效，请重试")

    def _select_source_text(self, extracted_text: str, ocr_text: str) -> tuple[str, str]:
        if extracted_text.strip():
            return "pdf_text", extracted_text
        if ocr_text.strip():
            return "ocr_text", ocr_text
        raise TextSourceUnavailableError("该页没有可用文本层，请改用图片区域笔记")

    def _locate_unique(self, source_text: str, excerpt: str) -> tuple[int, int]:
        hits = source_text.count(excerpt)
        if hits == 0:
            raise ExcerptNotFoundError("在页面来源文字中找不到该选区，请核对原文")
        if hits > 1:
            raise DuplicateExcerptError("该选区在页面来源文字中出现多次，请补充更多上下文")
        start = source_text.find(excerpt)
        return start, start + len(excerpt)

    def _read_page_image(self, image_path: Path) -> _PageImageInfo:
        if not image_path.is_file():
            raise PageImageMissingError(f"页面图像不存在：{image_path}")
        try:
            data = image_path.read_bytes()
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
        except (OSError, ValueError) as exc:
            raise PageImageUnreadableError(f"页面图像无法读取：{image_path}") from exc
        return _PageImageInfo(
            width=width, height=height, sha256=_sha256_bytes(data)
        )

    def _normalize_region(
        self, x0: object, y0: object, x1: object, y1: object,
        image: _PageImageInfo,
    ) -> dict[str, int]:
        try:
            return normalize_original_rect(x0, y0, x1, y1, image.width, image.height)
        except ValueError as exc:
            raise InvalidImageRegionError(f"图片区域无效：{exc}") from exc

    def _validate_personal_note(self, value: str) -> str:
        if not isinstance(value, str):
            raise NoteValidationError("个人笔记必须是文字")
        normalized = value.strip()
        if not normalized:
            raise NoteValidationError("个人笔记不能为空")
        if len(normalized) > MAX_NOTE_TEXT:
            raise NoteValidationError(f"个人笔记不能超过 {MAX_NOTE_TEXT} 字符")
        return normalized

    def _validate_excerpt(self, label: str, value: str) -> str:
        if not isinstance(value, str):
            raise NoteValidationError(f"{label}必须是文字")
        if not value.strip():
            raise NoteValidationError(f"{label}不能为空")
        if len(value) > MAX_NOTE_TEXT:
            raise NoteValidationError(f"{label}不能超过 {MAX_NOTE_TEXT} 字符")
        return value

    def _text_selection_status(self, note: Note) -> NoteSourceStatus:
        try:
            page = self._database.get_page(note.page_id)  # type: ignore[arg-type]
        except Exception:
            LOGGER.exception("读取文字选区来源失败：note %s", note.id)
            return NoteSourceStatus.UNAVAILABLE
        if page is None:
            return NoteSourceStatus.MISSING
        source = page.extracted_text if note.source_kind == "pdf_text" else page.ocr_text
        if not source.strip():
            return NoteSourceStatus.MISSING
        if _sha256(source) == note.source_page_text_sha256:
            return NoteSourceStatus.VALID
        return NoteSourceStatus.CHANGED

    def _image_region_status(
        self, note: Note, image_cache: dict[str, _PageImageInfo] | None = None
    ) -> NoteSourceStatus:
        try:
            page = self._database.get_page(note.page_id)  # type: ignore[arg-type]
        except Exception:
            LOGGER.exception("读取图片区域来源失败：note %s", note.id)
            return NoteSourceStatus.UNAVAILABLE
        if page is None:
            return NoteSourceStatus.MISSING
        try:
            image = self._read_page_image_cached(page.image_path, image_cache)
        except PageImageMissingError:
            return NoteSourceStatus.MISSING
        except PageImageUnreadableError:
            return NoteSourceStatus.UNREADABLE
        if (
            image.width == note.region_image_width
            and image.height == note.region_image_height
            and image.sha256 == note.region_image_sha256
        ):
            return NoteSourceStatus.VALID
        return NoteSourceStatus.CHANGED

    def _read_page_image_cached(
        self, image_path: Path, cache: dict[str, _PageImageInfo] | None
    ) -> _PageImageInfo:
        if cache is None:
            return self._read_page_image(image_path)
        key = str(image_path)
        if key not in cache:
            cache[key] = self._read_page_image(image_path)
        return cache[key]


class _PageImageInfo:
    """Measured facts about one stored page PNG."""

    __slots__ = ("width", "height", "sha256")

    def __init__(self, width: int, height: int, sha256: str) -> None:
        self.width = width
        self.height = height
        self.sha256 = sha256


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=int(row["id"]),
        note_type=NoteType(row["note_type"]),
        document_id=_optional_int(row["document_id"]),
        page_id=_optional_int(row["page_id"]),
        personal_note=str(row["personal_note"]),
        source_kind=row["source_kind"],
        source_page_text_sha256=row["source_page_text_sha256"],
        source_excerpt_snapshot=row["source_excerpt_snapshot"],
        selection_start=_optional_int(row["selection_start"]),
        selection_end=_optional_int(row["selection_end"]),
        user_excerpt=row["user_excerpt"],
        region_image_sha256=row["region_image_sha256"],
        region_image_width=_optional_int(row["region_image_width"]),
        region_image_height=_optional_int(row["region_image_height"]),
        region_x0=_optional_int(row["region_x0"]),
        region_y0=_optional_int(row["region_y0"]),
        region_x1=_optional_int(row["region_x1"]),
        region_y1=_optional_int(row["region_y1"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None  # type: ignore[arg-type]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


__all__ = [
    "DuplicateExcerptError",
    "ExcerptNotFoundError",
    "InvalidImageRegionError",
    "NoteDocumentNotFoundError",
    "NoteError",
    "NoteNotFoundError",
    "NotePageNotFoundError",
    "NoteService",
    "NoteTypeMismatchError",
    "NoteValidationError",
    "NoteWriteError",
    "PageImageMissingError",
    "PageImageUnreadableError",
    "TextSourceUnavailableError",
]
