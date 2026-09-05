"""Import a document and let the local Agent read every page."""

from __future__ import annotations

import logging

import streamlit as st

from src.agent.local_client import LocalDocumentAgentClient
from src.agent_document_reader import AgentReadingStore
from src.ocr_engine import OcrUnavailable
from src.runtime import (
    application_ai_provider,
    application_database,
    application_document_service,
    application_settings,
)
from src.workspace_ui import render_workspace

LOGGER = logging.getLogger(__name__)


def _local_agent_client() -> LocalDocumentAgentClient:
    """Build the in-process Agent from the same local database as the page."""

    settings = application_settings()
    return LocalDocumentAgentClient(
        database=application_database(),
        provider=application_ai_provider(),
        readings=AgentReadingStore(settings.agent_readings_dir),
        model=settings.ai_llm_model_hard,
    )


st.set_page_config(page_title="导入资料｜工程知识库 v0.6.0", page_icon="📥", layout="wide")
render_workspace("pages/1_导入资料.py")
st.title("添加一份资料")
st.caption("选择文件后点一下，Agent 会按页读完；原文件和每一页都保存在本机。")

uploaded_document = st.file_uploader(
    "选择 PDF、Word 或 PowerPoint 文件",
    type=["pdf", "doc", "docx", "ppt", "pptx"],
    accept_multiple_files=False,
    help="支持 PDF、DOC、DOCX、PPT 和 PPTX；相同内容不会重复保存。",
)

if uploaded_document is None:
    st.info("先选择一份资料。普通文字会直接读取，图片或手写页面会自动尝试识别。")
    st.stop()

st.caption(
    f"文件：{uploaded_document.name}　|　"
    f"大小：{uploaded_document.size / (1024 * 1024):.2f} MB"
)

if st.button("让 Agent 读这份资料", type="primary"):
    progress = st.progress(0, text="正在准备资料……")

    def update_progress(current: int, total: int) -> None:
        ratio = current / total if total else 0
        progress.progress(ratio, text=f"正在读取 {current} / {total} 页")

    try:
        document_service = application_document_service()
        result = document_service.import_document(
            file_content=uploaded_document.getvalue(),
            filename=uploaded_document.name,
        )
        for page in result.pages:
            try:
                document_service.run_page_ocr(page.id)
            except OcrUnavailable:
                # Text pages remain readable without OCR. A page with no usable
                # text will be rejected by the Agent reader below with a clear
                # page number instead of being silently marked complete.
                pass
        _local_agent_client().read_document(
            result.document.id,
            progress_callback=update_progress,
        )
    except Exception as exc:
        LOGGER.exception("Agent 读取资料失败：filename=%s", uploaded_document.name)
        progress.empty()
        st.error(f"这份资料还没有读完：{exc}")
    else:
        progress.progress(1.0, text=f"已读完 {len(result.pages)} / {len(result.pages)} 页")
        st.success("资料已经读完，可以去问 Agent 了。")
        actions = st.columns(2)
        if actions[0].button("去问 Agent", type="primary", use_container_width=True):
            st.switch_page("pages/0_知识Agent.py")
        if actions[1].button("查看识别结果（可选）", use_container_width=True):
            st.switch_page(
                "pages/17_我的资料.py",
                query_params={"document": str(result.document.id)},
            )
