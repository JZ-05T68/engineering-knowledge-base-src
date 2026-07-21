"""Streamlit dashboard for Engineering Knowledge Base v0.1.2."""

from __future__ import annotations

import logging

import streamlit as st

from src.runtime import (
    application_database,
    application_evidence_basket_service,
    application_settings,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="工程知识库 v0.1.2", page_icon="📚", layout="wide")
st.title("工程知识库 v0.1.2")
st.caption("本地、单用户的页面级工程知识管理系统")

try:
    settings = application_settings()
    database = application_database()
    basket_items = application_evidence_basket_service().list_items()
    stats = database.dashboard_stats()
    recent_documents = database.list_documents(sort_by="imported_desc")[:5]
    recent_pages = database.recent_edited_pages(5)
    next_review_page = next(iter(database.list_review_pages()), None)
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

if stats.documents == 0:
    st.info("知识库还是空的。按下面三个步骤建立第一批可检索、可复用的工程资料。")
    st.markdown(
        """
1. **导入第一份 PDF**：保留原件并逐页生成 PNG。
2. **复核并整理页面**：补充 Markdown，确认页面状态、标签和项目。
3. **搜索资料并加入证据篮**：收集具体选区，生成可追溯的证据包。
"""
    )
    if st.button("📥 导入第一份 PDF", use_container_width=True):
        st.switch_page("pages/1_导入资料.py")

main_actions = st.columns(2)
if main_actions[0].button(
    "继续处理下一待复核页",
    type="primary",
    disabled=next_review_page is None,
    use_container_width=True,
):
    st.query_params.clear()
    st.query_params["page_id"] = str(next_review_page.id)
    st.switch_page("pages/4_待整理页面.py")
if main_actions[1].button(
    f"查看证据篮（{len(basket_items)}）",
    use_container_width=True,
):
    st.switch_page("pages/9_证据篮.py")
if next_review_page is None:
    st.caption("当前没有待处理、草稿待复核或处理失败的页面。")

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
        st.info("还没有导入文档。请先导入第一份 PDF。")

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
        st.info("还没有编辑过页面笔记。导入后可从待复核页面开始整理。")

st.subheader("开始使用")
st.markdown(
    """
- **导入资料**：保存 PDF 原件，逐页生成 PNG 并提取已有文本层。
- **浏览资料**：筛选文档，在清晰双栏界面中阅读页面、编辑 Markdown、添加标签和项目。
- **待整理页面**：集中处理扫描件、手写页和失败页。
- **检索资料**：搜索页面、笔记、文档标题、标签和项目，直接跳转到命中页面。
- **证据篮**：持久收集多个页面的具体选区，排序并导出来源可追溯的 Markdown 证据包。
"""
)
st.info(
    f"所有资料仅保存在本机 `{settings.data_dir}`。服务只监听 "
    f"`{settings.host}:{settings.port}`，核心功能离线可用，不需要 API Key。"
)
