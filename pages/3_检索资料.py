"""Search all local knowledge fields and open matching pages directly."""

from __future__ import annotations

import logging
import re

import streamlit as st

from src.prompt_builder import PromptBuilder
from src.runtime import application_database
from src.search_service import SearchService

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="检索资料｜工程知识库 v0.0.3", page_icon="🔎", layout="wide")
st.title("检索资料")
st.caption("本地搜索文档标题、文件名、页面文本、OCR、Markdown 笔记、标签和项目。")

try:
    search_service = SearchService(application_database())
except Exception as exc:
    LOGGER.exception("初始化检索服务失败")
    st.error(f"初始化检索服务失败：{exc}")
    st.stop()

with st.form("search_form"):
    query = st.text_input(
        "搜索内容",
        value=st.session_state.get("knowledge_query", ""),
        placeholder="例如：STM32 电源 PID",
    )
    limit = st.slider("最多返回结果", min_value=5, max_value=50, value=20, step=5)
    submitted = st.form_submit_button("检索", type="primary")

if submitted:
    st.session_state["knowledge_query"] = query
    st.session_state["prompt_question"] = query
    try:
        st.session_state["knowledge_results"] = search_service.search(query, limit=limit)
    except Exception as exc:
        LOGGER.exception("全文检索失败：query=%r", query)
        st.error(f"检索失败：{exc}")
        st.session_state["knowledge_results"] = []
    st.session_state.pop("knowledge_prompt", None)

results = st.session_state.get("knowledge_results", [])
active_query = st.session_state.get("knowledge_query", query)
if submitted and not query.strip():
    st.warning("请输入要检索的内容。")
elif submitted and not results:
    st.info("没有找到相关页面。可以缩短关键词，或先为扫描页面补充 Markdown。")

if results:
    st.subheader(f"检索结果（{len(results)} 条）")
    terms = [term for term in re.findall(r"[\w\u3400-\u9fff]+", active_query) if term]
    for index, result in enumerate(results, start=1):
        with st.container(border=True):
            heading, action = st.columns([5, 1])
            heading.markdown(
                f"**{index}. {result.document_title} · 第 {result.page_number} 页**"
            )
            heading.caption(f"匹配类型：{result.match_type}　|　原文件：{result.filename}")
            snippet = result.snippet or result.content[:220]
            highlighted = snippet
            for term in sorted(terms, key=len, reverse=True):
                highlighted = re.sub(
                    re.escape(term),
                    lambda match: f"**{match.group(0)}**",
                    highlighted,
                    flags=re.IGNORECASE,
                )
            st.markdown(highlighted)
            if result.tags:
                st.caption("标签：" + "、".join(result.tags))
            if result.projects:
                st.caption("项目：" + "、".join(result.projects))
            if action.button("打开页面", key=f"open_result_{result.page_id}"):
                st.query_params.update(
                    {"document": str(result.document_id), "page": str(result.page_number)}
                )
                st.switch_page("pages/2_浏览资料.py")
            with st.expander("查看完整知识片段"):
                st.text(result.content)

    st.divider()
    st.subheader("生成外部 AI 提示词（可选、手动复制）")
    st.caption("本功能只生成本地文本，不连接任何 AI 服务，也不读取 API Key。")
    question = st.text_area(
        "要交给外部 AI 回答的问题",
        value=active_query,
        height=100,
        key="prompt_question",
    )
    if st.button("生成引用提示词"):
        try:
            st.session_state["knowledge_prompt"] = PromptBuilder().build(question, results)
        except Exception as exc:
            LOGGER.exception("生成外部 AI 提示词失败")
            st.error(f"生成提示词失败：{exc}")
    prompt = st.session_state.get("knowledge_prompt")
    if prompt:
        st.text_area("提示词", value=prompt, height=500)
