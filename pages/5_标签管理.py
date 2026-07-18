"""Create, inspect, and safely delete reusable tags."""

from __future__ import annotations

import logging

import streamlit as st

from src.runtime import application_database

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="标签管理｜工程知识库 v0.0.2", page_icon="🏷️", layout="wide")
st.title("标签管理")
st.caption("同名标签会自动复用；删除标签只删除关联，不删除文档、页面或笔记。")

try:
    database = application_database()
except Exception as exc:
    st.error(f"数据库初始化失败：{exc}")
    st.stop()

with st.form("create_tag", clear_on_submit=True):
    name = st.text_input("新标签名称", placeholder="例如：PID、STM32、运算放大器")
    submitted = st.form_submit_button("创建或复用标签", type="primary")
if submitted:
    try:
        tag = database.create_tag(name)
    except Exception as exc:
        LOGGER.exception("创建标签失败")
        st.error(f"创建标签失败：{exc}")
    else:
        st.success(f"标签“{tag.name}”可用。")
        st.rerun()

tags = database.list_tags()
if not tags:
    st.info("还没有标签。")
    st.stop()

tag_by_id = {tag.id: tag for tag in tags}
tag_id = st.selectbox(
    "查看标签",
    options=list(tag_by_id),
    format_func=lambda value: f"{tag_by_id[value].name}（使用 {tag_by_id[value].usage_count} 次）",
)
tag = tag_by_id[tag_id]
documents = database.list_documents(tag_ids=[tag.id])
pages = database.list_pages_by_tag(tag.id)

metrics = st.columns(3)
metrics[0].metric("总使用次数", tag.usage_count)
metrics[1].metric("关联文档", len(documents))
metrics[2].metric("直接关联页面", len(pages))

left, right = st.columns(2)
with left:
    st.subheader("相关文档")
    if documents:
        for document in documents:
            if st.button(
                document.title,
                key=f"tag_document_{document.id}",
                use_container_width=True,
            ):
                st.query_params["document"] = str(document.id)
                st.switch_page("pages/2_浏览资料.py")
    else:
        st.caption("暂无直接关联文档。")
with right:
    st.subheader("相关页面")
    if pages:
        for page in pages:
            document = database.get_document(page.document_id)
            if document and st.button(
                f"{document.title} · 第 {page.page_number} 页",
                key=f"tag_page_{page.id}",
                use_container_width=True,
            ):
                st.query_params.update(
                    {"document": str(document.id), "page": str(page.page_number)}
                )
                st.switch_page("pages/2_浏览资料.py")
    else:
        st.caption("暂无直接关联页面。")

with st.expander("删除标签"):
    confirm = st.checkbox(
        f"确认删除标签“{tag.name}”及其关联（不会删除资料）",
        key=f"confirm_delete_tag_{tag.id}",
    )
    if st.button("删除标签", disabled=not confirm, key=f"delete_tag_{tag.id}"):
        try:
            database.delete_tag(tag.id)
        except Exception as exc:
            st.error(f"删除标签失败：{exc}")
        else:
            st.success("标签及其关联已删除，资料未受影响。")
            st.rerun()
