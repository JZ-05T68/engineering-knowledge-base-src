"""Shared offline-only visual shell for the local Streamlit workspace."""

from __future__ import annotations

import html
import os
from functools import lru_cache
from pathlib import Path

import streamlit as st

_STYLE_PATH = Path(__file__).with_name("workspace.css")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_NAVIGATION = (
    ("常用功能", (
        ("app.py", "首页", "home"),
        ("pages/0_知识Agent.py", "问问 Agent", "auto_awesome"),
        ("pages/1_导入资料.py", "添加资料", "upload_file"),
        ("pages/17_我的资料.py", "我的资料", "library_books"),
        ("pages/5_待整理页面.py", "查看识别结果", "fact_check"),
        ("pages/15_知识记忆.py", "我保存过的内容", "history"),
    )),
    ("更多", (
        ("pages/11_文档管理.py", "管理资料", "description"),
        ("pages/12_系统维护.py", "备份与修复", "tune"),
        ("pages/13_运行说明.py", "使用帮助", "help_outline"),
    )),
)

# Internal/advanced pages remain routable so existing bookmarks and workflows
# do not break, but their implementation terms are no longer primary navigation.
_HIDDEN_PAGE_TITLES = {
    "pages/2_导入记录.py": "导入记录",
    "pages/4_检索资料.py": "搜索资料",
    "pages/6_结构化笔记.py": "结构化笔记",
    "pages/7_证据篮.py": "已选资料",
    "pages/8_知识聚合.py": "知识聚合",
    "pages/9_标签管理.py": "标签管理",
    "pages/10_项目管理.py": "项目管理",
    "pages/14_知识对象.py": "知识对象",
    "pages/16_AI调用台账.py": "AI 使用记录",
    "pages/3_浏览资料.py": "高级资料管理",
}


@lru_cache(maxsize=4)
def _workspace_styles(modified_ns: int) -> str:
    """Cache offline CSS while refreshing it when the stylesheet changes."""

    return _STYLE_PATH.read_text(encoding="utf-8")


def render_workspace(current: str) -> None:
    """Render grouped navigation without initializing business services."""

    # Do not animate the shared main container: it can still contain stale page nodes.
    st.markdown(f"<style>{_workspace_styles(_STYLE_PATH.stat().st_mtime_ns)}</style>",
                unsafe_allow_html=True)
    with st.sidebar:
        st.markdown(
            '<div class="ekb-brand"><span class="ekb-brand-mark">E</span>'
            '<div><b>EKB 我的知识库</b>'
            '<small>资料和经验都在这里</small></div></div>'
            '<div class="ekb-space"><span class="ekb-space-icon">K</span>'
            '<div>我的资料和经验<small>把学过、做过的事情留住</small></div></div>',
            unsafe_allow_html=True,
        )
        for group, entries in _NAVIGATION:
            if group == "更多":
                with st.expander(group, expanded=any(p == current for p, _, _ in entries)):
                    _render_links(entries, current)
            else:
                st.markdown(f'<div class="ekb-nav-label">{group}</div>',
                            unsafe_allow_html=True)
                _render_links(entries, current)
        st.markdown(
            '<div class="ekb-local-note"><span class="ekb-status-dot"></span>'
            '资料保存在这台电脑<small>只有你点击时，Agent 才会读取资料。</small></div>',
            unsafe_allow_html=True,
        )
    visible_titles = {
        path: label for _, entries in _NAVIGATION for path, label, _ in entries
    }
    title = visible_titles.get(current, _HIDDEN_PAGE_TITLES.get(current, "当前页面"))
    status = "预览环境" if os.environ.get("EKB_STAGING_INSTANCE") == "1" else "仅在本机运行"
    st.markdown(
        '<div class="ekb-topbar"><span>我的 EKB'
        f'<span class="ekb-slash">/</span><b>{html.escape(title)}</b></span>'
        f'<span class="ekb-top-status"><i></i>{status}</span></div>',
        unsafe_allow_html=True,
    )


def _render_links(entries: tuple[tuple[str, str, str], ...], current: str) -> None:
    """Route directly in the frontend without first rerunning the source page."""

    for path, label, icon in entries:
        active = "_active" if path == current else ""
        with st.container(key=f"nav_{Path(path).stem}{active}"):
            # Match the same file-derived URLs as Streamlit's pages-directory router.
            # Absolute sources also allow individual page previews and AppTest runs.
            page = st.Page(_PROJECT_ROOT / path, default=path == "app.py")
            st.page_link(
                page, label=label, icon=f":material/{icon}:",
                disabled=path == current, use_container_width=True,
            )


def section_heading(title: str, detail: str = "") -> None:
    """Render a compact heading with escaped text."""

    st.markdown(
        f'<div class="ekb-section"><h2>{html.escape(title)}</h2>'
        f'<span>{html.escape(detail)}</span></div>', unsafe_allow_html=True,
    )


def empty_panel(title: str, detail: str) -> None:
    """Explain empty data honestly while keeping the next action nearby."""

    st.markdown(
        '<div class="ekb-empty"><div class="ekb-empty-symbol">◇</div>'
        f'<strong>{html.escape(title)}</strong><p>{html.escape(detail)}</p></div>',
        unsafe_allow_html=True,
    )
