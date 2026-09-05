"""Plain-language UI for content the user explicitly chose to save."""

from __future__ import annotations

import logging

import streamlit as st

from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
)
from src.models import KnowledgeMemoryEntry

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 100
_OPEN_ENTRY_KEY = "saved_content_open_entry_id"
_DELETE_ENTRY_KEY = "saved_content_delete_entry_id"


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
        return
    st.caption(f"共保存了 {total} 条内容")
    for entry in entries:
        _render_entry(service, entry, database=database)


def _render_entry(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Render one compact card with only date, title, preview and two actions."""

    opened = st.session_state.get(_OPEN_ENTRY_KEY) == entry.id
    pending_delete = st.session_state.get(_DELETE_ENTRY_KEY) == entry.id
    with st.container(border=True):
        st.caption(_friendly_date(entry.created_at))
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
            st.markdown("#### 保存的完整内容")
            st.write(entry.content or "（没有正文）")
            if entry.root_cause.strip():
                st.markdown(f"**原因**：{entry.root_cause}")
            if entry.lesson.strip():
                st.markdown(f"**以后可以这样做**：{entry.lesson}")

        if pending_delete:
            st.warning("确定删除这条内容吗？删除后无法恢复，但不会删除原始资料。")
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


def _delete_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Delete only after the user confirms the specific saved item."""

    try:
        service.delete_entry(entry_id)
    except KnowledgeMemoryEntryNotFoundError as exc:
        st.error(str(exc))
    else:
        if st.session_state.get(_OPEN_ENTRY_KEY) == entry_id:
            st.session_state.pop(_OPEN_ENTRY_KEY, None)
        st.session_state.pop(_DELETE_ENTRY_KEY, None)
        st.session_state["saved_content_flash"] = "这条内容已删除。"
        st.rerun()


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
