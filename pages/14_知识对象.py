"""浏览、创建并关联个人知识对象。"""

from __future__ import annotations

import logging

import streamlit as st

from src.knowledge_object_ui import render_knowledge_object_page
from src.runtime import (
    application_database,
    application_knowledge_object_service,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="知识对象｜工程知识库 v0.5.2", page_icon="🧠", layout="wide")
st.title("知识对象")
st.caption("把散落在页面、笔记和证据中的知识提炼为可复用、可关联、可追溯的个人知识资产。")

try:
    database = application_database()
    service = application_knowledge_object_service()
except Exception as exc:
    LOGGER.exception("初始化知识对象服务失败")
    st.error(f"初始化知识对象服务失败：{exc}")
    st.stop()

render_knowledge_object_page(service, database)
