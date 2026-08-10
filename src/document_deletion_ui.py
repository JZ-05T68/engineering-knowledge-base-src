"""Shared Streamlit UI for permanent imported-document deletion.

Single rendering path for the document lifecycle deletion flow: read-only
impact preview (per-note-type counts, evidence items, files), then the
multi-step confirmation (checkbox + exact document title + an independent
evidence-basket confirmation whenever the document still has evidence
items). The execute button stays disabled until every required
confirmation is satisfied and no path anomaly exists. All execution goes
through :class:`DocumentDeletionService` — the UI never deletes anything
itself.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.document_deletion_service import DocumentDeletionService
from src.models import Document, DocumentAggregationImpact

LOGGER = logging.getLogger(__name__)


def _format_file_size(size_bytes: int) -> str:
    """Format a byte count with human-readable binary units."""

    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _render_aggregation_impact(impact: DocumentAggregationImpact) -> None:
    """Transparency block: which aggregation views this deletion changes.

    Display only — the existing confirmation chain (checkbox + exact title +
    evidence confirmation) already covers the risk, so no extra checkbox is
    added here.
    """

    st.markdown("**知识聚合影响**")
    if not impact.projects and not impact.tags:
        st.caption("此文档当前未出现在任何项目或标签知识聚合视图中。")
        return
    if impact.projects:
        lines = "\n".join(f"- {project.name}" for project in impact.projects)
        st.caption(f"项目聚合：{len(impact.projects)} 个")
        st.markdown(lines)
    if impact.tags:
        lines = "\n".join(f"- {tag.name}" for tag in impact.tags)
        st.caption(f"标签聚合：{len(impact.tags)} 个")
        st.markdown(lines)


def render_document_deletion_section(
    *,
    deletion_service: DocumentDeletionService,
    document: Document,
) -> None:
    """Render the impact preview and multi-step permanent-deletion flow."""

    st.markdown("**永久删除导入文档及关联数据**")
    try:
        deletion_preview = deletion_service.preview_document_deletion(document.id)
    except Exception as exc:
        LOGGER.exception("生成删除预览失败：document_id=%s", document.id)
        st.error(f"无法生成删除预览：{exc}")
        return
    st.warning(
        f"此操作不可撤销：将永久删除导入文档“{document.title}”及其全部页面、"
        "笔记、证据和派生数据。项目、标签与证据篮本身保留。"
    )
    preview_metrics = st.columns(4)
    preview_metrics[0].metric("页面", deletion_preview.page_count)
    preview_metrics[1].metric("结构化笔记", deletion_preview.note_count)
    preview_metrics[2].metric("证据项", deletion_preview.evidence_item_count)
    preview_metrics[3].metric("搜索记录", deletion_preview.search_record_count)
    st.caption(
        f"笔记明细：文档级 {deletion_preview.document_note_count} 条 · "
        f"页面级 {deletion_preview.page_note_count} 条 · "
        f"文字选区 {deletion_preview.text_selection_note_count} 条 · "
        f"图片区域 {deletion_preview.image_region_note_count} 条　|　"
        f"标签与项目关联 {deletion_preview.association_count} 条　|　"
        f"导入记录 {deletion_preview.import_record_count} 条（保留，仅解除关联）"
    )
    st.caption(
        f"独占文件：PDF {deletion_preview.pdf_file_count} 个 · "
        f"页面图片 {deletion_preview.page_image_count} 个 · "
        f"Markdown {deletion_preview.markdown_file_count} 个，"
        f"共 {_format_file_size(deletion_preview.total_size_bytes)}"
    )
    _render_aggregation_impact(deletion_preview.aggregation_impact)
    if deletion_preview.missing_files:
        st.warning(
            "以下登记文件在磁盘上缺失，删除时将跳过：\n"
            + "\n".join(f"- {path}" for path in deletion_preview.missing_files)
        )
    if deletion_preview.path_anomalies:
        st.error(
            "检测到路径异常，已禁止删除：\n"
            + "\n".join(f"- {item}" for item in deletion_preview.path_anomalies)
        )
    delete_confirmed = st.checkbox(
        "我确认永久删除此导入文档及其全部页面、笔记和派生数据。",
        key=f"doc_delete_confirm_{document.id}",
    )
    delete_title = st.text_input(
        f"请输入文档标题“{document.title}”以确认删除",
        key=f"doc_delete_title_{document.id}",
    )
    # Evidence items may carry user excerpts, snapshots and annotations, so
    # they earn their own explicit confirmation instead of hiding behind the
    # generic warning above.
    evidence_confirmed = True
    if deletion_preview.evidence_item_count > 0:
        evidence_confirmed = st.checkbox(
            f"此文档还有 {deletion_preview.evidence_item_count} 条证据篮条目。"
            "继续删除将永久删除这些摘录、快照和用户批注。",
            key=f"doc_delete_evidence_{document.id}",
        )
    if st.button(
        "永久删除此导入文档及关联数据",
        disabled=(
            not delete_confirmed
            or delete_title != document.title
            or not evidence_confirmed
            or bool(deletion_preview.path_anomalies)
        ),
        key=f"doc_delete_execute_{document.id}",
    ):
        try:
            deletion_result = deletion_service.delete_document(document.id)
        except Exception as exc:
            LOGGER.exception("删除导入文档失败：document_id=%s", document.id)
            st.error(f"删除失败：{exc}")
        else:
            st.session_state["doc_delete_reset_pending"] = True
            st.session_state["doc_delete_flash"] = (
                f"已永久删除导入文档“{document.title}”及其 "
                f"{deletion_result.preview.page_count} 个页面与全部派生数据。",
                deletion_result.cleanup_warnings,
            )
            st.query_params.clear()
            st.rerun()
