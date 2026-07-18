"""Import PDF documents with durable status and progress feedback."""

from __future__ import annotations

import logging
from pathlib import Path

import streamlit as st

from src.models import PageStatus
from src.runtime import application_document_service

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="导入资料｜工程知识库 v0.0.8", page_icon="📥", layout="wide")
st.title("导入资料")
st.caption("PDF 原件、逐页 PNG、文本和导入记录都保存在本机。")

uploaded_pdf = st.file_uploader(
    "选择 PDF 文件",
    type=["pdf"],
    accept_multiple_files=False,
    help="相同内容会按 SHA-256 识别；同名但内容不同的文件允许导入。",
)
default_title = Path(uploaded_pdf.name).stem if uploaded_pdf is not None else ""
document_title = st.text_input("文档标题", value=default_title, placeholder="默认使用 PDF 文件名")

if uploaded_pdf is None:
    st.info("选择第一份 PDF 后，系统会在本机保留原件、逐页生成 PNG，并提取已有文本层。")
    st.stop()

st.caption(f"文件：{uploaded_pdf.name}　|　大小：{uploaded_pdf.size / (1024 * 1024):.2f} MB")

if st.button("导入 PDF", type="primary"):
    progress = st.progress(0, text="正在准备导入……")

    def update_progress(current: int, total: int) -> None:
        ratio = current / total if total else 0
        progress.progress(ratio, text=f"正在处理第 {current}/{total} 页")

    try:
        result = application_document_service().import_pdf(
            file_content=uploaded_pdf.getvalue(),
            filename=uploaded_pdf.name,
            title=document_title,
            progress_callback=update_progress,
        )
    except Exception as exc:
        LOGGER.exception("导入 PDF 失败：filename=%s", uploaded_pdf.name)
        progress.empty()
        st.error(f"导入失败：{exc}")
    else:
        progress.progress(1.0, text="导入处理完成")
        if result.duplicate:
            st.warning(
                f"该文件已经导入：{result.document.title}（编号 {result.document.id}），"
                "未生成重复数据。"
            )
        else:
            failed_count = sum(page.status is PageStatus.FAILED for page in result.pages)
            review_count = sum(
                page.status in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}
                for page in result.pages
            )
            text_count = sum(
                page.processing_status in {"text_extracted", "ocr_completed"}
                for page in result.pages
            )
            message = (
                f"{result.document.title}：共 {len(result.pages)} 页，"
                f"已处理 {len(result.pages)} 页，"
                f"文本页 {text_count} 页，待复核 {review_count} 页。"
            )
            if failed_count:
                st.warning(f"部分完成。{message} 其中 {failed_count} 页处理失败。")
            else:
                st.success(f"导入完成。{message}")

st.divider()
st.markdown(
    """
**状态说明**

- `待处理`：新导入或尚未人工确认，需要继续整理。
- `草稿待复核`：已有 Markdown，但尚未人工确认完成。
- `处理失败`：单页处理出错；已完成的其他页面不会丢失。
- v0.0.8 不接入云端 OCR；原始页面图片始终保留。
"""
)
