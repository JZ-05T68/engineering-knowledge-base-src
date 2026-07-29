"""Create and manage local engineering projects and their material links."""

from __future__ import annotations

import logging

import streamlit as st

from src.runtime import application_database

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="项目管理｜工程知识库 v0.2.3", page_icon="🗂️", layout="wide")
st.title("项目管理")
st.caption("项目用于组织学习与竞赛方向；删除项目不会删除其中的资料。")

try:
    database = application_database()
    documents = database.list_documents()
except Exception as exc:
    st.error(f"读取项目数据失败：{exc}")
    st.stop()

with st.expander("创建项目", expanded=not database.list_projects()):
    with st.form("create_project", clear_on_submit=True):
        new_name = st.text_input("项目名称", placeholder="例如：电赛电源方向")
        new_description = st.text_area("项目描述")
        new_status = st.selectbox("项目状态", ["active", "paused", "completed"])
        create_submitted = st.form_submit_button("创建项目", type="primary")
    if create_submitted:
        try:
            database.create_project(new_name, new_description, new_status)
        except Exception as exc:
            LOGGER.exception("创建项目失败")
            st.error(f"创建项目失败：{exc}")
        else:
            st.success("项目已创建。")
            st.rerun()

projects = database.list_projects()
if not projects:
    st.info("还没有项目。使用上方表单创建第一个本地项目，再关联已有文档或页面。")
    st.stop()

project_by_id = {project.id: project for project in projects}
project_id = st.selectbox(
    "选择项目",
    options=list(project_by_id),
    format_func=lambda value: (
        f"{project_by_id[value].name}（{project_by_id[value].document_count} 文档 / "
        f"{project_by_id[value].page_count} 页面）"
    ),
)
project = project_by_id[project_id]

with st.form(f"edit_project_{project.id}"):
    edit_columns = st.columns(2)
    project_name = edit_columns[0].text_input("项目名称", value=project.name)
    project_status = edit_columns[1].selectbox(
        "状态",
        ["active", "paused", "completed"],
        index=["active", "paused", "completed"].index(project.status)
        if project.status in {"active", "paused", "completed"}
        else 0,
    )
    project_description = st.text_area("描述", value=project.description)
    save_project = st.form_submit_button("保存项目资料", type="primary")
if save_project:
    try:
        database.update_project(
            project.id,
            name=project_name,
            description=project_description,
            status=project_status,
        )
    except Exception as exc:
        st.error(f"保存项目失败：{exc}")
    else:
        st.success("项目资料已更新。")
        st.rerun()

linked_documents = database.list_project_documents(project.id)
selected_document_ids = st.multiselect(
    "关联文档",
    options=[document.id for document in documents],
    default=[document.id for document in linked_documents],
    format_func=lambda value: next(
        document.title for document in documents if document.id == value
    ),
)
if st.button("保存文档关联"):
    try:
        for document in documents:
            current_projects = database.get_document_projects(document.id)
            desired = {item.id for item in current_projects}
            if document.id in selected_document_ids:
                desired.add(project.id)
            else:
                desired.discard(project.id)
            database.set_document_projects(document.id, sorted(desired))
    except Exception as exc:
        st.error(f"保存文档关联失败：{exc}")
    else:
        st.success("文档关联已保存。")
        st.rerun()

st.subheader("项目资料")
linked_pages = database.list_project_pages(project.id)
left, right = st.columns(2)
with left:
    st.markdown("**关联文档**")
    for document in linked_documents:
        if st.button(
            document.title,
            key=f"project_document_{document.id}",
            use_container_width=True,
        ):
            st.query_params["document"] = str(document.id)
            st.switch_page("pages/2_浏览资料.py")
    if not linked_documents:
        st.caption("该项目尚未关联文档。可在上方选择并保存关联。")
with right:
    st.markdown("**直接关联页面**")
    for page in linked_pages:
        document = database.get_document(page.document_id)
        if document and st.button(
            f"{document.title} · 第 {page.page_number} 页",
            key=f"project_page_{page.id}",
            use_container_width=True,
        ):
            st.query_params.update(
                {"document": str(document.id), "page": str(page.page_number)}
            )
            st.switch_page("pages/2_浏览资料.py")
    if not linked_pages:
        st.caption("该项目尚未直接关联页面。可在浏览资料页为页面添加项目。")

with st.expander("删除项目"):
    confirm = st.checkbox(
        f"确认删除项目“{project.name}”及其关联（不会删除资料）",
        key=f"confirm_delete_project_{project.id}",
    )
    if st.button("删除项目", disabled=not confirm, key=f"delete_project_{project.id}"):
        try:
            database.delete_project(project.id)
        except Exception as exc:
            st.error(f"删除项目失败：{exc}")
        else:
            st.success("项目及关联已删除，文档、页面和笔记均未删除。")
            st.rerun()
