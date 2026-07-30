"""Streamlit rendering helpers for the reader's structured-notes tab.

This module only handles presentation, widget keys, form state and error
display. Every database operation goes through ``NoteService`` — no SQL here.
v0.3.0 offers full CRUD for document-level, page-level and text-selection
notes; image-region notes stay read-only until their dedicated interaction.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.models import NoteSourceStatus, NoteType, NoteView
from src.note_service import (
    DuplicateExcerptError,
    ExcerptNotFoundError,
    NoteDocumentNotFoundError,
    NoteNotFoundError,
    NotePageNotFoundError,
    NoteService,
    NoteValidationError,
    TextSourceUnavailableError,
)

LOGGER = logging.getLogger(__name__)

_FLASH_KEY = "note_tab_flash"
_PENDING_CLEAR_KEY = "note_tab_pending_key_clear"
MAX_NOTE_TEXT = 20000

_SOURCE_KIND_LABELS = {
    "pdf_text": "来源：PDF 文本层",
    "ocr_text": "来源：OCR 初稿",
}
_REBIND_CONFIRM_TEXT = (
    "确认重新绑定原文选区：当前原文锚点和用户摘录将替换为新选区，"
    "个人笔记内容会保留。"
)


def note_create_key(scope: str, owner_id: int) -> str:
    """Build the create-form widget key (type + owning id + create mode)."""

    return f"note_create_{scope}_{owner_id}"


def _source_kind_label(source_kind: str | None) -> str:
    return _SOURCE_KIND_LABELS.get(source_kind or "", "来源：未知")


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
    text_views = [
        view for view in views if view.note.note_type is NoteType.TEXT_SELECTION
    ]
    region_views = [
        view for view in views if view.note.note_type is NoteType.IMAGE_REGION
    ]
    if not page_views and not text_views and not region_views:
        st.caption("当前页面还没有页面级笔记。")
    for view in page_views:
        _render_editable_note(
            view,
            save=lambda note_id, text: note_service.update_page_note(note_id, text),
            delete=note_service.delete_note,
            delete_label="我确认删除这条页面级笔记",
            delete_flash="页面级笔记已删除。",
        )
    for view in text_views:
        _render_text_selection_note(note_service, view)
    for view in region_views:
        _render_readonly_anchor_note(view)
    _render_create_form(
        scope="page",
        owner_id=page_id,
        title="新建页面级笔记",
        create=lambda text: note_service.create_page_note(page_id, text),
        flash="页面级笔记已保存。",
    )
    _render_text_selection_create(note_service, page_id)


def _render_create_form(*, scope: str, owner_id: int, title: str, create, flash: str) -> None:
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
        _render_delete_area(note.id, delete, delete_label, delete_flash)


def _render_delete_area(note_id: int, delete, confirm_label: str, delete_flash: str) -> None:
    with st.expander("删除这条笔记", expanded=False):
        confirmed = st.checkbox(confirm_label, key=_delete_confirm_key(note_id))
        if st.button(
            "永久删除这条笔记", key=f"note_delete_{note_id}", disabled=not confirmed
        ):
            try:
                delete(note_id)
            except Exception as exc:
                _show_delete_error(exc)
            else:
                _queue_key_clear(
                    _edit_mode_key(note_id),
                    _edit_input_key(note_id),
                    _delete_confirm_key(note_id),
                    f"note_text_edit_mode_{note_id}",
                    f"note_text_edit_excerpt_{note_id}",
                    f"note_text_edit_personal_{note_id}",
                    f"note_text_rebind_input_{note_id}",
                    f"note_text_rebind_preview_{note_id}",
                    f"note_text_rebind_confirm_{note_id}",
                )
                st.session_state[_FLASH_KEY] = delete_flash
                st.rerun()


# ------------------------------------------------------------- text selection


def _render_text_selection_create(note_service: NoteService, page_id: int) -> None:
    with st.expander("新建文字选区笔记", expanded=False):
        try:
            preview = note_service.get_text_selection_source_preview(page_id)
        except TextSourceUnavailableError:
            st.warning("当前页面没有可用文字来源，请使用图片区域笔记。")
            return
        except Exception as exc:
            _show_load_error(exc)
            return
        st.caption(preview.label)
        st.caption(
            "从下方来源文本中复制一段原文作为锚点；系统只接受唯一完全匹配，"
            "找不到或出现多次都无法创建。"
        )
        st.code(preview.source_text, language=None)
        excerpt = st.text_area(
            "原文选段",
            height=100,
            key=f"note_text_create_excerpt_{page_id}",
            placeholder="粘贴来源文本中的一段精确原文（含原有空格与换行）。",
        )
        user_excerpt = st.text_area(
            "用户摘录（可选）",
            height=100,
            key=f"note_text_create_user_{page_id}",
            placeholder="留空时自动等于原文快照；可在此整理自己的摘录。",
        )
        personal_note = st.text_area(
            "个人笔记",
            height=120,
            key=f"note_text_create_personal_{page_id}",
            placeholder="记录你的判断、说明和想法。",
        )
        if st.button("保存文字选区笔记", key=f"note_text_create_save_{page_id}"):
            if not excerpt.strip():
                st.warning("原文选段不能为空")
                return
            if not personal_note.strip():
                st.warning("个人笔记不能为空")
                return
            try:
                note_service.create_text_selection_note(
                    page_id,
                    excerpt,
                    personal_note,
                    user_excerpt=user_excerpt if user_excerpt.strip() else None,
                )
            except ExcerptNotFoundError:
                st.warning("没有在当前文字来源中找到这段原文，请检查空格、换行和标点。")
            except DuplicateExcerptError:
                st.warning(
                    "这段原文在当前文字来源中出现多次，无法确定唯一位置。"
                    "请扩大选区后重试。"
                )
            except TextSourceUnavailableError:
                st.warning("当前页面没有可用文字来源，请使用图片区域笔记。")
            except Exception as exc:
                _show_save_error(exc)  # 全部输入保留
            else:
                _queue_key_clear(
                    f"note_text_create_excerpt_{page_id}",
                    f"note_text_create_user_{page_id}",
                    f"note_text_create_personal_{page_id}",
                )
                st.session_state[_FLASH_KEY] = "文字选区笔记已保存。"
                st.rerun()


def _render_text_selection_note(note_service: NoteService, view: NoteView) -> None:
    note = view.note
    with st.container(border=True):
        st.caption(
            f"文字选区笔记 #{note.id} · {_source_kind_label(note.source_kind)} · "
            f"创建于 {_format_time(note.created_at)} · 更新于 {_format_time(note.updated_at)}"
        )
        _render_source_status(view)
        st.caption(
            f"原文位置：{note.selection_start} – {note.selection_end}"
        )
        st.caption("原文快照（只读）")
        st.markdown(f"> {note.source_excerpt_snapshot}")
        if st.session_state.get(f"note_text_edit_mode_{note.id}"):
            _render_text_selection_edit(note_service, view)
        else:
            st.caption("用户摘录")
            st.markdown(note.user_excerpt or "")
            st.caption("个人笔记")
            st.markdown(note.personal_note)
            if st.button("编辑", key=f"note_text_edit_open_{note.id}"):
                st.session_state[f"note_text_edit_mode_{note.id}"] = True
                st.rerun()
        _render_rebind_area(note_service, view)
        _render_delete_area(
            note.id,
            note_service.delete_note,
            "我确认删除这条文字选区笔记",
            "文字选区笔记已删除。",
        )


def _render_source_status(view: NoteView) -> None:
    status = view.source_status
    if status is NoteSourceStatus.VALID:
        st.caption("来源有效")
    elif status is NoteSourceStatus.CHANGED:
        st.warning("当前页面文字已变化，原选区无法自动重新定位。")
    elif status is NoteSourceStatus.MISSING:
        st.warning("原绑定的文字来源已经不存在。")
    elif status in (NoteSourceStatus.UNAVAILABLE, NoteSourceStatus.UNREADABLE):
        st.error("暂时无法读取原绑定的文字来源。")


def _render_text_selection_edit(note_service: NoteService, view: NoteView) -> None:
    note = view.note
    excerpt_draft = st.text_area(
        "用户摘录",
        value=note.user_excerpt or "",
        height=100,
        key=f"note_text_edit_excerpt_{note.id}",
    )
    personal_draft = st.text_area(
        "个人笔记",
        value=note.personal_note,
        height=120,
        key=f"note_text_edit_personal_{note.id}",
    )
    if excerpt_draft != (note.user_excerpt or "") or personal_draft != note.personal_note:
        st.warning("● 有未保存修改")
    save_column, cancel_column = st.columns(2)
    if save_column.button("保存修改", key=f"note_text_edit_save_{note.id}"):
        if not excerpt_draft.strip():
            st.warning("用户摘录不能为空")
            return
        if not personal_draft.strip():
            st.warning("个人笔记不能为空")
            return
        try:
            note_service.update_text_selection_content(
                note.id,
                user_excerpt=excerpt_draft,
                personal_note=personal_draft,
            )
        except Exception as exc:
            _show_save_error(exc)  # 输入保留
        else:
            _queue_key_clear(
                f"note_text_edit_mode_{note.id}",
                f"note_text_edit_excerpt_{note.id}",
                f"note_text_edit_personal_{note.id}",
            )
            st.session_state[_FLASH_KEY] = "笔记已保存。"
            st.rerun()
    if cancel_column.button("取消编辑", key=f"note_text_edit_cancel_{note.id}"):
        _queue_key_clear(
            f"note_text_edit_mode_{note.id}",
            f"note_text_edit_excerpt_{note.id}",
            f"note_text_edit_personal_{note.id}",
        )
        st.rerun()


def _render_rebind_area(note_service: NoteService, view: NoteView) -> None:
    note = view.note
    with st.expander("重新绑定原文选区", expanded=False):
        draft = st.text_area(
            "新的原文选段",
            height=100,
            key=f"note_text_rebind_input_{note.id}",
            placeholder="粘贴当前文字来源中的一段新的精确原文。",
        )
        if st.button("预览重新绑定", key=f"note_text_rebind_preview_btn_{note.id}"):
            if not draft.strip():
                st.warning("新的原文选段不能为空")
                return
            try:
                preview = note_service.preview_text_selection_rebind(note.id, draft)
            except ExcerptNotFoundError:
                st.warning("没有在当前文字来源中找到这段原文，请检查空格、换行和标点。")
            except DuplicateExcerptError:
                st.warning(
                    "这段原文在当前文字来源中出现多次，无法确定唯一位置。"
                    "请扩大选区后重试。"
                )
            except TextSourceUnavailableError:
                st.warning("当前页面没有可用文字来源，请使用图片区域笔记。")
            except Exception as exc:
                _show_save_error(exc)
            else:
                st.session_state[f"note_text_rebind_preview_{note.id}"] = preview
                st.rerun()
        preview = st.session_state.get(f"note_text_rebind_preview_{note.id}")
        if preview:
            st.info(
                f"旧{_source_kind_label(preview['old_source_kind'])}："
                f"{preview['old_snapshot']}\n\n"
                f"新{_source_kind_label(preview['new_source_kind'])}："
                f"{preview['new_snapshot']}\n\n"
                f"新位置：{preview['selection_start']} – {preview['selection_end']}\n\n"
                "确认后用户摘录将重置为新原文，个人笔记保留。"
            )
            confirmed = st.checkbox(
                _REBIND_CONFIRM_TEXT, key=f"note_text_rebind_confirm_{note.id}"
            )
            if st.button(
                "确认重新绑定",
                key=f"note_text_rebind_apply_{note.id}",
                disabled=not confirmed,
            ):
                try:
                    note_service.rebind_text_selection(note.id, draft)
                except Exception as exc:
                    _show_save_error(exc)  # 预览与输入保留
                else:
                    _queue_key_clear(
                        f"note_text_rebind_input_{note.id}",
                        f"note_text_rebind_preview_{note.id}",
                        f"note_text_rebind_confirm_{note.id}",
                    )
                    st.session_state[_FLASH_KEY] = "已重新绑定原文选区。"
                    st.rerun()


def _render_readonly_anchor_note(view: NoteView) -> None:
    note = view.note
    status_label = view.source_status.label if view.source_status else ""
    with st.container(border=True):
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
