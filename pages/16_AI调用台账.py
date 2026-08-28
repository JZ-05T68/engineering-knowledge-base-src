"""只读 AI 调用台账页面。"""

from __future__ import annotations

import logging

import streamlit as st

from src.ai_ledger_ui import render_ai_ledger_page
from src.runtime import application_ai_ledger_service

LOGGER = logging.getLogger(__name__)

st.set_page_config(
    page_title="AI 调用台账｜工程知识库 v0.6.0", page_icon="🧾", layout="wide"
)
st.title("AI 调用台账")
st.caption("只读审计视图：本地 SQLite 查询，不发起任何模型调用。")

try:
    service = application_ai_ledger_service()
except Exception as exc:
    LOGGER.exception("初始化 AI 调用台账服务失败")
    st.error(f"初始化 AI 调用台账服务失败：{exc}")
    st.stop()

render_ai_ledger_page(service)
