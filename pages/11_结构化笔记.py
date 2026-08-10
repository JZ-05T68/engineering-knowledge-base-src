"""集中查看全部结构化笔记并返回原始文档或页面。"""

from __future__ import annotations

import logging

import streamlit as st

from src.note_list_ui import render_notes_list_page
from src.note_service import NoteService
from src.runtime import application_database

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="结构化笔记｜工程知识库 v0.3.2", page_icon="🗂️", layout="wide")
st.title("结构化笔记")
st.caption("集中查看文档级、页面级、文字选区和图片区域笔记，并返回原始文档或页面。")

try:
    database = application_database()
except Exception as exc:
    LOGGER.exception("读取资料失败")
    st.error(f"读取资料失败：{exc}")
    st.stop()

render_notes_list_page(NoteService(database))
