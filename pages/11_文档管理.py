"""全量文档清单与文档生命周期管理（永久删除）入口。"""

from __future__ import annotations

import logging

import streamlit as st

from src.document_deletion_ui import render_document_deletion_section
from src.runtime import (
    application_database,
    application_document_deletion_service,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="文档管理｜工程知识库 v0.5.3", page_icon="🗂️", layout="wide")
st.title("文档管理")
st.caption("查看全部已导入文档，并对选定文档执行影响整份文档的管理操作。")

try:
    database = application_database()
    deletion_service = application_document_deletion_service()
    documents = database.list_documents()
except Exception as exc:
    LOGGER.exception("读取文档清单失败")
    st.error(f"读取文档清单失败：{exc}")
    st.stop()

deletion_flash = st.session_state.pop("doc_delete_flash", None)
if deletion_flash:
    flash_message, flash_warnings = deletion_flash
    st.success(flash_message)
    for flash_warning in flash_warnings:
        st.warning(flash_warning)

# Widget keys may only be removed before their widgets are instantiated in a
# run, so a successful deletion defers the cleanup of its confirmation inputs
# to the top of the next run via this flag.
if st.session_state.pop("doc_delete_reset_pending", False):
    for stale_key in [
        key for key in st.session_state if key.startswith("doc_delete_")
    ]:
        del st.session_state[stale_key]

# Selection identity is the document id, never a list position. A deleted
# document's id may still sit in session state after the rerun triggered by
# the deletion flow, so a stale value is dropped before the selectbox is
# instantiated and Streamlit falls back to the first available document.
document_ids = [document.id for document in documents]
if st.session_state.get("doc_manage_selected_document_id") not in document_ids:
    st.session_state.pop("doc_manage_selected_document_id", None)

if not documents:
    st.info("当前还没有已导入的文档。请先在「导入资料」页面导入 PDF 文档。")
    st.stop()

st.dataframe(
    [
        {
            "标题": document.title,
            "原始文件名": document.filename,
            "页数": document.page_count,
            "状态": document.status_label,
            "导入时间": (
                f"{(document.imported_at or document.created_at).astimezone():%Y-%m-%d %H:%M}"
            ),
        }
        for document in documents
    ],
    use_container_width=True,
    hide_index=True,
)

st.divider()
st.header("删除导入文档")
st.caption("以下操作会影响整份文档，请谨慎执行。")

documents_by_id = {document.id: document for document in documents}
selected_document_id = st.selectbox(
    "选择文档",
    options=document_ids,
    format_func=lambda document_id: (
        f"{documents_by_id[document_id].title}"
        f"（{documents_by_id[document_id].filename}，"
        f"{documents_by_id[document_id].page_count} 页）"
    ),
    key="doc_manage_selected_document_id",
)
selected_document = documents_by_id.get(selected_document_id)
if selected_document is None:
    st.error("所选文档已不存在，请重新选择。")
    st.stop()

render_document_deletion_section(
    deletion_service=deletion_service,
    document=selected_document,
)
