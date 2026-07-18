"""Review pending and failed pages with document filtering and retry controls."""

from __future__ import annotations

import logging

import streamlit as st

from src.models import PageStatus
from src.runtime import application_database, application_document_service

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="待复核页面｜工程知识库 v0.0.2", page_icon="📝", layout="wide")
st.title("待复核页面")
st.caption("集中处理扫描件、手写页、文本不足页和单页处理失败。")

try:
    database = application_database()
    document_service = application_document_service()
    documents = database.list_documents()
except Exception as exc:
    LOGGER.exception("读取待复核页面失败")
    st.error(f"读取待复核页面失败：{exc}")
    st.stop()

document_options = {document.id: document for document in documents}
selected_document = st.selectbox(
    "按文档筛选",
    options=[None, *document_options],
    format_func=lambda value: "全部文档" if value is None else document_options[value].title,
)
pending_pages = database.list_review_pages(selected_document)
if not pending_pages:
    st.success("当前筛选范围内没有待复核页面。")
    st.stop()

pending_options = {page.id: page for page in pending_pages}
selected_page_id = st.selectbox(
    "选择页面",
    options=list(pending_options),
    format_func=lambda value: (
        f"{document_options[pending_options[value].document_id].title} · "
        f"第 {pending_options[value].page_number} 页 · {pending_options[value].status.label}"
    ),
)
page = pending_options[selected_page_id]
document = document_options[page.document_id]

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
                st.success("重新处理完成。")
                st.rerun()

with editor_column:
    st.subheader("人工整理 Markdown")
    markdown_content = st.text_area(
        "请根据左侧原图录入或校对本页内容",
        value=page.markdown_content,
        height=500,
        key=f"pending_markdown_{page.id}",
    )
    if markdown_content != page.markdown_content:
        st.warning("● 未保存")
    else:
        st.success("● 已保存")
    action_columns = st.columns(2)
    if action_columns[0].button("保存并标记已整理", type="primary"):
        try:
            document_service.save_page_markdown(
                document_id=document.id,
                page_number=page.page_number,
                markdown_content=markdown_content,
            )
        except Exception as exc:
            LOGGER.exception("保存待复核页面失败：page_id=%s", page.id)
            st.error(f"保存失败：{exc}")
        else:
            st.success("页面笔记已保存，并标记为已人工整理。")
            st.rerun()
    if action_columns[1].button("仅标记为已人工整理"):
        try:
            document_service.mark_page_reviewed(page.id)
        except Exception as exc:
            st.error(f"更新状态失败：{exc}")
        else:
            st.rerun()

if page.status is PageStatus.FAILED:
    st.warning("该页处理失败；其他已完成页面和原 PDF 不受影响。")
