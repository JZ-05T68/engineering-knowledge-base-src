"""Build traceable, copyable evidence packages from local search results."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.models import PageStatus, SearchField, SearchResult

_UNREVIEWED_STATUSES = {
    PageStatus.PENDING,
    PageStatus.DRAFT,
    PageStatus.SKIPPED,
    PageStatus.FAILED,
}


class EvidencePackageBuilder:
    """Create stable Markdown evidence without network links or hidden context."""

    def build(
        self,
        result: SearchResult,
        *,
        selected_excerpt: str | None = None,
        generated_at: datetime | None = None,
    ) -> str:
        """Return a traceable page-level evidence package in Markdown format."""

        timestamp = generated_at or datetime.now().astimezone()
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        title = result.document_title.strip() or "未命名文档"
        filename = result.filename.strip() or "未记录原始文件名"
        match_fields = result.match_fields or _fallback_match_fields(result)
        matched_excerpt = (
            selected_excerpt.strip()
            if selected_excerpt and selected_excerpt.strip()
            else result.snippet.strip() or result.content.strip()
        )
        lines = [
            "# 工程知识库引用证据包",
            "",
            f"- 文档标题：{title}",
            f"- 原始文件名：{filename}",
            f"- 页码：第 {result.page_number} 页",
            f"- 页面复核状态：{result.status.label}（{result.status.value}）",
        ]
        if result.projects:
            lines.append(f"- 所属项目：{'、'.join(result.projects)}")
        if result.tags:
            lines.append(f"- 标签：{'、'.join(result.tags)}")
        lines.extend(_path_lines(result))
        lines.extend(
            [
                "- 内部引用："
                f"document_id={result.document_id}; page_id={result.page_id}; "
                f"page_number={result.page_number}"
                + (
                    f"; document_sha256={result.document_sha256}"
                    if result.document_sha256
                    else ""
                ),
                f"- 生成时间：{timestamp.isoformat(timespec='seconds')}",
            ]
        )
        if result.status in _UNREVIEWED_STATUSES:
            lines.extend(
                [
                    "",
                    "> 复核提示：本页尚未处于“人工复核完成”状态。"
                    "以下文本仅作为待核对材料，不应视为已经确认的事实。",
                ]
            )
        lines.extend(
            [
                "",
                "## 命中片段",
                "",
                "命中来源：" + "、".join(field.label for field in match_fields),
                "",
                matched_excerpt or "（没有可用的命中片段）",
                "",
                "## 原始材料内容",
                "",
            ]
        )
        source_text, source_label = _original_material(result, matched_excerpt)
        lines.extend([f"来源：{source_label}", "", source_text])
        if result.markdown_content.strip():
            lines.extend(
                [
                    "",
                    "## 用户笔记",
                    "",
                    result.markdown_content.strip(),
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


def _path_lines(result: SearchResult) -> list[str]:
    lines: list[str] = []
    if result.document_source_path is not None:
        lines.append(
            f"- 原始 PDF 绝对路径：{_absolute_path(result.document_source_path)}"
        )
    if str(result.image_path):
        lines.append(f"- 页面图像绝对路径：{_absolute_path(result.image_path)}")
    return lines


def _absolute_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _fallback_match_fields(result: SearchResult) -> tuple[SearchField, ...]:
    if (
        result.markdown_content.strip()
        and result.content.strip() == result.markdown_content.strip()
    ):
        return (SearchField.MARKDOWN,)
    if result.ocr_text.strip() and result.content.strip() == result.ocr_text.strip():
        return (SearchField.OCR_TEXT,)
    if result.extracted_text.strip():
        return (SearchField.EXTRACTED_TEXT,)
    return (SearchField.DOCUMENT_TITLE,)


def _original_material(result: SearchResult, matched_excerpt: str) -> tuple[str, str]:
    content = result.content.strip()
    if (
        SearchField.OCR_TEXT in result.match_fields
        and result.ocr_text.strip()
        and content == result.ocr_text.strip()
    ):
        return matched_excerpt or result.ocr_text.strip(), "OCR 文本"
    if (
        SearchField.EXTRACTED_TEXT in result.match_fields
        and result.extracted_text.strip()
        and content == result.extracted_text.strip()
    ):
        return matched_excerpt or result.extracted_text.strip(), "PDF 文本层"
    if result.ocr_text.strip():
        return result.ocr_text.strip(), "OCR 文本"
    if result.extracted_text.strip():
        return result.extracted_text.strip(), "PDF 文本层"
    return "（本页没有可用的原始文本，请核对页面图像。）", "页面图像"


__all__ = ["EvidencePackageBuilder"]
