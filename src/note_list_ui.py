"""Rendering helpers for the standalone「结构化笔记」list page.

Presentation, widget keys, form state and navigation only — every database
operation goes through ``NoteService``. The list page deliberately does not
re-implement the reader's rebind/re-frame flows; it links back to the source
page for those.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.models import NoteImportance, NoteListItem, NoteType, NoteView
from src.note_service import (
    NoteNotFoundError,
    NoteService,
    NoteValidationError,
)
from src.note_ui import (
    _load_display_preferences,
    _render_importance_badge,
    _render_region_overlay,
    _render_region_status,
    _render_source_status,
)

LOGGER = logging.getLogger(__name__)

_FLASH_KEY = "note_list_flash"
_PENDING_CLEAR_KEY = "note_list_pending_key_clear"
_PAGE_KEY = "note_list_page"
_SIZE_KEY = "note_list_page_size"
_FILTER_SIG_KEY = "note_list_filter_signature"
_PAGE_SIZES = (20, 50, 100)
_IMPORTANCE_LEVELS = list(NoteImportance)
_PREF_COLOR_KEYS = {
    NoteImportance.PRIMARY: "note_list_pref_color_primary",
    NoteImportance.SECONDARY: "note_list_pref_color_secondary",
    NoteImportance.NORMAL: "note_list_pref_color_normal",
}

_TYPE_DELETE_LABELS = {
    NoteType.DOCUMENT: "我确认删除这条文档级笔记",
    NoteType.PAGE: "我确认删除这条页面级笔记",
    NoteType.TEXT_SELECTION: "我确认删除这条文字选区笔记",
    NoteType.IMAGE_REGION: "我确认删除这条图片区域笔记",
}


def render_notes_list_page(note_service: NoteService) -> None:
    """Render filters, pagination and note cards for the list page."""

    flash = st.session_state.pop(_FLASH_KEY, "")
    if flash:
        st.success(flash)
    _apply_pending_key_clears()
    preferences = _load_display_preferences(note_service)

    try:
        document_options = note_service.list_note_document_options()
    except Exception as exc:
        LOGGER.exception("读取笔记文档列表失败")
        st.error(f"读取笔记失败：{exc}")
        return

    filter_columns = st.columns([2, 1, 1, 1])
    selected_document = filter_columns[0].selectbox(
        "文档筛选",
        options=[0, *(document_id for document_id, _ in document_options)],
        format_func=lambda value: (
            "全部文档"
            if value == 0
            else next(title for doc_id, title in document_options if doc_id == value)
        ),
        key="note_list_filter_document",
    )
    selected_type = filter_columns[1].selectbox(
        "类型筛选",
        options=["全部类型", *list(NoteType)],
        format_func=lambda value: value if isinstance(value, str) else value.label,
        key="note_list_filter_type",
    )
    selected_importance = filter_columns[2].selectbox(
        "等级筛选",
        options=["全部等级", *_IMPORTANCE_LEVELS],
        format_func=lambda value: value if isinstance(value, str) else value.label,
        key="note_list_filter_importance",
    )
    page_size = filter_columns[3].selectbox(
        "每页条数", options=list(_PAGE_SIZES), key=_SIZE_KEY
    )

    signature = (
        selected_document, str(selected_type), str(selected_importance), page_size
    )
    if st.session_state.get(_FILTER_SIG_KEY) != signature:
        st.session_state[_FILTER_SIG_KEY] = signature
        st.session_state[_PAGE_KEY] = 1

    document_id = selected_document or None
    note_type = selected_type if isinstance(selected_type, NoteType) else None
    importance = (
        selected_importance
        if isinstance(selected_importance, NoteImportance)
        else None
    )
    try:
        total = note_service.count_notes(
            document_id=document_id, note_type=note_type, importance=importance
        )
    except Exception as exc:
        LOGGER.exception("统计笔记失败")
        st.error(f"读取笔记失败：{exc}")
        return

    if total == 0:
        if document_id is None and note_type is None and importance is None:
            st.info("还没有结构化笔记。请先在阅读页创建。")
        else:
            st.info("当前筛选条件下没有结构化笔记。")
        _render_display_settings(note_service, preferences)
        return

    max_page = max(1, (total + page_size - 1) // page_size)
    current_page = int(st.session_state.get(_PAGE_KEY, 1))
    if current_page < 1:
        current_page = 1
    if current_page > max_page:
        current_page = max_page
    st.session_state[_PAGE_KEY] = current_page

    try:
        items = note_service.list_note_summaries(
            document_id=document_id,
            note_type=note_type,
            importance=importance,
            limit=page_size,
            offset=(current_page - 1) * page_size,
        )
    except Exception as exc:
        LOGGER.exception("读取笔记列表失败")
        st.error(f"读取笔记失败：{exc}")
        return

    first_index = (current_page - 1) * page_size + 1
    last_index = min(total, current_page * page_size)
    nav_columns = st.columns([1, 1, 3])
    if nav_columns[0].button("← 上一页", disabled=current_page <= 1, key="note_list_prev"):
        st.session_state[_PAGE_KEY] = current_page - 1
        st.rerun()
    if nav_columns[1].button(
        "下一页 →", disabled=current_page >= max_page, key="note_list_next"
    ):
        st.session_state[_PAGE_KEY] = current_page + 1
        st.rerun()
    nav_columns[2].caption(
        f"第 {current_page} / {max_page} 页 · 第 {first_index}–{last_index} 条 · 共 {total} 条"
    )

    image_cache: dict = {}
    for item in items:
        _render_note_card(note_service, item, image_cache, preferences)

    _render_display_settings(note_service, preferences)


def _render_display_settings(note_service: NoteService, preferences) -> None:
    """The single editable color entry (frozen): three badge backgrounds."""

    with st.expander("显示设置", expanded=False):
        st.caption("三级笔记徽章的背景色；文字颜色会自动适配，笔记内容不受影响。")
        color_columns = st.columns(3)
        picked = {
            NoteImportance.PRIMARY: color_columns[0].color_picker(
                "重点背景色",
                value=preferences.color_primary,
                key=_PREF_COLOR_KEYS[NoteImportance.PRIMARY],
            ),
            NoteImportance.SECONDARY: color_columns[1].color_picker(
                "次重点背景色",
                value=preferences.color_secondary,
                key=_PREF_COLOR_KEYS[NoteImportance.SECONDARY],
            ),
            NoteImportance.NORMAL: color_columns[2].color_picker(
                "一般背景色",
                value=preferences.color_normal,
                key=_PREF_COLOR_KEYS[NoteImportance.NORMAL],
            ),
        }
        if st.button("保存配色", key="note_list_pref_save"):
            try:
                # 一次 service 调用完成三级更新（Phase 2 原子契约）
                note_service.update_display_preferences(
                    picked[NoteImportance.PRIMARY],
                    picked[NoteImportance.SECONDARY],
                    picked[NoteImportance.NORMAL],
                )
            except Exception as exc:
                _show_save_error(exc)  # 输入保留在控件中，绝不假成功
            else:
                _queue_key_clear(*_PREF_COLOR_KEYS.values())
                st.session_state[_FLASH_KEY] = "配色已保存。"
                st.rerun()
        if st.button("恢复默认配色", key="note_list_pref_reset"):
            try:
                note_service.reset_display_preferences()
            except Exception as exc:
                _show_save_error(exc)
            else:
                _queue_key_clear(*_PREF_COLOR_KEYS.values())
                st.session_state[_FLASH_KEY] = "配色已恢复默认。"
                st.rerun()


def _render_note_card(
    note_service: NoteService, item: NoteListItem, image_cache: dict, preferences
) -> None:
    note = item.note
    with st.container(border=True):
        _render_importance_badge(note, preferences)
        location = (
            "来源文档不存在"
            if item.document_title is None
            else item.document_title
        )
        if note.note_type is not NoteType.DOCUMENT:
            location += (
                f" · 第 {item.page_number} 页"
                if item.page_number is not None
                else " · 来源页面不存在"
            )
        st.caption(
            f"{note.note_type.label} #{note.id} · {location} · "
            f"创建于 {_format_time(note.created_at)} · 更新于 {_format_time(note.updated_at)}"
        )
        if note.note_type is NoteType.TEXT_SELECTION:
            _render_text_selection_body(item)
        elif note.note_type is NoteType.IMAGE_REGION:
            _render_image_region_body(note_service, item, image_cache)
        else:
            st.caption("个人笔记")
            st.markdown(note.personal_note)

        action_columns = st.columns([1, 1, 2])
        source_missing = (
            item.document_id is None
            or (note.note_type is not NoteType.DOCUMENT and item.page_number is None)
        )
        if action_columns[0].button(
            "返回来源",
            key=f"note_list_source_{note.id}",
            disabled=source_missing,
        ):
            _open_source(item)
        if action_columns[1].button("编辑", key=f"note_list_edit_open_{note.id}"):
            st.session_state[f"note_list_edit_mode_{note.id}"] = True
            st.rerun()
        if st.session_state.get(f"note_list_edit_mode_{note.id}"):
            _render_edit_area(note_service, item)
        _render_delete_area(note_service, item)


def _render_text_selection_body(item: NoteListItem) -> None:
    note = item.note
    st.caption("原文快照（只读）")
    st.markdown(f"> {note.source_excerpt_snapshot}")
    st.caption(
        f"来源：{'PDF 文本层' if note.source_kind == 'pdf_text' else 'OCR 初稿'} · "
        f"原文位置：{note.selection_start} – {note.selection_end}"
    )
    _render_source_status(NoteView(note=note, source_status=item.source_status))
    st.caption("用户摘录")
    st.markdown(note.user_excerpt or "")
    st.caption("个人笔记")
    st.markdown(note.personal_note)


def _render_image_region_body(
    note_service: NoteService, item: NoteListItem, image_cache: dict
) -> None:
    note = item.note
    st.caption(
        f"区域：({note.region_x0}, {note.region_y0}) - "
        f"({note.region_x1}, {note.region_y1}) · 区域 "
        f"{note.region_x1 - note.region_x0} × {note.region_y1 - note.region_y0} 像素 · "
        f"创建时图像 {note.region_image_width} × {note.region_image_height}"
    )
    st.caption("个人笔记")
    st.markdown(note.personal_note)
    preview_key = f"note_list_preview_show_{note.id}"
    if st.session_state.get(preview_key):
        try:
            view = note_service.get_note(note.id, image_cache=image_cache)
            preview = note_service.get_image_region_source_preview(
                note.page_id, image_cache=image_cache
            )
        except Exception as exc:
            st.warning(f"无法生成区域预览：{exc}")
            return
        _render_region_status(view)
        _render_region_overlay(
            preview.path,
            {
                "x0": note.region_x0,
                "y0": note.region_y0,
                "x1": note.region_x1,
                "y1": note.region_y1,
            },
        )
        if st.button("隐藏区域预览", key=f"note_list_preview_hide_{note.id}"):
            st.session_state.pop(preview_key, None)
            st.rerun()
    elif st.button("显示区域预览", key=f"note_list_preview_show_btn_{note.id}"):
        st.session_state[preview_key] = True
        st.rerun()


def _render_edit_area(note_service: NoteService, item: NoteListItem) -> None:
    note = item.note
    if note.note_type is NoteType.TEXT_SELECTION:
        excerpt_draft = st.text_area(
            "用户摘录",
            value=note.user_excerpt or "",
            height=100,
            key=f"note_list_edit_excerpt_{note.id}",
        )
    else:
        excerpt_draft = None
    personal_draft = st.text_area(
        "个人笔记",
        value=note.personal_note,
        height=120,
        key=f"note_list_edit_personal_{note.id}",
    )
    dirty = personal_draft != note.personal_note or (
        excerpt_draft is not None and excerpt_draft != (note.user_excerpt or "")
    )
    if dirty:
        st.warning("● 有未保存修改")
    save_column, cancel_column = st.columns(2)
    if save_column.button("保存修改", key=f"note_list_edit_save_{note.id}"):
        if not personal_draft.strip():
            st.warning("个人笔记不能为空")
            return
        try:
            if note.note_type is NoteType.DOCUMENT:
                note_service.update_document_note(note.id, personal_draft)
            elif note.note_type is NoteType.PAGE:
                note_service.update_page_note(note.id, personal_draft)
            elif note.note_type is NoteType.TEXT_SELECTION:
                if not (excerpt_draft or "").strip():
                    st.warning("用户摘录不能为空")
                    return
                note_service.update_text_selection_content(
                    note.id,
                    user_excerpt=excerpt_draft,
                    personal_note=personal_draft,
                )
            else:
                note_service.update_image_region_note(note.id, personal_draft)
        except Exception as exc:
            _show_save_error(exc)
        else:
            _queue_key_clear(
                f"note_list_edit_mode_{note.id}",
                f"note_list_edit_excerpt_{note.id}",
                f"note_list_edit_personal_{note.id}",
            )
            st.session_state[_FLASH_KEY] = "笔记已保存。"
            st.rerun()
    if cancel_column.button("取消编辑", key=f"note_list_edit_cancel_{note.id}"):
        _queue_key_clear(
            f"note_list_edit_mode_{note.id}",
            f"note_list_edit_excerpt_{note.id}",
            f"note_list_edit_personal_{note.id}",
        )
        st.rerun()


def _render_delete_area(note_service: NoteService, item: NoteListItem) -> None:
    note = item.note
    with st.expander("删除这条笔记", expanded=False):
        confirmed = st.checkbox(
            _TYPE_DELETE_LABELS[note.note_type],
            key=f"note_list_delete_confirm_{note.id}",
        )
        if st.button(
            "永久删除这条笔记",
            key=f"note_list_delete_{note.id}",
            disabled=not confirmed,
        ):
            try:
                note_service.delete_note(note.id)
            except Exception as exc:
                _show_delete_error(exc)
            else:
                _queue_key_clear(
                    f"note_list_edit_mode_{note.id}",
                    f"note_list_edit_excerpt_{note.id}",
                    f"note_list_edit_personal_{note.id}",
                    f"note_list_delete_confirm_{note.id}",
                    f"note_list_preview_show_{note.id}",
                )
                st.session_state[_FLASH_KEY] = "笔记已删除。"
                st.rerun()


def _open_source(item: NoteListItem) -> None:
    page_number = item.page_number if item.page_number is not None else 1
    source_params = {
        "document": str(item.document_id),
        "page": str(page_number),
        "from_search": "0",
    }
    # 与证据篮一致的一次性移交；阅读页会重新校验参数。
    st.session_state["pending_reader_query_params"] = source_params
    st.query_params.clear()
    st.query_params.update(source_params)
    st.switch_page("pages/3_浏览资料.py")


def _format_time(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _queue_key_clear(*keys: str) -> None:
    pending = st.session_state.setdefault(_PENDING_CLEAR_KEY, [])
    pending.extend(keys)


def _apply_pending_key_clears() -> None:
    for key in st.session_state.pop(_PENDING_CLEAR_KEY, []):
        st.session_state.pop(key, None)


def _show_save_error(exc: Exception) -> None:
    LOGGER.exception("保存笔记失败")
    if isinstance(exc, (NoteValidationError, NoteNotFoundError)):
        st.warning(str(exc))
    else:
        st.error(f"保存失败：{exc}")


def _show_delete_error(exc: Exception) -> None:
    LOGGER.exception("删除笔记失败")
    if isinstance(exc, NoteNotFoundError):
        st.warning(str(exc))
    else:
        st.error(f"删除失败：{exc}")


__all__ = ["render_notes_list_page"]
