"""Simple document reader and per-page correction surface for ordinary users."""

from __future__ import annotations

import logging

import streamlit as st

from src.agent.local_client import LocalDocumentAgentClient
from src.agent_document_reader import AgentReadingStore
from src.runtime import (
    application_ai_provider,
    application_database,
    application_document_service,
    application_settings,
)
from src.workspace_ui import render_workspace

LOGGER = logging.getLogger(__name__)


def _agent_client() -> LocalDocumentAgentClient:
    settings = application_settings()
    return LocalDocumentAgentClient(
        database=application_database(),
        provider=application_ai_provider(),
        readings=AgentReadingStore(settings.agent_readings_dir),
        model=settings.ai_llm_model_hard,
    )


st.set_page_config(
    page_title="我的资料｜工程知识库 v0.6.0", page_icon="📖", layout="wide"
)
render_workspace("pages/17_我的资料.py")
st.title("我的资料")
st.caption("选择一份资料，逐页查看原文；发现文字有误时，可以自己修改。")

try:
    database = application_database()
    document_service = application_document_service()
    documents = database.list_documents(sort_by="imported_desc")
except Exception as exc:
    LOGGER.exception("打开资料失败")
    st.error(f"暂时无法打开资料：{exc}")
    st.stop()

if not documents:
    st.info("这里还没有资料。先添加一份 PDF、Word 或 PowerPoint 文件吧。")
    if st.button("添加资料", type="primary", use_container_width=True):
        st.switch_page("pages/1_导入资料.py")
    st.stop()

document_by_id = {document.id: document for document in documents}
requested_document = str(st.query_params.get("document", ""))
try:
    requested_document_id = int(requested_document)
except ValueError:
    requested_document_id = documents[0].id
if requested_document_id not in document_by_id:
    requested = database.get_document(requested_document_id)
    if requested is not None:
        document_by_id[requested.id] = requested
    else:
        requested_document_id = documents[0].id

document_id = st.selectbox(
    "选择资料",
    options=list(document_by_id),
    index=list(document_by_id).index(requested_document_id),
    format_func=lambda value: (
        f"{document_by_id[value].title}（{document_by_id[value].page_count} 页）"
    ),
)
document = document_by_id[document_id]
st.query_params["document"] = str(document.id)

pages = sorted(database.list_pages(document.id), key=lambda item: item.page_number)
if not pages:
    st.warning("这份资料还没有可以查看的页面。")
    st.stop()

page_by_number = {page.page_number: page for page in pages}
requested_page = str(st.query_params.get("page", ""))
try:
    initial_page = int(requested_page)
except ValueError:
    initial_page = pages[0].page_number
if initial_page not in page_by_number:
    initial_page = pages[0].page_number

st.markdown(f"### {document.title}")
st.caption(f"原文件：{document.filename}　·　共 {len(pages)} 页")

navigation = st.columns([1, 1, 3])
page_numbers = list(page_by_number)
current_index = page_numbers.index(initial_page)
if navigation[0].button(
    "← 上一页", disabled=current_index == 0, use_container_width=True
):
    st.query_params["page"] = str(page_numbers[current_index - 1])
    st.rerun()
if navigation[1].button(
    "下一页 →", disabled=current_index == len(page_numbers) - 1,
    use_container_width=True,
):
    st.query_params["page"] = str(page_numbers[current_index + 1])
    st.rerun()
page_number = navigation[2].selectbox(
    "页码",
    options=page_numbers,
    index=current_index,
    format_func=lambda value: f"第 {value} 页",
    label_visibility="collapsed",
)
st.query_params["page"] = str(page_number)
page = page_by_number[page_number]

original_text = page.ocr_text.strip() or page.extracted_text.strip()
editable_text = page.markdown_content if page.markdown_content.strip() else original_text
image_column, text_column = st.columns([1.08, 1], gap="large")
with image_column:
    st.subheader(f"原文第 {page.page_number} 页")
    if page.image_path.is_file():
        st.image(
            str(page.image_path),
            caption=f"{document.title} · 第 {page.page_number} 页",
            width="stretch",
        )
    else:
        st.warning("这一页的图片暂时无法显示。")
    with st.expander("查看 Agent 读到的原始文字"):
        st.text_area(
            "原始文字",
            value=original_text or "这一页没有识别出文字。",
            height=260,
            disabled=True,
            label_visibility="collapsed",
        )

with text_column:
    st.subheader("修改这一页的文字")
    st.caption("如果图片中的文字有识别错误，直接在下面改正，然后保存。")
    corrected_text = st.text_area(
        "修改后的文字",
        value=editable_text,
        height=560,
        key=f"simple_page_correction_{page.id}",
        placeholder="这一页没有可编辑的文字，你可以照着左边的图片补上。",
    )
    if corrected_text != editable_text:
        st.warning("修改还没有保存。")
    else:
        st.caption("当前文字已保存。" if page.markdown_content.strip() else "还没有人工修改。")
    if st.button(
        "保存修改并让 Agent 重读",
        type="primary",
        use_container_width=True,
        disabled=not corrected_text.strip(),
    ):
        try:
            document_service.save_page_markdown(
                document_id=document.id,
                page_number=page.page_number,
                markdown_content=corrected_text,
            )
        except Exception as exc:
            LOGGER.exception("保存页面修改失败：page_id=%s", page.id)
            st.error(f"修改没有保存成功：{exc}")
        else:
            progress = st.progress(0, text="正在让 Agent 重读这份资料……")

            def update_progress(current: int, total: int) -> None:
                progress.progress(
                    current / total if total else 0,
                    text=f"正在读取 {current} / {total} 页",
                )

            try:
                _agent_client().read_document(
                    document.id, progress_callback=update_progress
                )
            except Exception as exc:
                LOGGER.exception("Agent 重读页面失败：page_id=%s", page.id)
                progress.empty()
                st.warning(f"文字已经保存，但 Agent 暂时没有读完：{exc}")
            else:
                progress.progress(1.0, text=f"已读完 {len(pages)} / {len(pages)} 页")
                st.success("修改已保存，Agent 也重新读完了。")
