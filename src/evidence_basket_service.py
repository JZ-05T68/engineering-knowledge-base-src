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
from io import BytesIO
from pathlib import Path
from typing import Protocol

from PIL import Image

from src.models import (
    Document,
    EvidenceBasket,
    EvidenceConfirmationStatus,
    EvidenceContextKind,
    EvidenceItem,
    EvidenceTextKind,
    EvidenceType,
    Page,
    PageStatus,
)
from src.note_geometry import normalize_original_rect
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

    def get_item(self, item_id: int) -> EvidenceItem | None:
        """Return one evidence item by primary key, or ``None`` when absent."""

        return self._repository.get_item(item_id)

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
        context_kind = (
            EvidenceContextKind.USER_PROVIDED
            if clean_context
            else EvidenceContextKind.SYSTEM_GENERATED
        )
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
                context_kind=context_kind,
                user_note=note,
                source_text_sha256=_sha256(source_text),
                selection_sha256=_sha256(normalized_selection),
            )
        except sqlite3.IntegrityError as exc:
            if "unique" in str(exc).casefold():
                raise DuplicateEvidenceError("同一证据选区已在证据篮中，无需重复加入。") from exc
            raise EvidenceBasketError(f"无法保存证据：{exc}") from exc

    def add_page_item(
        self,
        basket_id: int,
        document_id: int,
        page_id: int,
        user_note: str = "",
    ) -> EvidenceItem:
        """Validate a source and store one whole-page evidence item.

        ``source_text_sha256`` freezes the page's current original text hash as
        audit information only: whole-page reference semantics do not change
        when the page text is edited, so ``validated_item`` for page evidence
        checks document/page/file existence, not the text hash.
        ``selection_sha256`` is the SHA-256 of the empty byte string, so the
        existing UNIQUE(basket_id, page_id, selection_sha256) constraint
        allows at most one whole-page item per page per basket.
        """

        basket = self._require_basket(basket_id)
        document, page = self._validated_source(document_id, page_id)
        note = _clean_bounded(user_note, "用户备注", MAX_NOTE_CHARS)
        projects, tags = self._page_classification(page)
        source_text = _original_source_text(page)
        context = (
            build_context_excerpt(source_text, (), max_chars=360) if source_text else ""
        )
        try:
            return self._repository.insert_item(
                basket_id=basket.id,
                document=document,
                page=page,
                projects=projects,
                tags=tags,
                evidence_text="",
                text_kind=EvidenceTextKind.ORIGINAL,
                context=context,
                context_kind=EvidenceContextKind.SYSTEM_GENERATED,
                user_note=note,
                source_text_sha256=_sha256(source_text),
                selection_sha256=_sha256(""),
                evidence_type=EvidenceType.PAGE,
            )
        except sqlite3.IntegrityError as exc:
            if "unique" in str(exc).casefold():
                raise DuplicateEvidenceError(
                    "该页面的整页证据已在证据篮中，无需重复加入。"
                ) from exc
            raise EvidenceBasketError(f"无法保存证据：{exc}") from exc

    def add_region_item(
        self,
        basket_id: int,
        document_id: int,
        page_id: int,
        *,
        x0: object,
        y0: object,
        x1: object,
        y1: object,
        user_note: str = "",
    ) -> EvidenceItem:
        """Validate a source and store one image-region evidence item.

        Width, height and SHA-256 are always measured from the real page PNG
        (fully decoded, never trusted from the caller); coordinates are
        normalized with the same rules as structured region notes.
        ``selection_sha256`` hashes the normalized ``x0,y0,x1,y1`` tuple, so
        the same region on the same page can only be added once per basket.
        ``source_text_sha256`` records the page text hash at capture time as
        audit information; the durable anchor is the image hash.
        """

        basket = self._require_basket(basket_id)
        document, page = self._validated_source(document_id, page_id)
        note = _clean_bounded(user_note, "用户备注", MAX_NOTE_CHARS)
        width, height, image_sha256 = _read_page_image_info(page.image_path)
        try:
            rect = normalize_original_rect(x0, y0, x1, y1, width, height)
        except ValueError as exc:
            raise EvidenceBasketError(f"图片区域无效：{exc}") from exc
        projects, tags = self._page_classification(page)
        source_text = _original_source_text(page)
        context = (
            build_context_excerpt(source_text, (), max_chars=360) if source_text else ""
        )
        selection_sha256 = _sha256(
            f"{rect['x0']},{rect['y0']},{rect['x1']},{rect['y1']}"
        )
        try:
            return self._repository.insert_item(
                basket_id=basket.id,
                document=document,
                page=page,
                projects=projects,
                tags=tags,
                evidence_text="",
                text_kind=EvidenceTextKind.ORIGINAL,
                context=context,
                context_kind=EvidenceContextKind.SYSTEM_GENERATED,
                user_note=note,
                source_text_sha256=_sha256(source_text),
                selection_sha256=selection_sha256,
                evidence_type=EvidenceType.IMAGE_REGION,
                region_image_sha256=image_sha256,
                region_image_width=width,
                region_image_height=height,
                region_x0=rect["x0"],
                region_y0=rect["y0"],
                region_x1=rect["x1"],
                region_y1=rect["y1"],
            )
        except sqlite3.IntegrityError as exc:
            if "unique" in str(exc).casefold():
                raise DuplicateEvidenceError(
                    "相同坐标的图片区域证据已在证据篮中，无需重复加入。"
                ) from exc
            raise EvidenceBasketError(f"无法保存证据：{exc}") from exc

    def set_confirmation(self, item_id: int, confirmed: bool) -> EvidenceItem:
        """Set the manual confirmation state of one evidence item.

        Repeating the call with the state the item already has is an error
        (``EvidenceBasketError``), never a silent no-op: callers must observe
        that no transition happened.
        """

        if not isinstance(confirmed, bool):
            raise EvidenceBasketError("确认状态必须是布尔值。")
        return self._repository.set_confirmation(item_id, confirmed)

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
        return [self._validated_snapshot(item) for item in items]

    def validated_item(
        self, item_id: int, *, basket_id: int | None = None
    ) -> EvidenceItem:
        """Validate and refresh one item for safe source navigation."""

        items = self.list_items(basket_id)
        item = next((candidate for candidate in items if candidate.id == item_id), None)
        if item is None:
            raise EvidenceBasketError(f"证据条目 {item_id} 不存在或不属于当前证据篮。")
        return self._validated_snapshot(item)

    def export_markdown(
        self,
        *,
        basket_id: int | None = None,
        title: str | None = None,
        generated_at: datetime | None = None,
    ) -> str:
        """Validate every live source, then build an ordered Markdown package."""

        from src.evidence_service import EvidenceBasketPackageBuilder

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        items = self.validated_items(basket.id)
        if not items:
            raise EmptyEvidenceBasketError("证据篮为空，无法生成证据包。")
        return EvidenceBasketPackageBuilder().build(
            basket,
            items,
            title=title,
            generated_at=generated_at,
        )

    def export_prompt_package(
        self,
        question: str,
        *,
        basket_id: int | None = None,
    ) -> str:
        """Validate every confirmed source, then build a grounded prompt package.

        Only confirmed evidence participates. Any confirmed item whose stored
        source no longer passes the existing integrity checks aborts the whole
        generation (fail closed); unconfirmed items are never validated here
        because they never enter the package.
        """

        from src.evidence_prompt_builder import EvidencePromptBuilder

        basket = self.default_basket() if basket_id is None else self._require_basket(basket_id)
        validated, page_texts = self._confirmed_prompt_inputs(basket.id)
        return EvidencePromptBuilder().build(question, validated, page_texts=page_texts)

    def prompt_package_fingerprint(
        self,
        question: str,
        *,
        basket_id: int | None = None,
    ) -> str:
        """Hash the exact prompt that the current effective inputs would generate.

        Freshness guard (v0.4.2): the fingerprint is derived from the same
        validated, confirmed-only inputs as :meth:`export_prompt_package`, so
        any change that would alter the generated prompt — question, confirmed
        set, order, notes, page text or source validity — changes the
        fingerprint, while unconfirmed-only changes never do. Source
        validation stays fail closed: a broken confirmed source raises here
        exactly as it would during generation.
        """

        prompt = self.export_prompt_package(question, basket_id=basket_id)
        return _sha256(prompt)

    def _confirmed_prompt_inputs(
        self, basket_id: int
    ) -> tuple[list[EvidenceItem], dict[int, str]]:
        """Return validated confirmed items and current PAGE source texts."""

        from src.evidence_prompt_builder import NO_CONFIRMED_EVIDENCE_MESSAGE

        confirmed = [
            item
            for item in self.list_items(basket_id)
            if item.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
        ]
        if not confirmed:
            raise EmptyEvidenceBasketError(NO_CONFIRMED_EVIDENCE_MESSAGE)
        validated = [self._validated_snapshot(item) for item in confirmed]
        page_texts: dict[int, str] = {}
        for item in validated:
            if item.evidence_type is not EvidenceType.PAGE:
                continue
            page = self._database.get_page(item.page_id)
            if page is None:  # pragma: no cover - _validated_snapshot already checked
                raise EvidenceSourceError(f"页面记录不存在（页面编号 {item.page_id}）。")
            page_texts[item.id] = _original_source_text(page)
        return validated, page_texts

    def _require_basket(self, basket_id: int) -> EvidenceBasket:
        basket = self._repository.get_basket(basket_id)
        if basket is None:
            raise EvidenceBasketError(f"证据篮 {basket_id} 不存在。")
        return basket

    def _validated_snapshot(self, item: EvidenceItem) -> EvidenceItem:
        document, page = self._validated_source(item.document_id, item.page_id)
        if page.page_number != item.page_number:
            raise EvidenceSourceError(
                f"证据 {item.id} 的页码已与来源记录不一致，已停止继续处理。"
            )
        if item.evidence_type is EvidenceType.TEXT_SELECTION:
            source_hash = _sha256(_original_source_text(page))
            if source_hash != item.source_text_sha256:
                raise EvidenceSourceError(
                    f"证据 {item.id} 的原始页面文本已发生变化，请重新核对并加入。"
                )
        elif item.evidence_type is EvidenceType.IMAGE_REGION:
            self._validate_region_anchor(item, page)
        # 整页证据：引用语义不随页面文本变化，仅依赖文档/页面/文件存在（上方已校验）。
        tags = tuple(tag.name for tag in self._database.get_page_tags(page.id))
        projects = tuple(
            project.name for project in self._database.get_page_projects(page.id)
        )
        normalized_ocr = _match_text(page.ocr_text)
        from_ocr = (
            item.evidence_type is EvidenceType.TEXT_SELECTION
            and item.text_kind is EvidenceTextKind.ORIGINAL
            and bool(normalized_ocr)
            and _match_text(item.evidence_text) in normalized_ocr
        )
        return replace(
            item,
            document_title=document.title,
            filename=document.filename,
            review_status=page.status,
            projects=projects,
            tags=tags,
            document_source_path=document.source_path,
            image_path=page.image_path,
            document_sha256=document.sha256,
            from_ocr_text=from_ocr,
        )

    def _validate_region_anchor(self, item: EvidenceItem, page: Page) -> None:
        width, height, image_sha256 = _read_page_image_info(page.image_path)
        if image_sha256 != item.region_image_sha256:
            raise EvidenceSourceError(
                f"证据 {item.id} 的页面图像已发生变化，区域锚点失效，请重新核对并加入。"
            )
        coordinates = (
            item.region_x0,
            item.region_y0,
            item.region_x1,
            item.region_y1,
        )
        if any(value is None for value in coordinates) or not (
            0 <= item.region_x0 < item.region_x1 <= width
            and 0 <= item.region_y0 < item.region_y1 <= height
        ):
            raise EvidenceSourceError(
                f"证据 {item.id} 的区域坐标已超出当前页面图像范围。"
            )

    def _page_classification(self, page: Page) -> tuple[tuple[str, ...], tuple[str, ...]]:
        tags = tuple(tag.name for tag in self._database.get_page_tags(page.id))
        projects = tuple(
            project.name for project in self._database.get_page_projects(page.id)
        )
        return projects, tags

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
            # BEGIN IMMEDIATE serializes the check-then-insert below: without a
            # write lock, concurrent first access can create duplicate baskets
            # because evidence_baskets.name has no UNIQUE constraint.
            connection.execute("BEGIN IMMEDIATE")
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
        context_kind: EvidenceContextKind,
        user_note: str,
        source_text_sha256: str,
        selection_sha256: str,
        evidence_type: EvidenceType = EvidenceType.TEXT_SELECTION,
        region_image_sha256: str | None = None,
        region_image_width: int | None = None,
        region_image_height: int | None = None,
        region_x0: int | None = None,
        region_y0: int | None = None,
        region_x1: int | None = None,
        region_y1: int | None = None,
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
                    evidence_type, evidence_text, text_kind, context, context_kind,
                    user_note, region_image_sha256, region_image_width,
                    region_image_height, region_x0, region_y0, region_x1, region_y1,
                    source_text_sha256, source_locator, selection_sha256,
                    added_at, position
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
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
                    evidence_type.value,
                    evidence_text,
                    text_kind.value,
                    context,
                    context_kind.value,
                    user_note,
                    region_image_sha256,
                    region_image_width,
                    region_image_height,
                    region_x0,
                    region_y0,
                    region_x1,
                    region_y1,
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

    def set_confirmation(self, item_id: int, confirmed: bool) -> EvidenceItem:
        """Flip one item's confirmation state in a single guarded UPDATE."""

        target = (
            EvidenceConfirmationStatus.CONFIRMED
            if confirmed
            else EvidenceConfirmationStatus.UNCONFIRMED
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise EvidenceBasketError(f"证据条目 {item_id} 不存在。")
            current = EvidenceConfirmationStatus(str(row["confirmation_status"]))
            if current is target:
                raise EvidenceBasketError(
                    f"证据条目 {item_id} 已是{target.label}状态，无需重复操作。"
                )
            confirmed_at = _utc_now() if confirmed else None
            cursor = connection.execute(
                "UPDATE evidence_items SET confirmation_status = ?, confirmed_at = ? "
                "WHERE id = ?",
                (target.value, confirmed_at, item_id),
            )
            if cursor.rowcount != 1:
                raise EvidenceBasketError(f"证据条目 {item_id} 的确认状态更新未生效。")
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (item_id,)
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

    def get_item(self, item_id: int) -> EvidenceItem | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (item_id,)
            ).fetchone()
        return _item_from_row(row) if row is not None else None

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
    confirmed_at = row["confirmed_at"]
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
        context_kind=EvidenceContextKind(str(row["context_kind"])),
        user_note=str(row["user_note"]),
        source_text_sha256=str(row["source_text_sha256"]),
        source_locator=str(row["source_locator"]),
        added_at=datetime.fromisoformat(str(row["added_at"])),
        position=int(row["position"]),
        evidence_type=EvidenceType(str(row["evidence_type"])),
        confirmation_status=EvidenceConfirmationStatus(str(row["confirmation_status"])),
        confirmed_at=(
            datetime.fromisoformat(str(confirmed_at)) if confirmed_at else None
        ),
        region_image_sha256=(
            str(row["region_image_sha256"]) if row["region_image_sha256"] else None
        ),
        region_image_width=_optional_int(row["region_image_width"]),
        region_image_height=_optional_int(row["region_image_height"]),
        region_x0=_optional_int(row["region_x0"]),
        region_y0=_optional_int(row["region_y0"]),
        region_x1=_optional_int(row["region_x1"]),
        region_y1=_optional_int(row["region_y1"]),
    )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _read_page_image_info(image_path: Path) -> tuple[int, int, str]:
    """Fully decode one page PNG and return ``(width, height, sha256)``.

    Same semantics as structured region notes: PIL decoding is lazy, so
    ``load()`` is forced to prove the pixel data is intact before its hash is
    stored or trusted as an anchor.
    """

    if not image_path.is_file():
        raise EvidenceSourceError(f"页面图像缺失：{image_path}")
    try:
        data = image_path.read_bytes()
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise EvidenceSourceError(f"页面图像无法读取：{image_path}") from exc
    return width, height, hashlib.sha256(data).hexdigest()


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
