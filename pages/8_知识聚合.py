"""按项目或标签聚合分散在多份资料中的笔记与证据。"""

from __future__ import annotations

import logging

import streamlit as st

from src.aggregation_service import AggregationService
from src.aggregation_ui import render_aggregation_page
from src.runtime import application_database

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="知识聚合｜工程知识库 v0.5.0", page_icon="🧩", layout="wide")
st.title("知识聚合")
st.caption("按项目或标签汇总分散在多个资料中的笔记与证据，并保留原始出处。")

try:
    database = application_database()
except Exception as exc:
    LOGGER.exception("读取资料失败")
    st.error(f"读取资料失败：{exc}")
    st.stop()

render_aggregation_page(AggregationService(database), database)
