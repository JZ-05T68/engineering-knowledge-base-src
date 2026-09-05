"""Plain-language UI for content the user explicitly chose to save.

v0.7 Phase 1 boundary: raw saved Q&A (``kind='raw_qa'``) is always shown as
"保存的问答" — a verbatim question + agent answer copy the user kept. It is
never presented as user experience. Structured experiences are shown as
"经验". Deleted entries move to a simple "最近删除" list with restore and
explicit permanent-delete actions; nothing is destroyed in one step.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
)
from src.models import (
    KnowledgeMemoryEntry,
    MemoryCitation,
    MemoryCitationSnapshotError,
    parse_memory_citations,
)

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 100
_OPEN_ENTRY_KEY = "saved_content_open_entry_id"
_DELETE_ENTRY_KEY = "saved_content_delete_entry_id"
_PURGE_ENTRY_KEY = "saved_content_purge_entry_id"


def render_knowledge_memory_page(
    service: KnowledgeMemoryService, *, database: Database | None = None
) -> None:
    """Render saved items without exposing internal types, IDs or status fields."""

    flash = st.session_state.pop("saved_content_flash", "")
    if flash:
        st.success(flash)
    entries = service.list(limit=PAGE_SIZE)
    total = service.count()
    if total == 0:
        st.info("你还没有保存过内容。向 Agent 提问后，可以手动保存有用的问答。")
    else:
        st.caption(f"共保存了 {total} 条内容")
        for entry in entries:
            _render_entry(service, entry, database=database)
    _render_deleted_section(service)


def _render_entry(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Render one compact card with date, type, title, preview and actions."""

    opened = st.session_state.get(_OPEN_ENTRY_KEY) == entry.id
    pending_delete = st.session_state.get(_DELETE_ENTRY_KEY) == entry.id
    with st.container(border=True):
        st.caption(
            f"{_friendly_date(entry.created_at)} · {_kind_label(entry)}"
        )
        st.markdown(f"**{_display_title(entry, database)}**")
        preview = _content_preview(entry.content)
        if preview:
            st.write(f"“{preview}”")
        action_columns = st.columns([1, 1])
        if action_columns[0].button(
            "收起" if opened else "查看",
            key=f"saved_view_{entry.id}",
            use_container_width=True,
        ):
            st.session_state[_OPEN_ENTRY_KEY] = None if opened else entry.id
            st.rerun()
        if action_columns[1].button(
            "删除", key=f"saved_delete_{entry.id}", use_container_width=True
        ):
            st.session_state[_DELETE_ENTRY_KEY] = entry.id
            st.rerun()

        if opened:
            _render_entry_detail(entry, database=database)

        if pending_delete:
            st.warning(
                "确定删除这条内容吗？删除后可以在“最近删除”里恢复，"
                "原始资料不受影响。"
            )
            confirm_column, cancel_column = st.columns([1, 1])
            if confirm_column.button(
                "确认删除",
                key=f"saved_confirm_delete_{entry.id}",
                type="primary",
                use_container_width=True,
            ):
                _delete_entry(service, entry.id)
            if cancel_column.button(
                "取消", key=f"saved_cancel_delete_{entry.id}", use_container_width=True
            ):
                st.session_state.pop(_DELETE_ENTRY_KEY, None)
                st.rerun()


def _render_entry_detail(
    entry: KnowledgeMemoryEntry, *, database: Database | None
) -> None:
    """Render the expanded view: full copy, citation history and notes."""

    st.markdown("#### 保存的完整内容")
    st.write(entry.content or "（没有正文）")
    if entry.root_cause.strip():
        st.markdown(f"**原因**：{entry.root_cause}")
    if entry.lesson.strip():
        st.markdown(f"**以后可以这样做**：{entry.lesson}")
    if database is not None:
        _render_citation_history(entry, database)


def _render_citation_history(entry: KnowledgeMemoryEntry, database: Database) -> None:
    """Show what the saved answer cited, honoring deleted source material.

    The snapshot is frozen at save time, so a citation survives the deletion
    of its document as an honest historical note — never as a link that
    pretends the material is still openable.
    """

    try:
        citations = parse_memory_citations(entry.citation_snapshot)
    except MemoryCitationSnapshotError:
        st.caption("保存时记录的引用信息无法解析，已跳过显示。")
        return
    if not citations:
        return
    st.markdown("##### 保存时引用的原始资料")
    for citation in citations:
        st.write(_citation_line(citation, database))


def _citation_line(citation: MemoryCitation, database: Database) -> str:
    """Return one plain-language citation line with live availability."""

    location = f"第 {citation.page_number} 页" if citation.page_number else ""
    document = (
        database.get_document(citation.document_id)
        if citation.document_id is not None
        else None
    )
    page_exists = (
        citation.page_id is None or database.get_page(citation.page_id) is not None
    )
    title = citation.document_title or "一份已删除的资料"
    if document is not None and page_exists:
        return f"《{title}》{location}".strip()
    if location:
        return f"曾引用《{title}》（{location}），但原资料现已不可用。"
    return f"曾引用《{title}》，但原资料现已不可用。"


def _render_deleted_section(service: KnowledgeMemoryService) -> None:
    """Render the simple restore area for tombstoned entries."""

    try:
        deleted_entries = service.list_deleted(limit=PAGE_SIZE)
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("读取最近删除失败")
        return
    if not deleted_entries:
        return
    with st.expander(f"最近删除（{len(deleted_entries)} 条）"):
        st.caption("删除的内容先放在这里；确认不再需要时才永久删除。")
        for entry in deleted_entries:
            _render_deleted_entry(service, entry)


def _render_deleted_entry(
    service: KnowledgeMemoryService, entry: KnowledgeMemoryEntry
) -> None:
    """Render one tombstoned entry with restore and explicit purge actions."""

    pending_purge = st.session_state.get(_PURGE_ENTRY_KEY) == entry.id
    with st.container(border=True):
        st.caption(
            f"{_friendly_date(entry.created_at)} · {_kind_label(entry)}"
        )
        st.write(entry.title)
        action_columns = st.columns([1, 1])
        if action_columns[0].button(
            "恢复",
            key=f"saved_restore_{entry.id}",
            use_container_width=True,
        ):
            _restore_entry(service, entry.id)
        if action_columns[1].button(
            "永久删除", key=f"saved_purge_{entry.id}", use_container_width=True
        ):
            st.session_state[_PURGE_ENTRY_KEY] = entry.id
            st.rerun()
        if pending_purge:
            st.error(
                "永久删除后无法恢复。原始资料不受影响，但这份保存内容会彻底消失。"
            )
            confirm_column, cancel_column = st.columns([1, 1])
            if confirm_column.button(
                "确认永久删除",
                key=f"saved_purge_confirm_{entry.id}",
                type="primary",
                use_container_width=True,
            ):
                _purge_entry(service, entry.id)
            if cancel_column.button(
                "取消", key=f"saved_purge_cancel_{entry.id}", use_container_width=True
            ):
                st.session_state.pop(_PURGE_ENTRY_KEY, None)
                st.rerun()


def _delete_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Move one saved item to the restore area (soft delete)."""

    try:
        service.delete_entry(entry_id)
    except KnowledgeMemoryEntryNotFoundError as exc:
        st.error(str(exc))
    else:
        st.session_state.pop(_DELETE_ENTRY_KEY, None)
        if st.session_state.get(_OPEN_ENTRY_KEY) == entry_id:
            st.session_state.pop(_OPEN_ENTRY_KEY, None)
        st.session_state["saved_content_flash"] = "这条内容已删除，可在“最近删除”里恢复。"
        st.rerun()


def _restore_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Restore one tombstoned entry back into the saved list."""

    try:
        service.restore_entry(entry_id)
    except Exception as exc:
        LOGGER.exception("恢复保存内容失败")
        st.error(f"恢复失败：{exc}")
    else:
        st.session_state["saved_content_flash"] = "这条内容已恢复。"
        st.rerun()


def _purge_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Permanently delete one already-deleted entry."""

    try:
        service.purge_entry(entry_id)
    except Exception as exc:
        LOGGER.exception("永久删除保存内容失败")
        st.error(f"永久删除失败：{exc}")
    else:
        st.session_state.pop(_PURGE_ENTRY_KEY, None)
        st.session_state["saved_content_flash"] = "这条内容已永久删除。"
        st.rerun()


def _kind_label(entry: KnowledgeMemoryEntry) -> str:
    """Return the plain-language record type shown on every card.

    Raw saved Q&A must never be labelled as experience; its fixed label is
    "保存的问答".
    """

    if entry.kind.value == "raw_qa":
        return "保存的问答"
    return entry.kind.label


def _friendly_date(value) -> str:
    """Return a short date a non-technical user can scan quickly."""

    local_value = value.astimezone() if value.tzinfo is not None else value
    return f"{local_value.month} 月 {local_value.day} 日"


def _display_title(entry: KnowledgeMemoryEntry, database: Database | None) -> str:
    """Use a document name when available, without exposing its internal ID."""

    if database is not None and entry.document_id is not None:
        document = database.get_document(entry.document_id)
        if document is not None:
            return f"关于 {document.title} 的讨论"
    return entry.title


def _content_preview(content: str, *, limit: int = 140) -> str:
    """Prefer the saved Agent answer and keep the card preview compact."""

    text = content.strip()
    if "Agent 回答：" in text:
        text = text.split("Agent 回答：", 1)[1].strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"
