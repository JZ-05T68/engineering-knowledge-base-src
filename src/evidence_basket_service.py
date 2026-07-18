"""Durable local evidence basket operations and source-integrity checks."""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from src.models import (
    Document,
    EvidenceBasket,
    EvidenceItem,
    EvidenceTextKind,
    Page,
    PageStatus,
)
from src.text_utils import build_context_excerpt

DEFAULT_BASKET_NAME = "默认证据篮"
MAX_EVIDENCE_CHARS = 20_000
MAX_CONTEXT_CHARS = 4_000
MAX_NOTE_CHARS = 4_000


class EvidenceDatabase(Protocol):
    """Database lookups required to capture and validate evidence sources."""

    database_path: Path

    def get_document(self, document_id: int) -> Document | None: ...

    def get_page(self, page_id: int) -> Page | None: ...

    def get_page_tags(self, page_id: int) -> list[object]: ...

    def get_page_projects(self, page_id: int) -> list[object]: ...


class EvidenceBasketError(RuntimeError):
    """Base error for a safe evidence-basket operation."""


class DuplicateEvidenceError(EvidenceBasketError):
    """Raised when the same normalized selection already exists in a basket."""


class EvidenceSourceError(EvidenceBasketError):
    """Raised when a stored or requested source can no longer be trusted."""


class EmptyEvidenceBasketError(EvidenceBasketError):
    """Raised when an operation requires at least one evidence item."""


class EvidenceBasketService:
    """Manage evidence selections that persist across Streamlit and service restarts."""

    def __init__(self, database: EvidenceDatabase) -> None:
        self._database = database
        self._repository = _EvidenceRepository(database.database_path)

    def create_basket(self, name: str) -> EvidenceBasket:
        """Create a named basket for future multi-basket expansion."""

        return self._repository.create_basket(_clean_basket_name(name))

    def default_basket(self) -> EvidenceBasket:
        """Return the durable default basket, creating it once when necessary."""

        return self._repository.get_or_create_basket(DEFAULT_BASKET_NAME)

    def list_baskets(self) -> list[EvidenceBasket]:
        """Return every local evidence basket in creation order."""

        return self._repository.list_baskets()

    def list_items(self, basket_id: int | None = None) -> list[EvidenceItem]:
        """Return ordered evidence snapshots without silently repairing sources."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        return self._repository.list_items(basket.id)

    def contains(
        self,
        page_id: int,
        evidence_text: str,
        *,
        basket_id: int | None = None,
    ) -> bool:
        """Return whether the normalized selection already exists in the basket."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        selection = _clean_selection(evidence_text)
        return self._repository.contains(basket.id, page_id, _sha256(_match_text(selection)))

    def add_item(
        self,
        *,
        document_id: int,
        page_id: int,
        evidence_text: str,
        context: str = "",
        user_note: str = "",
        basket_id: int | None = None,
    ) -> EvidenceItem:
        """Validate a source and store one exact, classified evidence selection."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        document, page = self._validated_source(document_id, page_id)
        selection = _clean_selection(evidence_text)
        note = _clean_bounded(user_note, "用户备注", MAX_NOTE_CHARS)
        source_text = _original_source_text(page)
        normalized_source = _match_text(source_text)
        normalized_selection = _match_text(selection)
        is_original = bool(normalized_source and normalized_selection in normalized_source)
        text_kind = (
            EvidenceTextKind.ORIGINAL if is_original else EvidenceTextKind.USER_EXCERPT
        )
        clean_context = _clean_bounded(context, "上下文", MAX_CONTEXT_CHARS)
        if not clean_context and source_text:
            clean_context = build_context_excerpt(
                source_text,
                [selection] if is_original else (),
                max_chars=360,
            )
        tags = tuple(tag.name for tag in self._database.get_page_tags(page.id))
        projects = tuple(
            project.name for project in self._database.get_page_projects(page.id)
        )
        try:
            return self._repository.insert_item(
                basket_id=basket.id,
                document=document,
                page=page,
                projects=projects,
                tags=tags,
                evidence_text=selection,
                text_kind=text_kind,
                context=clean_context,
                user_note=note,
                source_text_sha256=_sha256(source_text),
                selection_sha256=_sha256(normalized_selection),
            )
        except sqlite3.IntegrityError as exc:
            if "unique" in str(exc).casefold():
                raise DuplicateEvidenceError("同一证据选区已在证据篮中，无需重复加入。") from exc
            raise EvidenceBasketError(f"无法保存证据：{exc}") from exc

    def remove_item(self, item_id: int, *, basket_id: int | None = None) -> None:
        """Remove one item and compact the remaining stable positions."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        if not self._repository.delete_item(basket.id, item_id):
            raise EvidenceBasketError(f"证据条目 {item_id} 不存在或不属于当前证据篮。")

    def clear(self, *, basket_id: int | None = None) -> int:
        """Explicitly clear a basket and return the removed item count."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        return self._repository.clear_items(basket.id)

    def update_note(
        self,
        item_id: int,
        user_note: str,
        *,
        basket_id: int | None = None,
    ) -> EvidenceItem:
        """Update only user-authored notes, preserving captured source material."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        note = _clean_bounded(user_note, "用户备注", MAX_NOTE_CHARS)
        item = self._repository.update_note(basket.id, item_id, note)
        if item is None:
            raise EvidenceBasketError(f"证据条目 {item_id} 不存在或不属于当前证据篮。")
        return item

    def reorder(
        self,
        ordered_item_ids: Sequence[int],
        *,
        basket_id: int | None = None,
    ) -> list[EvidenceItem]:
        """Persist an exact permutation of every current item in one transaction."""

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        self._repository.reorder(basket.id, ordered_item_ids)
        return self._repository.list_items(basket.id)

    def validated_items(self, basket_id: int | None = None) -> list[EvidenceItem]:
        """Return current metadata only after all stored sources pass integrity checks."""

        items = self.list_items(basket_id)
        validated: list[EvidenceItem] = []
        for item in items:
            document, page = self._validated_source(item.document_id, item.page_id)
            if page.page_number != item.page_number:
                raise EvidenceSourceError(
                    f"证据 {item.id} 的页码已与来源记录不一致，已停止继续处理。"
                )
            source_hash = _sha256(_original_source_text(page))
            if source_hash != item.source_text_sha256:
                raise EvidenceSourceError(
                    f"证据 {item.id} 的原始页面文本已发生变化，请重新核对并加入。"
                )
            tags = tuple(tag.name for tag in self._database.get_page_tags(page.id))
            projects = tuple(
                project.name for project in self._database.get_page_projects(page.id)
            )
            validated.append(
                replace(
                    item,
                    document_title=document.title,
                    filename=document.filename,
                    review_status=page.status,
                    projects=projects,
                    tags=tags,
                )
            )
        return validated

    def _require_basket(self, basket_id: int) -> EvidenceBasket:
        basket = self._repository.get_basket(basket_id)
        if basket is None:
            raise EvidenceBasketError(f"证据篮 {basket_id} 不存在。")
        return basket

    def _validated_source(self, document_id: int, page_id: int) -> tuple[Document, Page]:
        document = self._database.get_document(document_id)
        if document is None:
            raise EvidenceSourceError(f"文档记录不存在（文档编号 {document_id}）。")
        page = self._database.get_page(page_id)
        if page is None:
            raise EvidenceSourceError(f"页面记录不存在（页面编号 {page_id}）。")
        if page.document_id != document.id:
            raise EvidenceSourceError("页面所属文档不一致，已停止加入以避免错误引用。")
        if page.page_number <= 0 or page.page_number > document.page_count:
            raise EvidenceSourceError(
                f"页面 {page.id} 的页码 {page.page_number} 超出文档记录范围。"
            )
        if not document.source_path.is_file():
            raise EvidenceSourceError(f"原始 PDF 文件缺失：{document.source_path}")
        if not page.image_path.is_file():
            raise EvidenceSourceError(f"页面图像缺失：{page.image_path}")
        return document, page


def evidence_text_html(value: str) -> str:
    """Return evidence text escaped for any explicit unsafe-HTML rendering."""

    return html.escape(value, quote=True)


class _EvidenceRepository:
    """Small parameterized SQLite repository for schema v4 evidence tables."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_basket(self, name: str) -> EvidenceBasket:
        now = _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO evidence_baskets(name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
            row = connection.execute(
                "SELECT * FROM evidence_baskets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _basket_from_row(row)

    def get_or_create_basket(self, name: str) -> EvidenceBasket:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_baskets WHERE name = ? ORDER BY id LIMIT 1", (name,)
            ).fetchone()
            if row is None:
                now = _utc_now()
                cursor = connection.execute(
                    "INSERT INTO evidence_baskets(name, created_at, updated_at) "
                    "VALUES (?, ?, ?)",
                    (name, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM evidence_baskets WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        return _basket_from_row(row)

    def get_basket(self, basket_id: int) -> EvidenceBasket | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_baskets WHERE id = ?", (basket_id,)
            ).fetchone()
        return _basket_from_row(row) if row else None

    def list_baskets(self) -> list[EvidenceBasket]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_baskets ORDER BY id"
            ).fetchall()
        return [_basket_from_row(row) for row in rows]

    def contains(self, basket_id: int, page_id: int, selection_sha256: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM evidence_items "
                "WHERE basket_id = ? AND page_id = ? AND selection_sha256 = ?",
                (basket_id, page_id, selection_sha256),
            ).fetchone()
        return row is not None

    def insert_item(
        self,
        *,
        basket_id: int,
        document: Document,
        page: Page,
        projects: tuple[str, ...],
        tags: tuple[str, ...],
        evidence_text: str,
        text_kind: EvidenceTextKind,
        context: str,
        user_note: str,
        source_text_sha256: str,
        selection_sha256: str,
    ) -> EvidenceItem:
        now = _utc_now()
        locator = (
            f"document_id={document.id}; page_id={page.id}; "
            f"page_number={page.page_number}; document_sha256={document.sha256}"
        )
        with self._connection() as connection:
            position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM evidence_items "
                    "WHERE basket_id = ?",
                    (basket_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO evidence_items(
                    basket_id, document_id, page_id, document_title, filename,
                    page_number, review_status, projects_json, tags_json,
                    evidence_text, text_kind, context, user_note,
                    source_text_sha256, source_locator, selection_sha256,
                    added_at, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basket_id,
                    document.id,
                    page.id,
                    document.title,
                    document.filename,
                    page.page_number,
                    page.status.value,
                    json.dumps(projects, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    evidence_text,
                    text_kind.value,
                    context,
                    user_note,
                    source_text_sha256,
                    locator,
                    selection_sha256,
                    now,
                    position,
                ),
            )
            connection.execute(
                "UPDATE evidence_baskets SET updated_at = ? WHERE id = ?",
                (now, basket_id),
            )
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _item_from_row(row)

    def list_items(self, basket_id: int) -> list[EvidenceItem]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_items WHERE basket_id = ? "
                "ORDER BY position, id",
                (basket_id,),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def delete_item(self, basket_id: int, item_id: int) -> bool:
        now = _utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT position FROM evidence_items WHERE basket_id = ? AND id = ?",
                (basket_id, item_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM evidence_items WHERE id = ?", (item_id,))
            remaining_ids = tuple(
                int(item[0])
                for item in connection.execute(
                    "SELECT id FROM evidence_items WHERE basket_id = ? "
                    "ORDER BY position, id",
                    (basket_id,),
                ).fetchall()
            )
            count = len(remaining_ids)
            if count:
                connection.execute(
                    "UPDATE evidence_items SET position = position + ? "
                    "WHERE basket_id = ?",
                    (count, basket_id),
                )
                for position, remaining_id in enumerate(remaining_ids, start=1):
                    connection.execute(
                        "UPDATE evidence_items SET position = ? WHERE id = ?",
                        (position, remaining_id),
                    )
            connection.execute(
                "UPDATE evidence_baskets SET updated_at = ? WHERE id = ?",
                (now, basket_id),
            )
        return True

    def clear_items(self, basket_id: int) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM evidence_items WHERE basket_id = ?", (basket_id,)
            )
            connection.execute(
                "UPDATE evidence_baskets SET updated_at = ? WHERE id = ?",
                (_utc_now(), basket_id),
            )
            return max(cursor.rowcount, 0)

    def update_note(
        self, basket_id: int, item_id: int, user_note: str
    ) -> EvidenceItem | None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE evidence_items SET user_note = ? WHERE basket_id = ? AND id = ?",
                (user_note, basket_id, item_id),
            )
            if cursor.rowcount == 0:
                return None
            connection.execute(
                "UPDATE evidence_baskets SET updated_at = ? WHERE id = ?",
                (_utc_now(), basket_id),
            )
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (item_id,)
            ).fetchone()
        return _item_from_row(row)

    def reorder(self, basket_id: int, ordered_item_ids: Sequence[int]) -> None:
        requested = tuple(int(item_id) for item_id in ordered_item_ids)
        if len(requested) != len(set(requested)):
            raise EvidenceBasketError("证据排序中包含重复条目。")
        with self._connection() as connection:
            current = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM evidence_items WHERE basket_id = ? ORDER BY position, id",
                    (basket_id,),
                ).fetchall()
            )
            if set(requested) != set(current) or len(requested) != len(current):
                raise EvidenceBasketError("排序列表必须完整包含当前证据篮的全部条目。")
            count = len(current)
            if count:
                connection.execute(
                    "UPDATE evidence_items SET position = position + ? WHERE basket_id = ?",
                    (count, basket_id),
                )
                for position, item_id in enumerate(requested, start=1):
                    connection.execute(
                        "UPDATE evidence_items SET position = ? "
                        "WHERE basket_id = ? AND id = ?",
                        (position, basket_id, item_id),
                    )
            connection.execute(
                "UPDATE evidence_baskets SET updated_at = ? WHERE id = ?",
                (_utc_now(), basket_id),
            )


def _basket_from_row(row: sqlite3.Row) -> EvidenceBasket:
    return EvidenceBasket(
        id=int(row["id"]),
        name=str(row["name"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _item_from_row(row: sqlite3.Row) -> EvidenceItem:
    try:
        projects = tuple(str(value) for value in json.loads(str(row["projects_json"])))
        tags = tuple(str(value) for value in json.loads(str(row["tags_json"])))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceSourceError(f"证据 {row['id']} 的分类元数据损坏。") from exc
    return EvidenceItem(
        id=int(row["id"]),
        basket_id=int(row["basket_id"]),
        document_id=int(row["document_id"]),
        page_id=int(row["page_id"]),
        document_title=str(row["document_title"]),
        filename=str(row["filename"]),
        page_number=int(row["page_number"]),
        review_status=PageStatus(str(row["review_status"])),
        projects=projects,
        tags=tags,
        evidence_text=str(row["evidence_text"]),
        text_kind=EvidenceTextKind(str(row["text_kind"])),
        context=str(row["context"]),
        user_note=str(row["user_note"]),
        source_text_sha256=str(row["source_text_sha256"]),
        source_locator=str(row["source_locator"]),
        added_at=datetime.fromisoformat(str(row["added_at"])),
        position=int(row["position"]),
    )


def _clean_basket_name(value: str) -> str:
    name = " ".join(unicodedata.normalize("NFKC", value or "").strip().split())
    if not name:
        raise EvidenceBasketError("证据篮名称不能为空。")
    if len(name) > 100:
        raise EvidenceBasketError("证据篮名称不能超过 100 个字符。")
    return name


def _clean_selection(value: str) -> str:
    selection = unicodedata.normalize("NFKC", value or "").strip()
    if not selection:
        raise EvidenceBasketError("证据选区不能为空。")
    if len(selection) > MAX_EVIDENCE_CHARS:
        raise EvidenceBasketError(f"证据选区不能超过 {MAX_EVIDENCE_CHARS} 个字符。")
    return selection


def _clean_bounded(value: str, label: str, maximum: int) -> str:
    cleaned = unicodedata.normalize("NFKC", value or "").strip()
    if len(cleaned) > maximum:
        raise EvidenceBasketError(f"{label}不能超过 {maximum} 个字符。")
    return cleaned


def _match_text(value: str) -> str:
    """Normalize Unicode and whitespace while retaining Chinese continuity."""

    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _original_source_text(page: Page) -> str:
    return "\n".join(
        part.strip() for part in (page.extracted_text, page.ocr_text) if part.strip()
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "DuplicateEvidenceError",
    "EmptyEvidenceBasketError",
    "EvidenceBasketError",
    "EvidenceBasketService",
    "EvidenceSourceError",
    "evidence_text_html",
]
