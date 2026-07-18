"""Continuous, guarded workflow for reviewing local document pages."""

from __future__ import annotations

import logging

import streamlit as st
import streamlit.components.v1 as components

from src.models import Page, PageStatus
from src.review_shortcuts import review_shortcuts_html
from src.runtime import application_database, application_document_service

LOGGER = logging.getLogger(__name__)
_ACTIVE_PAGE_KEY = "review_active_page_id"
_SELECTOR_KEY = "review_page_selector"
_PENDING_TARGET_KEY = "review_pending_target_id"
_FLASH_KEY = "review_flash"

st.set_page_config(page_title="待复核页面｜工程知识库 v0.0.8", page_icon="📝", layout="wide")
st.title("待复核页面")
st.caption("连续保存草稿、人工复核或暂时跳过；所有内容仅保存在本机。")


def _editor_key(page_id: int) -> str:
    return f"review_markdown_{page_id}"


def _saved_key(page_id: int) -> str:
    return f"review_saved_markdown_{page_id}"


def _is_dirty(page_id: int) -> bool:
    return st.session_state.get(_editor_key(page_id), "") != st.session_state.get(
        _saved_key(page_id), ""
    )


def _activate_page(page_id: int) -> None:
    st.session_state[_ACTIVE_PAGE_KEY] = page_id
    st.session_state.pop(_PENDING_TARGET_KEY, None)


def _on_page_selector_change() -> None:
    requested = int(st.session_state[_SELECTOR_KEY])
    current = int(st.session_state[_ACTIVE_PAGE_KEY])
    if requested == current:
        return
    if _is_dirty(current):
        st.session_state[_PENDING_TARGET_KEY] = requested
        st.session_state[_SELECTOR_KEY] = current
        return
    _activate_page(requested)


def _page_label(page: Page, document_titles: dict[int, str]) -> str:
    return (
        f"{document_titles.get(page.document_id, f'文档 {page.document_id}')} · "
        f"第 {page.page_number} 页 · {page.status.label}"
    )


try:
    database = application_database()
    document_service = application_document_service()
    documents = database.list_documents()
except Exception as exc:
    LOGGER.exception("读取待复核页面失败")
    st.error(f"读取待复核页面失败：{exc}")
    st.stop()

document_options = {document.id: document for document in documents}
document_titles = {document.id: document.title for document in documents}
selected_document = st.selectbox(
    "按文档筛选待处理队列",
    options=[None, *document_options],
    format_func=lambda value: "全部文档" if value is None else document_options[value].title,
    key="review_document_filter",
)
review_queue = database.list_review_pages(selected_document)

query_page_id = st.query_params.get("page_id")
try:
    requested_page_id = int(query_page_id) if query_page_id else None
except ValueError:
    requested_page_id = None

active_page = None
active_page_id = st.session_state.get(_ACTIVE_PAGE_KEY)
if active_page_id is not None:
    active_page = database.get_page(int(active_page_id))
if active_page is None and requested_page_id is not None:
    active_page = database.get_page(requested_page_id)
if active_page is None and review_queue:
    active_page = review_queue[0]
if active_page is None:
    if documents:
        st.success("当前筛选范围内没有待复核页面。可以查看已经整理过的资料。")
        if st.button("📖 查看已复核资料", use_container_width=True):
            st.switch_page("pages/2_浏览资料.py")
    else:
        st.info("还没有文档或待复核页面。请先导入第一份 PDF。")
        if st.button("📥 前往导入资料", use_container_width=True):
            st.switch_page("pages/1_导入资料.py")
    st.stop()

if _ACTIVE_PAGE_KEY not in st.session_state or (
    int(st.session_state[_ACTIVE_PAGE_KEY]) != active_page.id
):
    _activate_page(active_page.id)

selectable_pages = {page.id: page for page in review_queue}
selectable_pages.setdefault(active_page.id, active_page)
selector_options = sorted(
    selectable_pages,
    key=lambda page_id: (
        selectable_pages[page_id].document_id,
        selectable_pages[page_id].page_number,
    ),
)
st.session_state[_SELECTOR_KEY] = int(st.session_state[_ACTIVE_PAGE_KEY])
st.selectbox(
    "选择待处理页面",
    options=selector_options,
    format_func=lambda value: _page_label(selectable_pages[value], document_titles),
    key=_SELECTOR_KEY,
    on_change=_on_page_selector_change,
)

active_page_id = int(st.session_state[_ACTIVE_PAGE_KEY])
page = database.get_page(active_page_id)
if page is None:
    st.error("当前页面记录不存在，请返回待复核队列重新选择。")
    st.stop()
document = document_options.get(page.document_id)
if document is None:
    st.error("当前页面所属文档不存在。")
    st.stop()
st.query_params["page_id"] = str(page.id)

flash = st.session_state.pop(_FLASH_KEY, None)
if flash is not None:
    level, message = flash
    getattr(st, level)(message)

pending_target_id = st.session_state.get(_PENDING_TARGET_KEY)
if pending_target_id is not None:
    target = database.get_page(int(pending_target_id))
    target_label = _page_label(target, document_titles) if target else "目标页面"
    st.warning(f"当前页有未保存修改。是否放弃修改并切换到：{target_label}？")
    stay_column, discard_column = st.columns(2)
    if stay_column.button("留在当前页", use_container_width=True):
        st.session_state.pop(_PENDING_TARGET_KEY, None)
        st.rerun()
    if discard_column.button("放弃未保存修改并切换", use_container_width=True):
        st.session_state[_editor_key(page.id)] = st.session_state.get(
            _saved_key(page.id), page.markdown_content
        )
        if target is not None:
            _activate_page(target.id)
        else:
            st.session_state.pop(_PENDING_TARGET_KEY, None)
        st.rerun()

if st.session_state.get("review_last_viewed_page_id") != page.id:
    try:
        page = database.mark_page_viewed(page.id)
        st.session_state["review_last_viewed_page_id"] = page.id
    except Exception as exc:
        LOGGER.exception("记录页面访问时间失败：page_id=%s", page.id)
        st.warning(f"页面可以继续编辑，但访问时间记录失败：{exc}")

progress = database.review_progress(document.id)
heading_columns = st.columns([5, 1])
heading_columns[0].markdown(f"### {document.title}")
heading_columns[1].metric("当前页", f"第 {page.page_number} 页")
progress_columns = st.columns(4)
progress_columns[0].markdown(
    "<div style='margin-top:0.45rem'>"
    f"<span style='display:inline-block;padding:0.35rem 0.7rem;border-radius:999px;"
    "background:#e8f0fe;color:#174ea6;font-weight:700;white-space:nowrap'>"
    f"当前状态：{page.status.label}</span></div>",
    unsafe_allow_html=True,
)
progress_columns[1].metric("已处理数", progress.processed)
progress_columns[2].metric("总页数", progress.total)
progress_columns[3].metric("剩余待处理数", progress.remaining)

previous_page = database.get_adjacent_review_page(page.id, "previous", selected_document)
next_page = database.get_adjacent_review_page(page.id, "next", selected_document)
continuation_page = next_page
if page.status not in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}:
    continuation_page = review_queue[0] if review_queue else None
if st.button(
    "继续处理下一待复核页",
    disabled=continuation_page is None,
    use_container_width=True,
):
    if continuation_page is not None and _is_dirty(page.id):
        st.session_state[_PENDING_TARGET_KEY] = continuation_page.id
    elif continuation_page is not None:
        _activate_page(continuation_page.id)
    st.rerun()

image_column, editor_column = st.columns([1, 1], gap="large")
with image_column:
    st.subheader("原始页面")
    with st.container(height=720, border=True):
        if page.image_path.exists():
            st.image(str(page.image_path), width="stretch")
        else:
            st.error(f"页面图片缺失：{page.image_path}")
    with st.expander("查看已提取文本"):
        st.text(page.extracted_text or "（没有提取到文本）")
    if page.processing_error:
        st.error(f"失败原因：{page.processing_error}")
        if st.button("重新处理此页"):
            try:
                with st.spinner("正在使用本地 PDF 处理流程重试……"):
                    document_service.reprocess_page(page.id)
            except Exception as exc:
                LOGGER.exception("重新处理页面失败：page_id=%s", page.id)
                st.error(f"重新处理失败：{exc}")
            else:
                st.session_state[_FLASH_KEY] = ("success", "重新处理完成。")
                st.rerun()

with editor_column:
    st.subheader("人工整理 Markdown")
    editor_key = _editor_key(page.id)
    saved_key = _saved_key(page.id)
    if editor_key not in st.session_state:
        st.session_state[editor_key] = page.markdown_content
    if saved_key not in st.session_state:
        st.session_state[saved_key] = page.markdown_content
    elif (
        st.session_state[editor_key] == st.session_state[saved_key]
        and st.session_state[saved_key] != page.markdown_content
    ):
        st.session_state[editor_key] = page.markdown_content
        st.session_state[saved_key] = page.markdown_content
    st.text_area(
        "请根据左侧原图录入或校对本页内容",
        height=500,
        key=editor_key,
    )
    markdown_content = str(st.session_state[editor_key])
    dirty = markdown_content != page.markdown_content
    if dirty:
        st.warning("● 有未保存修改；切换页面前会要求确认。")
    else:
        st.success("● 内容已保存")

    primary_actions = st.columns(3)
    save_draft = primary_actions[0].button(
        "保存草稿", type="primary", use_container_width=True
    )
    save_reviewed = primary_actions[1].button(
        "保存并标记已复核", use_container_width=True
    )
    save_reviewed_next = primary_actions[2].button(
        "保存、复核并进入下一页", use_container_width=True
    )

    navigation_actions = st.columns(3)
    skip_page = navigation_actions[0].button(
        "暂时跳过",
        disabled=dirty,
        use_container_width=True,
        help="有未保存修改时请先保存草稿，避免丢失内容。",
    )
    go_previous = navigation_actions[1].button(
        "上一待处理页",
        disabled=previous_page is None,
        use_container_width=True,
    )
    go_next = navigation_actions[2].button(
        "下一待处理页",
        disabled=next_page is None,
        use_container_width=True,
    )
    if dirty:
        st.caption("为保护未保存内容，“暂时跳过”已停用；请先保存草稿或复核。")

    if save_draft or save_reviewed or save_reviewed_next:
        try:
            with st.spinner("正在保存到本机……"):
                destination = None
                if save_reviewed_next:
                    updated_page, destination = document_service.save_page_markdown_and_next(
                        document.id,
                        page.page_number,
                        markdown_content,
                        queue_document_id=selected_document,
                    )
                else:
                    updated_page = document_service.save_page_markdown(
                        document.id,
                        page.page_number,
                        markdown_content,
                        mark_reviewed=save_reviewed,
                    )
        except Exception as exc:
            LOGGER.exception("保存待复核页面失败：page_id=%s", page.id)
            st.error(f"保存失败：{exc}。编辑框内容已保留，请重试。")
        else:
            st.session_state[saved_key] = updated_page.markdown_content
            if save_reviewed_next and destination is not None:
                _activate_page(destination.id)
                st.session_state[_FLASH_KEY] = (
                    "success",
                    "本页已保存并完成人工复核，已进入下一待处理页。",
                )
            elif save_reviewed_next:
                st.session_state[_FLASH_KEY] = (
                    "info",
                    "本页已保存并完成人工复核；当前队列没有下一待处理页。",
                )
            elif save_reviewed:
                st.session_state[_FLASH_KEY] = (
                    "success",
                    "页面已保存并标记为人工复核完成。",
                )
            else:
                st.session_state[_FLASH_KEY] = ("success", "Markdown 草稿已保存。")
            st.rerun()

    if skip_page:
        try:
            _, destination = document_service.skip_page_and_next(
                page.id,
                queue_document_id=selected_document,
            )
        except Exception as exc:
            LOGGER.exception("跳过页面失败：page_id=%s", page.id)
            st.error(f"暂时跳过失败：{exc}")
        else:
            if destination is not None:
                _activate_page(destination.id)
                st.session_state[_FLASH_KEY] = (
                    "success",
                    "本页已设为暂不整理，已进入下一待处理页。",
                )
            else:
                st.session_state[_FLASH_KEY] = (
                    "info",
                    "本页已设为暂不整理；当前队列没有下一待处理页。",
                )
            st.rerun()

    requested_navigation = previous_page if go_previous else next_page if go_next else None
    if go_previous or go_next:
        if requested_navigation is None:
            direction_label = "上一" if go_previous else "下一"
            st.info(f"当前队列没有{direction_label}待处理页。")
        elif dirty:
            st.session_state[_PENDING_TARGET_KEY] = requested_navigation.id
            st.rerun()
        else:
            _activate_page(requested_navigation.id)
            st.rerun()

if page.status is PageStatus.FAILED:
    st.warning("该页处理失败；其他已完成页面和原 PDF 不受影响。")

st.caption(
    "快捷键：Ctrl+S 保存草稿；Ctrl+Enter 保存、复核并进入下一页；"
    "Alt+Left / Alt+Right 切换待处理页（文本输入时不触发方向快捷键）。"
)
components.html(review_shortcuts_html(), height=0, width=0)
