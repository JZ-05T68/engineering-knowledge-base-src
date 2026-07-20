"""Display durable PDF import history and result statistics."""

from __future__ import annotations

import streamlit as st

from src.runtime import application_database

st.set_page_config(page_title="导入记录｜工程知识库 v0.1.1", page_icon="📋", layout="wide")
st.title("导入记录")
st.caption("查看每次导入的状态、页数统计和错误；失败记录不会隐藏。")

try:
    records = application_database().list_import_records()
except Exception as exc:
    st.error(f"读取导入记录失败：{exc}")
    st.stop()

if not records:
    st.info("当前还没有新的导入记录；旧版本文档已经保留。")
    st.stop()

for record in records:
    status_label = {
        "pending": "等待中",
        "processing": "处理中",
        "completed": "已完成",
        "failed": "失败",
        "partially_completed": "部分完成",
    }[record.status.value]
    with st.container(border=True):
        st.markdown(f"**{record.title or record.filename} · {status_label}**")
        st.caption(
            f"原文件：{record.filename}　|　"
            f"开始：{record.started_at.astimezone():%Y-%m-%d %H:%M:%S}"
        )
        columns = st.columns(5)
        columns[0].metric("总页数", record.total_pages)
        columns[1].metric("已处理", record.processed_pages)
        columns[2].metric("文本页", record.text_pages)
        columns[3].metric("待复核", record.review_pages)
        columns[4].metric("失败页", record.failed_pages)
        if record.error_message:
            if record.error_message == "该文件已经导入":
                st.info(record.error_message)
            else:
                st.error(record.error_message)
