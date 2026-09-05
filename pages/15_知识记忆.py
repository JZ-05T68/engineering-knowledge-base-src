"""Show content that the user explicitly chose to save."""

from __future__ import annotations

import logging

import streamlit as st

from src.knowledge_memory_ui import render_knowledge_memory_page
from src.runtime import application_database, application_knowledge_memory_service
from src.workspace_ui import render_workspace

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="我保存过的内容｜工程知识库 v0.6.0", page_icon="🧭", layout="wide")
render_workspace("pages/15_知识记忆.py")
st.title("我保存过的内容")
st.caption(
    "这里只保存你手动保留的问答副本。删除原资料后，副本仍会保留，"
    "除非你再手动删除。"
)

try:
    database = application_database()
    service = application_knowledge_memory_service()
except Exception as exc:
    LOGGER.exception("打开已保存内容失败")
    st.error(f"打开已保存内容失败：{exc}")
    st.stop()

render_knowledge_memory_page(service, database=database)
