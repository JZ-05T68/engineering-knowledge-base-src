"""记录问题解决过程、经验与决策，沉淀个人知识记忆。"""

from __future__ import annotations

import logging

import streamlit as st

from src.knowledge_memory_ui import render_knowledge_memory_page
from src.runtime import (
    application_database,
    application_knowledge_memory_service,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="知识记忆｜工程知识库 v0.5.2", page_icon="🧭", layout="wide")
st.title("知识记忆")
st.caption("记录问题解决过程、经验和决策；知识对象的变更会自动留痕。")

try:
    database = application_database()
    service = application_knowledge_memory_service()
except Exception as exc:
    LOGGER.exception("初始化知识记忆服务失败")
    st.error(f"初始化知识记忆服务失败：{exc}")
    st.stop()

render_knowledge_memory_page(service)
