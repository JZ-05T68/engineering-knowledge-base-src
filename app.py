"""Local knowledge workspace: overview, search and useful next actions."""

from __future__ import annotations

import logging
from datetime import date

import streamlit as st

from src.runtime import (
    application_database,
    application_settings,
    application_startup_reconciliation,
)
from src.workspace_ui import empty_panel, render_workspace, section_heading

LOGGER = logging.getLogger(__name__)


def _render_footer() -> None:
    """Keep the product version visible on both first-use and normal home pages."""

    st.markdown(
        '<div class="ekb-footer"><span>我的资料和经验 · 由我保存，为我所用</span>'
        '<span>EKB v0.6.0</span></div>', unsafe_allow_html=True,
    )


st.set_page_config(page_title="工作台 · 工程知识库 v0.6.0", page_icon="📚", layout="wide")
render_workspace("app.py")

try:
    settings = application_settings()
    database = application_database()
    quarantine_reconciliation = application_startup_reconciliation()
    stats = database.dashboard_stats()
    recent_documents = database.list_documents(sort_by="imported_desc")[:5]
    recent_pages = database.recent_edited_pages(5)
    next_review_page = next(iter(database.list_review_pages()), None)
except Exception as exc:
    LOGGER.exception("应用初始化失败")
    st.error(f"应用初始化失败：{exc}")
    st.stop()

if quarantine_reconciliation is not None and quarantine_reconciliation.has_attention:
    st.warning(
        "检测到需要人工处理的删除操作残留：系统未自动删除或覆盖这些文件，"
        "请前往“备份与修复”页查看详情。"
    )

if stats.documents == 0:
    st.markdown(
        '<div class="ekb-intro"><div><h1>第 1 步：添加资料</h1>'
        '<p>先选择一个 PDF、Word 或 PowerPoint 文件，其他事情交给 Agent。</p>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    with st.container(key="home_first_use"):
        section_heading("第一次使用，只需要三步", "")
        for number, title, detail in (
            ("01", "添加资料", "从电脑中选择一份文件。"),
            ("02", "让 Agent 阅读", "点击一次，等待 Agent 按页读完。"),
            ("03", "开始提问", "用自己的话提问，并查看答案来自哪一页。"),
        ):
            st.markdown(
                f'<div class="ekb-step"><span class="ekb-step-num">{number}</span>'
                f'<div><b>{title}</b><p>{detail}</p></div></div>',
                unsafe_allow_html=True,
            )
        if st.button(
            "添加资料",
            icon=":material/upload_file:",
            type="primary",
            use_container_width=True,
            key="first_use_add_document",
        ):
            st.switch_page("pages/1_导入资料.py")
        st.caption("资料读完后，页面会直接带你去问 Agent。")
    _render_footer()
    st.stop()

today = date.today()
weekday = "一二三四五六日"[today.weekday()]
st.markdown(
    '<div class="ekb-intro"><div><h1>把学过的、做过的，变成以后用得上的经验。</h1>'
    '<p>添加资料，让 Agent 读懂；需要的时候，随时回来问。</p></div>'
    f'<span class="ekb-date">{today:%Y年%m月%d日} · 星期{weekday}</span></div>',
    unsafe_allow_html=True,
)

with st.container(key="home_search"), st.form("home_search_form", border=True):
    query_column, action_column = st.columns([6, 1])
    query = query_column.text_input(
        "搜索我的资料", placeholder="搜索资料名称或记得的一句话…",
        label_visibility="collapsed", key="home_search_query",
    )
    search_submitted = action_column.form_submit_button(
        "搜索", icon=":material/search:", type="primary", use_container_width=True,
    )
if search_submitted:
    if query.strip():
        # Keep the existing search-state handoff, including its destination validation.
        st.session_state["pending_search_query_params"] = {"q": query.strip()[:500]}
        st.switch_page("pages/4_检索资料.py")
    else:
        st.info("请输入你想找的内容。")

st.markdown(
    '<div class="ekb-hero"><div class="ekb-hero-kicker">资料 · 理解 · 经验</div>'
    '<h2>不只收藏资料，<br>还要记住你从中学到了什么。</h2>'
    '<p>Agent 会阅读你添加的资料、回答问题并标出原始页码。<br>'
    '有用的对话，可以由你亲手保存起来。</p>'
    '<div class="ekb-hero-tags"><span>✓ 文件在本机</span><span>✓ 答案标页码</span>'
    '<span>✓ 是否保存由你决定</span></div>'
    '<div class="ekb-art" aria-hidden="true"><div class="ekb-orbit"></div>'
    '<div class="ekb-orbit inner"></div><div class="ekb-book"><b>Ekb.</b>KNOWLEDGE'
    '<i class="ekb-book-line"></i><i class="ekb-book-line"></i></div>'
    '<span class="ekb-art-dot">✧</span><div class="ekb-art-label">连接 · 理解 · 复用</div>'
    '</div></div>', unsafe_allow_html=True,
)
with st.container(key="home_actions"):
    actions = st.columns([1.3, 1.1, 2.8], vertical_alignment="center")
    if actions[0].button("问问 Agent →", type="primary", use_container_width=True):
        st.switch_page("pages/0_知识Agent.py")
    if actions[1].button("添加资料", icon=":material/add:", use_container_width=True):
        st.switch_page("pages/1_导入资料.py")
    actions[2].caption("先让 Agent 读完一份资料，再直接用自己的话提问。")

section_heading("我的内容", "保存在这台电脑里的内容")
with st.container(key="home_metrics"):
    for column, label, value in zip(
        st.columns(4), ("资料", "已保存页面", "我的笔记", "可查看页面"),
        (stats.documents, stats.pages, stats.noted_pages, stats.review_pages), strict=True,
    ):
        column.metric(label, value)

left, right = st.columns([1.7, 1], gap="medium")
with left, st.container(key="home_recent"):
    section_heading("最近导入", "最近 5 份资料")
    if recent_documents:
        for document in recent_documents:
            if st.button(
                f"{document.title} · {document.page_count} 页 · {document.status_label}",
                icon=":material/description:", key=f"recent_document_{document.id}",
                use_container_width=True,
            ):
                st.switch_page(
                    "pages/17_我的资料.py", query_params={"document": str(document.id)}
                )
    else:
        empty_panel(
            "你的下一次积累，从这里开始",
            "添加讲义、说明书或学习资料，让 Agent 从第一页开始读。",
        )
        st.info("这里还没有资料。添加第一份文件后就可以开始提问。")
        if st.button("添加第一份资料", icon=":material/upload_file:", use_container_width=True):
            st.switch_page("pages/1_导入资料.py")
    if st.button("查看全部资料 →", key="home_browse_all", use_container_width=True):
        st.switch_page("pages/17_我的资料.py")

with right, st.container(key="home_workflow"):
    section_heading("只需要三步", "")
    for number, title, detail in (
        ("01", "添加资料", "选择 PDF、Word 或 PowerPoint 文件。"),
        ("02", "让 Agent 读", "Agent 按页读取，图片或手写页会尝试识别。"),
        ("03", "开始提问", "答案会告诉你来自哪份资料、哪一页。"),
    ):
        st.markdown(
            f'<div class="ekb-step"><span class="ekb-step-num">{number}</span>'
            f'<div><b>{title}</b><p>{detail}</p></div></div>', unsafe_allow_html=True,
        )
    if st.button("查看识别结果", disabled=next_review_page is None,
                 use_container_width=True):
        st.switch_page(
            "pages/5_待整理页面.py", query_params={"page_id": str(next_review_page.id)}
        )
    if next_review_page is None:
        st.caption("当前没有新的识别结果需要提醒。")

with st.container(key="home_edits"):
    section_heading("最近修改", "我亲手校对过的页面")
    if recent_pages:
        for page in recent_pages:
            document = database.get_document(page.document_id)
            if document and st.button(
                f"{document.title} · 第 {page.page_number} 页", icon=":material/edit_note:",
                key=f"recent_page_{page.id}", use_container_width=True,
            ):
                st.switch_page(
                    "pages/17_我的资料.py",
                    query_params={"document": str(document.id), "page": str(page.page_number)},
                )
    else:
        st.caption("还没有修改过页面。发现识别文字有误时再修改即可。")

_render_footer()
with st.expander("资料保存在哪里"):
    st.caption(
        f"资料保存在本机 {settings.data_dir}。"
        f"这个页面只在 {settings.host}:{settings.port} 打开。"
    )
