"""Streamlit rendering helpers for the reader's structured-notes tab.

This module only handles presentation, widget keys, form state and error
display. Every database operation goes through ``NoteService`` — no SQL here.
The tab offers full CRUD for document-level and page-level notes in v0.3.0;
text-selection and image-region notes are rendered read-only as a defensive
measure until their dedicated interactions land in later tasks.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.models import NoteType, NoteView
from src.note_service import (
    NoteDocumentNotFoundError,
    NoteNotFoundError,
    NotePageNotFoundError,
    NoteService,
    NoteValidationError,
)

LOGGER = logging.getLogger(__name__)

_FLASH_KEY = "note_tab_flash"
_PENDING_CLEAR_KEY = "note_tab_pending_key_clear"
MAX_NOTE_TEXT = 20000


def note_create_key(scope: str, owner_id: int) -> str:
    """Build the create-form widget key (type + owning id + create mode)."""

    return f"note_create_{scope}_{owner_id}"


def _edit_mode_key(note_id: int) -> str:
    return f"note_edit_mode_{note_id}"


def _edit_input_key(note_id: int) -> str:
    return f"note_edit_input_{note_id}"


def _delete_confirm_key(note_id: int) -> str:
    return f"note_delete_confirm_{note_id}"


def render_structured_notes_tab(
    note_service: NoteService, *, document_id: int, page_id: int
) -> None:
    """Render the whole「结构化笔记」tab: document notes + current-page notes."""

    flash = st.session_state.pop(_FLASH_KEY, "")
    if flash:
        st.success(flash)
    _apply_pending_key_clears()
    _render_document_section(note_service, document_id)
    st.divider()
    _render_page_section(note_service, page_id)


def _render_document_section(note_service: NoteService, document_id: int) -> None:
    with st.expander("文档级笔记", expanded=False):
        try:
            views = note_service.list_document_notes(document_id)
        except Exception as exc:
            _show_load_error(exc)
            return
        if not views:
            st.caption("这份文档还没有文档级笔记。")
        for view in views:
            _render_editable_note(
                view,
                save=lambda note_id, text: note_service.update_document_note(note_id, text),
                delete=note_service.delete_note,
                delete_label="我确认删除这条文档级笔记",
                delete_flash="文档级笔记已删除。",
            )
        _render_create_form(
            note_service,
            scope="document",
            owner_id=document_id,
            title="新建文档级笔记",
            create=lambda text: note_service.create_document_note(document_id, text),
            flash="文档级笔记已保存。",
        )


def _render_page_section(note_service: NoteService, page_id: int) -> None:
    st.subheader("本页笔记")
    try:
        views = note_service.list_page_notes(page_id)
    except Exception as exc:
        _show_load_error(exc)
        return
    page_views = [view for view in views if view.note.note_type is NoteType.PAGE]
    anchor_views = [view for view in views if view.note.note_type is not NoteType.PAGE]
    if not page_views and not anchor_views:
        st.caption("当前页面还没有页面级笔记。")
    for view in page_views:
        _render_editable_note(
            view,
            save=lambda note_id, text: note_service.update_page_note(note_id, text),
            delete=note_service.delete_note,
            delete_label="我确认删除这条页面级笔记",
            delete_flash="页面级笔记已删除。",
        )
    for view in anchor_views:
        _render_readonly_anchor_note(view)
    _render_create_form(
        note_service,
        scope="page",
        owner_id=page_id,
        title="新建页面级笔记",
        create=lambda text: note_service.create_page_note(page_id, text),
        flash="页面级笔记已保存。",
    )


def _render_create_form(
    note_service: NoteService,
    *,
    scope: str,
    owner_id: int,
    title: str,
    create,
    flash: str,
) -> None:
    del note_service
    key = note_create_key(scope, owner_id)
    text = st.text_area(
        f"{title}（最多 {MAX_NOTE_TEXT} 字符）",
        height=140,
        key=key,
        placeholder="记录你的判断、说明和想法。",
    )
    if st.button(f"保存{title.removeprefix('新建')}", key=f"{key}_save"):
        if not text.strip():
            st.warning("个人笔记不能为空")
            return
        try:
            create(text)
        except Exception as exc:
            _show_save_error(exc)  # 输入保留在 widget 中
        else:
            _queue_key_clear(key)
            st.session_state[_FLASH_KEY] = flash
            st.rerun()


def _render_editable_note(
    view: NoteView, *, save, delete, delete_label: str, delete_flash: str
) -> None:
    note = view.note
    with st.container(border=True):
        st.caption(
            f"{note.note_type.label} #{note.id} · "
            f"创建于 {_format_time(note.created_at)} · 更新于 {_format_time(note.updated_at)}"
        )
        if st.session_state.get(_edit_mode_key(note.id)):
            draft = st.text_area(
                "编辑笔记",
                value=note.personal_note,
                height=140,
                key=_edit_input_key(note.id),
                label_visibility="collapsed",
            )
            if draft != note.personal_note:
                st.warning("● 有未保存修改")
            save_column, cancel_column = st.columns(2)
            if save_column.button("保存修改", key=f"note_edit_save_{note.id}"):
                if not draft.strip():
                    st.warning("个人笔记不能为空")
                    return
                try:
                    save(note.id, draft)
                except Exception as exc:
                    _show_save_error(exc)  # 输入保留
                else:
                    _queue_key_clear(_edit_mode_key(note.id), _edit_input_key(note.id))
                    st.session_state[_FLASH_KEY] = "笔记已保存。"
                    st.rerun()
            if cancel_column.button("取消编辑", key=f"note_edit_cancel_{note.id}"):
                _queue_key_clear(_edit_mode_key(note.id), _edit_input_key(note.id))
                st.rerun()
        else:
            st.markdown(note.personal_note)
            if st.button("编辑", key=f"note_edit_open_{note.id}"):
                st.session_state[_edit_mode_key(note.id)] = True
                st.rerun()
        with st.expander("删除这条笔记", expanded=False):
            confirmed = st.checkbox(delete_label, key=_delete_confirm_key(note.id))
            if st.button("永久删除这条笔记", key=f"note_delete_{note.id}", disabled=not confirmed):
                try:
                    delete(note.id)
                except Exception as exc:
                    _show_delete_error(exc)
                else:
                    _queue_key_clear(
                        _edit_mode_key(note.id),
                        _edit_input_key(note.id),
                        _delete_confirm_key(note.id),
                    )
                    st.session_state[_FLASH_KEY] = delete_flash
                    st.rerun()


def _render_readonly_anchor_note(view: NoteView) -> None:
    note = view.note
    status_label = view.source_status.label if view.source_status else ""
    with st.container(border=True):
        if note.note_type is NoteType.TEXT_SELECTION:
            st.caption(f"文字选区笔记 #{note.id} · {status_label}")
            st.caption("原文（只读）")
            st.markdown(f"> {note.source_excerpt_snapshot}")
            st.caption("用户摘录")
            st.markdown(note.user_excerpt or "")
        elif note.note_type is NoteType.IMAGE_REGION:
            st.caption(f"图片区域笔记 #{note.id} · {status_label}")
            st.caption(
                f"区域：({note.region_x0}, {note.region_y0}) - "
                f"({note.region_x1}, {note.region_y1}) · "
                f"图像 {note.region_image_width}x{note.region_image_height}"
            )
        st.caption("个人笔记")
        st.markdown(note.personal_note)
        st.caption("该类型笔记的创建与修改将在后续版本开放。")


def _format_time(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _queue_key_clear(*keys: str) -> None:
    pending = st.session_state.setdefault(_PENDING_CLEAR_KEY, [])
    pending.extend(keys)


def _apply_pending_key_clears() -> None:
    for key in st.session_state.pop(_PENDING_CLEAR_KEY, []):
        st.session_state.pop(key, None)


def _show_load_error(exc: Exception) -> None:
    LOGGER.exception("读取结构化笔记失败")
    st.error(f"读取笔记失败：{exc}")


def _show_save_error(exc: Exception) -> None:
    LOGGER.exception("保存结构化笔记失败")
    if isinstance(
        exc,
        (
            NoteValidationError,
            NoteDocumentNotFoundError,
            NotePageNotFoundError,
            NoteNotFoundError,
        ),
    ):
        st.warning(str(exc))
    else:
        st.error(f"保存失败：{exc}")


def _show_delete_error(exc: Exception) -> None:
    LOGGER.exception("删除结构化笔记失败")
    if isinstance(exc, NoteNotFoundError):
        st.warning(str(exc))
    else:
        st.error(f"删除失败：{exc}")


__all__ = ["note_create_key", "render_structured_notes_tab"]
