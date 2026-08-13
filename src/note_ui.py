"""Streamlit rendering helpers for the reader's structured-notes tab.

This module only handles presentation, widget keys, form state and error
display. Every database operation goes through ``NoteService`` — no SQL here.
v0.3.0 offers full CRUD for document-level, page-level and text-selection
notes; image-region notes stay read-only until their dedicated interaction.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw
from streamlit_image_coordinates import streamlit_image_coordinates

from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
    EvidenceBasketService,
)
from src.models import (
    Note,
    NoteDisplayPreferences,
    NoteImportance,
    NoteSourceStatus,
    NoteType,
    NoteView,
)
from src.note_geometry import (
    display_to_original,
    make_component_key,
    normalize_original_rect,
)
from src.note_service import (
    DuplicateExcerptError,
    ExcerptNotFoundError,
    InvalidImageRegionError,
    NoteDocumentNotFoundError,
    NoteNotFoundError,
    NotePageNotFoundError,
    NoteService,
    NoteValidationError,
    PageImageMissingError,
    PageImageUnreadableError,
    TextSourceUnavailableError,
    badge_foreground,
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
_IMPORTANCE_LEVELS = list(NoteImportance)
_IMPORTANCE_BADGE_BACKGROUNDS = {
    NoteImportance.PRIMARY: "color_primary",
    NoteImportance.SECONDARY: "color_secondary",
    NoteImportance.NORMAL: "color_normal",
}


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


def _importance_of(note: Note) -> NoteImportance | None:
    """Strict semantic mapping; unknown DB values surface as an error, never as normal."""

    try:
        return NoteImportance(note.importance)
    except ValueError:
        LOGGER.error("笔记 %s 的重要程度数据异常：%r", note.id, note.importance)
        return None


def _importance_index(level: NoteImportance | None) -> int:
    return _IMPORTANCE_LEVELS.index(level) if level is not None else 2


def _render_importance_badge(note: Note, preferences: NoteDisplayPreferences) -> None:
    """Render the semantic badge; text label is always present.

    Color is presentation only: background comes from display preferences,
    foreground from the shared ``badge_foreground`` helper. Color anomalies
    degrade to a plain-text badge; unknown importance is an explicit error,
    never silently rendered as 一般.
    """

    level = _importance_of(note)
    if level is None:
        st.error(f"笔记 #{note.id} 的重要程度数据异常：{note.importance!r}")
        return
    background = getattr(preferences, _IMPORTANCE_BADGE_BACKGROUNDS[level])
    try:
        foreground = badge_foreground(background)
    except Exception:
        LOGGER.exception("徽章配色计算失败：%s", background)
        st.markdown(f"**{level.label}**")
        return
    st.markdown(
        f"<span style='background:{background};color:{foreground};"
        "padding:0.1rem 0.45rem;border-radius:0.35rem;font-weight:600'>"
        f"{level.label}</span>",
        unsafe_allow_html=True,
    )


def _load_display_preferences(note_service: NoteService) -> NoteDisplayPreferences:
    """Read badge preferences once per render; failures degrade, never crash."""

    try:
        return note_service.get_display_preferences()
    except Exception:
        LOGGER.exception("读取笔记显示偏好失败")
        st.warning("配色偏好暂时读取失败，本次按默认配色显示。")
        return NoteDisplayPreferences()


def _render_importance_selector(note: Note, key: str) -> NoteImportance:
    """Edit-form level selector initialized from the stored value."""

    current = _importance_of(note)
    return st.selectbox(
        "重要程度",
        options=_IMPORTANCE_LEVELS,
        index=_importance_index(current),
        format_func=lambda item: item.label,
        key=key,
    )


def render_structured_notes_tab(
    note_service: NoteService,
    *,
    document_id: int,
    page_id: int,
    display_width: int,
    basket_service: EvidenceBasketService | None = None,
) -> None:
    """Render the whole「结构化笔记」tab: document notes + current-page notes.

    ``basket_service`` is optional: pages that pass it get a「加入证据篮」
    action on anchored notes; pages that omit it render notes exactly as
    before.
    """

    flash = st.session_state.pop(_FLASH_KEY, "")
    if flash:
        st.success(flash)
    _apply_pending_key_clears()
    preferences = _load_display_preferences(note_service)
    _render_document_section(note_service, document_id, preferences)
    st.divider()
    _render_page_section(
        note_service, document_id, page_id, display_width, preferences, basket_service
    )


def _render_document_section(
    note_service: NoteService, document_id: int, preferences: NoteDisplayPreferences
) -> None:
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
                preferences,
                save=lambda note_id, text, imp: note_service.update_document_note(
                    note_id, text, importance=imp
                ),
                delete=note_service.delete_note,
                delete_label="我确认删除这条文档级笔记",
                delete_flash="文档级笔记已删除。",
            )
        _render_create_form(
            scope="document",
            owner_id=document_id,
            title="新建文档级笔记",
            create=lambda text, level: note_service.create_document_note(
                document_id, text, importance=level
            ),
            flash="文档级笔记已保存。",
        )


def _render_page_section(
    note_service: NoteService,
    document_id: int,
    page_id: int,
    display_width: int,
    preferences: NoteDisplayPreferences,
    basket_service: EvidenceBasketService | None,
) -> None:
    st.subheader("本页笔记")
    image_cache: dict = {}
    try:
        views = note_service.list_page_notes(page_id, image_cache=image_cache)
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
            preferences,
            save=lambda note_id, text, imp: note_service.update_page_note(
                note_id, text, importance=imp
            ),
            delete=note_service.delete_note,
            delete_label="我确认删除这条页面级笔记",
            delete_flash="页面级笔记已删除。",
        )
    for view in text_views:
        _render_text_selection_note(
            note_service,
            view,
            preferences,
            document_id=document_id,
            basket_service=basket_service,
        )
    for view in region_views:
        _render_image_region_note(
            note_service, view, document_id, display_width, preferences, basket_service
        )
    _render_create_form(
        scope="page",
        owner_id=page_id,
        title="新建页面级笔记",
        create=lambda text, level: note_service.create_page_note(
            page_id, text, importance=level
        ),
        flash="页面级笔记已保存。",
        save_button_type="primary",
    )
    _render_text_selection_create(note_service, page_id)
    _render_image_region_create(note_service, document_id, page_id, display_width)


def _render_create_form(
    *, scope: str, owner_id: int, title: str, create, flash: str,
    save_button_type: str = "secondary",
) -> None:
    key = note_create_key(scope, owner_id)
    text = st.text_area(
        f"{title}（最多 {MAX_NOTE_TEXT} 字符）",
        height=140,
        key=key,
        placeholder="记录你的判断、说明和想法。",
    )
    level = st.selectbox(
        "重要程度",
        options=_IMPORTANCE_LEVELS,
        index=2,
        format_func=lambda item: item.label,
        key=f"{key}_imp",
    )
    if st.button(
        f"保存{title.removeprefix('新建')}", key=f"{key}_save", type=save_button_type
    ):
        if not text.strip():
            st.warning("个人笔记不能为空")
            return
        try:
            create(text, level.value)
        except Exception as exc:
            _show_save_error(exc)  # 输入保留在 widget 中
        else:
            _queue_key_clear(key, f"{key}_imp")
            st.session_state[_FLASH_KEY] = flash
            st.rerun()


def _render_editable_note(
    view: NoteView,
    preferences: NoteDisplayPreferences,
    *,
    save,
    delete,
    delete_label: str,
    delete_flash: str,
) -> None:
    note = view.note
    with st.container(border=True):
        _render_importance_badge(note, preferences)
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
            current_level = _importance_of(note)
            selected_level = _render_importance_selector(note, f"note_edit_imp_{note.id}")
            dirty = draft != note.personal_note or selected_level != current_level
            if dirty:
                st.warning("● 有未保存修改")
            save_column, cancel_column = st.columns(2)
            if save_column.button("保存修改", key=f"note_edit_save_{note.id}"):
                if not draft.strip():
                    st.warning("个人笔记不能为空")
                    return
                try:
                    # 等级未变 → None = preserve（冻结契约）；变了 → 显式语义值
                    save(
                        note.id,
                        draft,
                        selected_level.value if selected_level != current_level else None,
                    )
                except Exception as exc:
                    _show_save_error(exc)  # 输入保留
                else:
                    _queue_key_clear(
                        _edit_mode_key(note.id),
                        _edit_input_key(note.id),
                        f"note_edit_imp_{note.id}",
                    )
                    st.session_state[_FLASH_KEY] = "笔记已保存。"
                    st.rerun()
            if cancel_column.button("取消编辑", key=f"note_edit_cancel_{note.id}"):
                _queue_key_clear(
                    _edit_mode_key(note.id),
                    _edit_input_key(note.id),
                    f"note_edit_imp_{note.id}",
                )
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
                    f"note_edit_imp_{note_id}",
                    _delete_confirm_key(note_id),
                    f"note_text_edit_mode_{note_id}",
                    f"note_text_edit_excerpt_{note_id}",
                    f"note_text_edit_personal_{note_id}",
                    f"note_text_edit_imp_{note_id}",
                    f"note_text_rebind_input_{note_id}",
                    f"note_text_rebind_preview_{note_id}",
                    f"note_text_rebind_confirm_{note_id}",
                    f"note_image_edit_mode_{note_id}",
                    f"note_image_edit_personal_{note_id}",
                    f"note_image_edit_imp_{note_id}",
                    f"note_image_rebind_active_{note_id}",
                    f"note_image_rebind_region_{note_id}",
                    f"note_image_rebind_confirm_{note_id}",
                    f"note_image_anchor_version_rebind_{note_id}",
                    f"note_image_preview_toggle_{note_id}",
                )
                st.session_state[_FLASH_KEY] = delete_flash
                st.rerun()


def _render_add_to_basket_button(
    basket_service: EvidenceBasketService, *, document_id: int, note: Note
) -> None:
    """Render the one-click「加入证据篮」action for one anchored note."""

    if st.button("加入证据篮", key=f"note_add_basket_{note.id}"):
        try:
            _add_note_to_basket(basket_service, document_id=document_id, note=note)
        except DuplicateEvidenceError as exc:
            st.info(str(exc))
        except EvidenceBasketError as exc:
            st.error(f"加入证据篮失败：{exc}")
        except Exception as exc:
            LOGGER.exception("笔记加入证据篮失败：note_id=%s", note.id)
            st.error(f"加入证据篮失败：{exc}")
        else:
            st.session_state[_FLASH_KEY] = "已加入证据篮，证据锚点与笔记锚点一致。"
            st.rerun()


def _add_note_to_basket(
    basket_service: EvidenceBasketService, *, document_id: int, note: Note
) -> None:
    """Persist the note's existing anchor as evidence in the default basket.

    The anchor is never re-derived here: a text-selection note contributes its
    stored excerpt (same normalized SHA-256 semantics as the note anchor), an
    image-region note contributes its stored original-pixel coordinates.
    """

    if note.note_type is NoteType.TEXT_SELECTION:
        evidence_text = note.source_excerpt_snapshot or note.user_excerpt or ""
        basket_service.add_item(
            document_id=document_id,
            page_id=note.page_id,
            evidence_text=evidence_text,
            user_note=note.personal_note,
        )
        return
    if note.note_type is NoteType.IMAGE_REGION:
        basket_service.add_region_item(
            basket_service.default_basket().id,
            document_id,
            note.page_id,
            x0=note.region_x0,
            y0=note.region_y0,
            x1=note.region_x1,
            y1=note.region_y1,
            user_note=note.personal_note,
        )
        return
    raise EvidenceBasketError(f"笔记类型「{note.note_type.label}」不支持加入证据篮。")


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
        level = st.selectbox(
            "重要程度",
            options=_IMPORTANCE_LEVELS,
            index=2,
            format_func=lambda item: item.label,
            key=f"note_text_create_imp_{page_id}",
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
                    importance=level.value,
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
                    f"note_text_create_imp_{page_id}",
                )
                st.session_state[_FLASH_KEY] = "文字选区笔记已保存。"
                st.rerun()


def _render_text_selection_note(
    note_service: NoteService,
    view: NoteView,
    preferences: NoteDisplayPreferences,
    *,
    document_id: int,
    basket_service: EvidenceBasketService | None,
) -> None:
    note = view.note
    with st.container(border=True):
        _render_importance_badge(note, preferences)
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
        if basket_service is not None:
            _render_add_to_basket_button(
                basket_service, document_id=document_id, note=note
            )
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
    current_level = _importance_of(note)
    selected_level = _render_importance_selector(note, f"note_text_edit_imp_{note.id}")
    dirty = (
        excerpt_draft != (note.user_excerpt or "")
        or personal_draft != note.personal_note
        or selected_level != current_level
    )
    if dirty:
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
                importance=(
                    selected_level.value if selected_level != current_level else None
                ),
            )
        except Exception as exc:
            _show_save_error(exc)  # 输入保留
        else:
            _queue_key_clear(
                f"note_text_edit_mode_{note.id}",
                f"note_text_edit_excerpt_{note.id}",
                f"note_text_edit_personal_{note.id}",
                f"note_text_edit_imp_{note.id}",
            )
            st.session_state[_FLASH_KEY] = "笔记已保存。"
            st.rerun()
    if cancel_column.button("取消编辑", key=f"note_text_edit_cancel_{note.id}"):
        _queue_key_clear(
            f"note_text_edit_mode_{note.id}",
            f"note_text_edit_excerpt_{note.id}",
            f"note_text_edit_personal_{note.id}",
            f"note_text_edit_imp_{note.id}",
        )
        st.rerun()


def _render_rebind_area(note_service: NoteService, view: NoteView) -> None:
    note = view.note
    preview_key = f"note_text_rebind_preview_{note.id}"
    confirm_key = f"note_text_rebind_confirm_{note.id}"
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
                _discard_rebind_preview(preview_key, confirm_key)
                st.warning("没有在当前文字来源中找到这段原文，请检查空格、换行和标点。")
            except DuplicateExcerptError:
                _discard_rebind_preview(preview_key, confirm_key)
                st.warning(
                    "这段原文在当前文字来源中出现多次，无法确定唯一位置。"
                    "请扩大选区后重试。"
                )
            except TextSourceUnavailableError:
                _discard_rebind_preview(preview_key, confirm_key)
                st.warning("当前页面没有可用文字来源，请使用图片区域笔记。")
            except Exception as exc:
                _discard_rebind_preview(preview_key, confirm_key)
                _show_save_error(exc)
            else:
                # Bind the preview to the exact input it was generated from;
                # execution must match what the user last confirmed.
                st.session_state[preview_key] = {"input": draft, "preview": preview}
                st.rerun()
        bundle = st.session_state.get(preview_key)
        if bundle is not None and bundle.get("input") != draft:
            _discard_rebind_preview(preview_key, confirm_key)
            bundle = None
            st.warning("新的原文选段已修改，请先重新预览再确认。")
        if bundle:
            preview = bundle["preview"]
            st.info(
                f"旧{_source_kind_label(preview['old_source_kind'])}："
                f"{preview['old_snapshot']}\n\n"
                f"新{_source_kind_label(preview['new_source_kind'])}："
                f"{preview['new_snapshot']}\n\n"
                f"新位置：{preview['selection_start']} – {preview['selection_end']}\n\n"
                "确认后用户摘录将重置为新原文，个人笔记保留。"
            )
            confirmed = st.checkbox(_REBIND_CONFIRM_TEXT, key=confirm_key)
            if st.button(
                "确认重新绑定",
                key=f"note_text_rebind_apply_{note.id}",
                disabled=not confirmed,
            ):
                try:
                    note_service.rebind_text_selection(note.id, bundle["input"])
                except Exception as exc:
                    _show_save_error(exc)  # 预览与输入保留
                else:
                    _queue_key_clear(
                        f"note_text_rebind_input_{note.id}",
                        preview_key,
                        confirm_key,
                    )
                    st.session_state[_FLASH_KEY] = "已重新绑定原文选区。"
                    st.rerun()


def _discard_rebind_preview(preview_key: str, confirm_key: str) -> None:
    """Drop any stored preview and its confirmation before the widgets render."""

    st.session_state.pop(preview_key, None)
    st.session_state.pop(confirm_key, None)


# ------------------------------------------------------------- image region


def _render_image_region_create(
    note_service: NoteService, document_id: int, page_id: int, display_width: int
) -> None:
    with st.expander("新建图片区域笔记", expanded=False):
        try:
            preview = note_service.get_image_region_source_preview(page_id)
        except PageImageMissingError:
            st.warning("当前页面图像不存在，无法创建图片区域笔记。")
            return
        except PageImageUnreadableError:
            st.warning("当前页面图像无法读取，请检查页面文件完整性。")
            return
        except Exception as exc:
            _show_load_error(exc)
            return

        st.caption(
            f"原始 PNG 尺寸：{preview.width} × {preview.height} 像素，"
            "框选坐标以此为准（显示宽度不影响最终坐标）。"
        )
        version = st.session_state.get(f"note_image_anchor_version_create_{page_id}", 0)
        component_key = (
            make_component_key(
                document_id, page_id, mode="create_region", anchor_version=version
            )
            + f"_w{display_width}"
        )
        value = streamlit_image_coordinates(
            str(preview.path),
            width=display_width,
            key=component_key,
            click_and_drag=True,
            cursor="crosshair",
        )
        region_key = f"note_image_create_region_{page_id}"
        if value is not None:
            try:
                rect = display_to_original(
                    value.get("x1"),
                    value.get("y1"),
                    value.get("x2"),
                    value.get("y2"),
                    int(value["width"]),
                    int(value["height"]),
                    preview.width,
                    preview.height,
                )
            except (ValueError, TypeError, KeyError):
                st.warning("框选区域无效，请重新选择。")
            else:
                if st.session_state.get(region_key) != rect:
                    st.session_state[region_key] = rect

        region = st.session_state.get(region_key)
        if region:
            st.success(
                f"当前框选：({region['x0']}, {region['y0']}) - "
                f"({region['x1']}, {region['y1']}) · 区域 "
                f"{region['x1'] - region['x0']} × {region['y1'] - region['y0']} 像素"
            )
            _render_region_overlay(preview.path, region)
            if st.button("清除框选", key=f"note_image_create_clear_{page_id}"):
                _queue_key_clear(region_key)
                st.rerun()

        with st.expander("高级坐标输入（调试与故障兜底）", expanded=False):
            columns = st.columns(4)
            manual = {
                "x0": columns[0].number_input(
                    "x0", min_value=0, max_value=preview.width, value=0,
                    key=f"note_image_manual_x0_{page_id}",
                ),
                "y0": columns[1].number_input(
                    "y0", min_value=0, max_value=preview.height, value=0,
                    key=f"note_image_manual_y0_{page_id}",
                ),
                "x1": columns[2].number_input(
                    "x1", min_value=0, max_value=preview.width,
                    value=min(100, preview.width),
                    key=f"note_image_manual_x1_{page_id}",
                ),
                "y1": columns[3].number_input(
                    "y1", min_value=0, max_value=preview.height,
                    value=min(100, preview.height),
                    key=f"note_image_manual_y1_{page_id}",
                ),
            }
            if st.button("使用这组坐标", key=f"note_image_manual_apply_{page_id}"):
                try:
                    rect = normalize_original_rect(
                        manual["x0"], manual["y0"], manual["x1"], manual["y1"],
                        preview.width, preview.height,
                    )
                except ValueError:
                    st.warning("框选区域无效，请重新选择。")
                else:
                    st.session_state[region_key] = rect
                    st.rerun()

        personal = st.text_area(
            "个人笔记",
            height=120,
            key=f"note_image_create_personal_{page_id}",
            placeholder="记录你对这个区域的判断、说明和想法。",
        )
        level = st.selectbox(
            "重要程度",
            options=_IMPORTANCE_LEVELS,
            index=2,
            format_func=lambda item: item.label,
            key=f"note_image_create_imp_{page_id}",
        )
        if st.button("保存图片区域笔记", key=f"note_image_create_save_{page_id}"):
            if not region:
                st.warning("请先在页面图像上拖拽选择一个区域。")
                return
            if not personal.strip():
                st.warning("个人笔记不能为空")
                return
            try:
                note_service.create_image_region_note(
                    page_id,
                    region["x0"],
                    region["y0"],
                    region["x1"],
                    region["y1"],
                    personal,
                    importance=level.value,
                )
            except PageImageMissingError:
                st.warning("当前页面图像不存在，无法创建图片区域笔记。")
            except PageImageUnreadableError:
                st.warning("当前页面图像无法读取，请检查页面文件完整性。")
            except InvalidImageRegionError:
                st.warning("框选区域无效，请重新选择。")
            except Exception as exc:
                _show_save_error(exc)  # 框选与个人笔记均保留
            else:
                st.session_state[f"note_image_anchor_version_create_{page_id}"] = (
                    version + 1
                )
                _queue_key_clear(
                    region_key,
                    f"note_image_create_personal_{page_id}",
                    f"note_image_create_imp_{page_id}",
                    f"note_image_manual_x0_{page_id}",
                    f"note_image_manual_y0_{page_id}",
                    f"note_image_manual_x1_{page_id}",
                    f"note_image_manual_y1_{page_id}",
                )
                st.session_state[_FLASH_KEY] = "图片区域笔记已保存。"
                st.rerun()


def _render_image_region_note(
    note_service: NoteService,
    view: NoteView,
    document_id: int,
    display_width: int,
    preferences: NoteDisplayPreferences,
    basket_service: EvidenceBasketService | None,
) -> None:
    note = view.note
    with st.container(border=True):
        _render_importance_badge(note, preferences)
        st.caption(
            f"图片区域笔记 #{note.id} · "
            f"创建于 {_format_time(note.created_at)} · 更新于 {_format_time(note.updated_at)}"
        )
        _render_region_status(view)
        st.caption(
            f"区域：({note.region_x0}, {note.region_y0}) - "
            f"({note.region_x1}, {note.region_y1}) · 区域 "
            f"{note.region_x1 - note.region_x0} × {note.region_y1 - note.region_y0} 像素 · "
            f"创建时图像 {note.region_image_width} × {note.region_image_height}"
        )
        with st.expander("区域预览", expanded=False):
            if st.checkbox("生成区域预览", key=f"note_image_preview_toggle_{note.id}"):
                try:
                    preview = note_service.get_image_region_source_preview(note.page_id)
                except Exception:
                    st.caption("当前页面图像不可用，无法生成预览。")
                else:
                    _render_region_overlay(
                        preview.path,
                        {
                            "x0": note.region_x0,
                            "y0": note.region_y0,
                            "x1": note.region_x1,
                            "y1": note.region_y1,
                        },
                    )
        if st.session_state.get(f"note_image_edit_mode_{note.id}"):
            _render_image_region_edit(note_service, view)
        else:
            st.caption("个人笔记")
            st.markdown(note.personal_note)
            if st.button("编辑", key=f"note_image_edit_open_{note.id}"):
                st.session_state[f"note_image_edit_mode_{note.id}"] = True
                st.rerun()
        _render_image_rebind_area(note_service, view, document_id, display_width)
        if basket_service is not None:
            _render_add_to_basket_button(
                basket_service, document_id=document_id, note=note
            )
        _render_delete_area(
            note.id,
            note_service.delete_note,
            "我确认删除这条图片区域笔记",
            "图片区域笔记已删除。",
        )


def _render_region_status(view: NoteView) -> None:
    status = view.source_status
    if status is NoteSourceStatus.VALID:
        st.caption("图片来源有效")
    elif status is NoteSourceStatus.CHANGED:
        st.warning("当前页面图像已经变化，原区域可能无法准确对应。")
    elif status is NoteSourceStatus.MISSING:
        st.warning("原绑定的页面图像已经不存在。")
    elif status is NoteSourceStatus.UNREADABLE:
        st.error("原绑定的页面图像无法读取。")
    elif status is NoteSourceStatus.UNAVAILABLE:
        st.error("暂时无法检查原绑定的页面图像。")


def _render_image_region_edit(note_service: NoteService, view: NoteView) -> None:
    note = view.note
    draft = st.text_area(
        "个人笔记",
        value=note.personal_note,
        height=120,
        key=f"note_image_edit_personal_{note.id}",
    )
    current_level = _importance_of(note)
    selected_level = _render_importance_selector(note, f"note_image_edit_imp_{note.id}")
    dirty = draft != note.personal_note or selected_level != current_level
    if dirty:
        st.warning("● 有未保存修改")
    save_column, cancel_column = st.columns(2)
    if save_column.button("保存修改", key=f"note_image_edit_save_{note.id}"):
        if not draft.strip():
            st.warning("个人笔记不能为空")
            return
        try:
            note_service.update_image_region_note(
                note.id,
                draft,
                importance=(
                    selected_level.value if selected_level != current_level else None
                ),
            )
        except Exception as exc:
            _show_save_error(exc)  # 输入保留
        else:
            _queue_key_clear(
                f"note_image_edit_mode_{note.id}",
                f"note_image_edit_personal_{note.id}",
                f"note_image_edit_imp_{note.id}",
            )
            st.session_state[_FLASH_KEY] = "笔记已保存。"
            st.rerun()
    if cancel_column.button("取消编辑", key=f"note_image_edit_cancel_{note.id}"):
        _queue_key_clear(
            f"note_image_edit_mode_{note.id}",
            f"note_image_edit_personal_{note.id}",
            f"note_image_edit_imp_{note.id}",
        )
        st.rerun()


def _render_image_rebind_area(
    note_service: NoteService, view: NoteView, document_id: int, display_width: int
) -> None:
    note = view.note
    active_key = f"note_image_rebind_active_{note.id}"
    with st.expander("重新框选区域", expanded=False):
        if not st.session_state.get(active_key):
            if st.button("开始重新框选", key=f"note_image_rebind_start_{note.id}"):
                st.session_state[active_key] = True
                st.rerun()
            return
        if st.button("取消重新框选", key=f"note_image_rebind_stop_{note.id}"):
            _queue_key_clear(
                active_key,
                f"note_image_rebind_region_{note.id}",
                f"note_image_rebind_confirm_{note.id}",
            )
            st.rerun()
            return
        st.caption(
            f"旧区域：({note.region_x0}, {note.region_y0}) - "
            f"({note.region_x1}, {note.region_y1}) · "
            f"旧图像 {note.region_image_width} × {note.region_image_height}"
        )
        try:
            preview = note_service.get_image_region_source_preview(note.page_id)
        except (PageImageMissingError, PageImageUnreadableError) as exc:
            st.warning(str(exc))
            return
        except Exception as exc:
            _show_load_error(exc)
            return
        st.caption(f"当前图像：{preview.width} × {preview.height} 像素")

        version = st.session_state.get(f"note_image_anchor_version_rebind_{note.id}", 0)
        component_key = (
            make_component_key(
                document_id,
                note.page_id,
                mode=f"rebind_region_{note.id}",
                anchor_version=version,
            )
            + f"_w{display_width}"
        )
        value = streamlit_image_coordinates(
            str(preview.path),
            width=display_width,
            key=component_key,
            click_and_drag=True,
            cursor="crosshair",
        )
        region_key = f"note_image_rebind_region_{note.id}"
        if value is not None:
            try:
                rect = display_to_original(
                    value.get("x1"),
                    value.get("y1"),
                    value.get("x2"),
                    value.get("y2"),
                    int(value["width"]),
                    int(value["height"]),
                    preview.width,
                    preview.height,
                )
            except (ValueError, TypeError, KeyError):
                st.warning("框选区域无效，请重新选择。")
            else:
                if st.session_state.get(region_key) != rect:
                    st.session_state[region_key] = rect

        region = st.session_state.get(region_key)
        if region:
            st.info(
                f"新区域：({region['x0']}, {region['y0']}) - "
                f"({region['x1']}, {region['y1']}) · 区域 "
                f"{region['x1'] - region['x0']} × {region['y1'] - region['y0']} 像素\n\n"
                "确认后区域锚点将被替换，个人笔记内容会保留。"
            )
            _render_region_overlay(preview.path, region)
            confirmed = st.checkbox(
                "确认重新框选区域：当前图片区域锚点将被替换，个人笔记内容会保留。",
                key=f"note_image_rebind_confirm_{note.id}",
            )
            if st.button(
                "确认重新框选",
                key=f"note_image_rebind_apply_{note.id}",
                disabled=not confirmed,
            ):
                try:
                    note_service.rebind_image_region(
                        note.id,
                        region["x0"],
                        region["y0"],
                        region["x1"],
                        region["y1"],
                    )
                except Exception as exc:
                    _show_save_error(exc)  # 新框选与个人笔记保留
                else:
                    st.session_state[f"note_image_anchor_version_rebind_{note.id}"] = (
                        version + 1
                    )
                    _queue_key_clear(region_key, f"note_image_rebind_confirm_{note.id}")
                    st.session_state[_FLASH_KEY] = "已重新框选区域。"
                    st.rerun()


def _region_overlay_bytes(image_path: Path, region: dict) -> bytes:
    """Draw the region rectangle on an in-memory copy; never touches the PNG."""

    with Image.open(image_path) as image:
        canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        [region["x0"], region["y0"], region["x1"], region["y1"]],
        outline="red",
        width=4,
    )
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_region_overlay(image_path: Path, region: dict) -> None:
    """Render one region overlay with failure isolation at the render boundary.

    A corrupted or undecodable PNG must degrade this single preview to a
    warning; it must never escape and crash the whole Streamlit page.
    """

    try:
        st.image(_region_overlay_bytes(image_path, region))
    except (OSError, ValueError) as exc:
        LOGGER.warning("区域预览生成失败：%s（%s）", image_path, exc)
        st.warning(f"区域预览生成失败：页面图像无法解码（{image_path.name}）。")


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
