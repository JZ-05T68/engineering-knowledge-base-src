"""Streamlit dashboard for Engineering Knowledge Base v0.0.2."""

from __future__ import annotations

import logging

import streamlit as st

from src.runtime import application_database, application_settings

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="工程知识库 v0.0.2", page_icon="📚", layout="wide")
st.title("工程知识库 v0.0.2")
st.caption("本地、单用户的页面级工程知识管理系统")

try:
    settings = application_settings()
    database = application_database()
    stats = database.dashboard_stats()
    recent_documents = database.list_documents(sort_by="imported_desc")[:5]
    recent_pages = database.recent_edited_pages(5)
except Exception as exc:
    LOGGER.exception("应用初始化失败")
    st.error(f"应用初始化失败：{exc}")
    st.stop()

columns = st.columns(6)
for column, label, value in zip(
    columns,
    ("文档", "页面", "已写笔记", "待复核", "标签", "项目"),
    (
        stats.documents,
        stats.pages,
        stats.noted_pages,
        stats.review_pages,
        stats.tags,
        stats.projects,
    ),
    strict=True,
):
    column.metric(label, value)

left, right = st.columns(2, gap="large")
with left:
    st.subheader("最近导入")
    if recent_documents:
        for document in recent_documents:
            if st.button(
                f"{document.title} · {document.page_count} 页 · {document.status_label}",
                key=f"recent_document_{document.id}",
                use_container_width=True,
            ):
                st.query_params["document"] = str(document.id)
                st.switch_page("pages/2_浏览资料.py")
    else:
        st.info("还没有导入文档。")

with right:
    st.subheader("最近编辑")
    if recent_pages:
        for page in recent_pages:
            document = database.get_document(page.document_id)
            if document and st.button(
                f"{document.title} · 第 {page.page_number} 页",
                key=f"recent_page_{page.id}",
                use_container_width=True,
            ):
                st.query_params.update(
                    {"document": str(document.id), "page": str(page.page_number)}
                )
                st.switch_page("pages/2_浏览资料.py")
    else:
        st.info("还没有编辑过页面笔记。")

st.subheader("开始使用")
st.markdown(
    """
- **导入资料**：保存 PDF 原件，逐页生成 PNG 并提取已有文本层。
- **浏览资料**：筛选文档，在清晰双栏界面中阅读页面、编辑 Markdown、添加标签和项目。
- **待整理页面**：集中处理扫描件、手写页和失败页。
- **检索资料**：搜索页面、笔记、文档标题、标签和项目，直接跳转到命中页面。
"""
)
st.info(
    f"所有资料仅保存在本机 `{settings.data_dir}`。服务只监听 "
    f"`{settings.host}:{settings.port}`，核心功能离线可用，不需要 API Key。"
)
