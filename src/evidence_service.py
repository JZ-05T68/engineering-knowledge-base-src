"""Build traceable, copyable evidence packages from local search results."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from src.models import (
    EvidenceBasket,
    EvidenceItem,
    EvidenceTextKind,
    PageStatus,
    SearchField,
    SearchResult,
)

_UNREVIEWED_STATUSES = {
    PageStatus.PENDING,
    PageStatus.DRAFT,
    PageStatus.SKIPPED,
    PageStatus.FAILED,
}
_MARKDOWN_INLINE = re.compile(r"([\\`*_{}\[\]<>#+|])")
OCR_EVIDENCE_WARNING = (
    "本段内容来自本地 OCR 初稿，未经人工核验，请以原始页面图像为准。"
)


class EvidencePackageError(RuntimeError):
    """Raised when a trustworthy evidence package cannot be generated."""


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
        title = _markdown_inline(result.document_title.strip() or "未命名文档")
        filename = _markdown_inline(result.filename.strip() or "未记录原始文件名")
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
            lines.append(
                f"- 所属项目：{_markdown_inline('、'.join(result.projects))}"
            )
        if result.tags:
            lines.append(f"- 标签：{_markdown_inline('、'.join(result.tags))}")
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
                _markdown_block(matched_excerpt or "（没有可用的命中片段）"),
                "",
                "## 原始材料内容",
                "",
            ]
        )
        source_text, source_label, source_is_ocr = _original_material(
            result, matched_excerpt
        )
        lines.append(f"来源：{source_label}")
        if source_is_ocr:
            lines.extend(["", f"> OCR 提示：{OCR_EVIDENCE_WARNING}"])
        lines.extend(["", _markdown_block(source_text)])
        if result.markdown_content.strip():
            lines.extend(
                [
                    "",
                    "## 用户笔记",
                    "",
                    _markdown_block(result.markdown_content.strip()),
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


class EvidenceBasketPackageBuilder:
    """Build one ordered, multi-document Markdown package from a basket."""

    def build(
        self,
        basket: EvidenceBasket,
        items: Sequence[EvidenceItem],
        *,
        title: str | None = None,
        generated_at: datetime | None = None,
    ) -> str:
        """Return safe Markdown, rejecting empty or incomplete source records."""

        if not items:
            raise EvidencePackageError("证据篮为空，无法生成证据包。")
        ordered_items = tuple(sorted(items, key=lambda item: (item.position, item.id)))
        if any(item.basket_id != basket.id for item in ordered_items):
            raise EvidencePackageError("证据条目与当前证据篮不一致，已停止导出。")
        if any(item.page_number <= 0 for item in ordered_items):
            raise EvidencePackageError("证据中存在异常页码，已停止导出。")

        timestamp = generated_at or datetime.now().astimezone()
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        package_title = _markdown_inline(
            (title or basket.name or "多页面工程证据包").strip()
        )
        document_count = len({item.document_id for item in ordered_items})
        unreviewed_count = sum(
            item.review_status is not PageStatus.REVIEWED for item in ordered_items
        )
        lines = [
            f"# {package_title}",
            "",
            f"- 生成时间：{timestamp.isoformat(timespec='seconds')}",
            f"- 证据条数：{len(ordered_items)}",
            f"- 涉及文档数：{document_count}",
            f"- 证据篮：{_markdown_inline(basket.name)}（basket_id={basket.id}）",
        ]
        if unreviewed_count >= 2:
            lines.extend(
                [
                    "",
                    f"> 整体复核警告：本证据包包含 {unreviewed_count} 条未处于“人工复核完成”"
                    "状态的证据。引用前必须逐页核对原始页面图像。",
                ]
            )

        for number, item in enumerate(ordered_items, start=1):
            lines.extend(self._item_lines(number, item))
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _item_lines(number: int, item: EvidenceItem) -> list[str]:
        title = _markdown_inline(item.document_title or "未命名文档")
        filename = _markdown_inline(item.filename or "未记录原始文件名")
        projects = _markdown_inline("、".join(item.projects) or "未关联项目")
        tags = _markdown_inline("、".join(item.tags) or "未添加标签")
        lines = [
            "",
            "---",
            "",
            f"## 证据 {number}：{title} · 第 {item.page_number} 页",
            "",
            f"- 原始文件名：{filename}",
            f"- 页码：第 {item.page_number} 页",
            f"- 项目：{projects}",
            f"- 标签：{tags}",
            "- 复核状态："
            f"{item.review_status.label}（{item.review_status.value}）",
            f"- 加入时间：{item.added_at.isoformat(timespec='seconds')}",
            f"- 排序位置：{item.position}",
            f"- 来源定位：{_markdown_inline(item.source_locator)}",
        ]
        if item.document_source_path is not None:
            lines.append(
                f"- 原始 PDF 绝对路径：{_absolute_path(item.document_source_path)}"
            )
        if item.image_path is not None:
            lines.append(f"- 页面图像绝对路径：{_absolute_path(item.image_path)}")
        if item.review_status is not PageStatus.REVIEWED:
            lines.extend(
                [
                    "",
                    "> 本条复核警告：该页尚未人工复核完成，不应将以下内容视为"
                    "已经确认的事实。",
                ]
            )

        lines.extend(["", "### 原始材料", ""])
        if item.text_kind is EvidenceTextKind.ORIGINAL:
            lines.extend(
                [
                    "可信度：该选区已在加入时匹配当前 PDF 文本层或 OCR 原始文本。",
                ]
            )
            if item.from_ocr_text:
                lines.extend(["", f"> OCR 提示：{OCR_EVIDENCE_WARNING}"])
            lines.extend(["", _markdown_block(item.evidence_text)])
        else:
            lines.append("（本条选区未匹配原始文本；不得视为已验证原文。）")

        lines.extend(["", "### 用户摘录", ""])
        if item.text_kind is EvidenceTextKind.USER_EXCERPT:
            lines.extend(
                [
                    "可信度：用户摘录 / 整理内容，未经原文匹配确认。",
                    "",
                    _markdown_block(item.evidence_text),
                ]
            )
        else:
            lines.append("（无；本条选区已归入原始材料。）")

        lines.extend(
            [
                "",
                "### 用户笔记",
                "",
                _markdown_block(item.user_note) if item.user_note else "（无）",
                "",
                f"### {item.context_kind.label}",
                "",
                _markdown_block(item.context) if item.context else "（无）",
            ]
        )
        return lines


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


def _original_material(
    result: SearchResult, matched_excerpt: str
) -> tuple[str, str, bool]:
    content = result.content.strip()
    if (
        SearchField.OCR_TEXT in result.match_fields
        and result.ocr_text.strip()
        and content == result.ocr_text.strip()
    ):
        return matched_excerpt or result.ocr_text.strip(), "OCR 文本", True
    if (
        SearchField.EXTRACTED_TEXT in result.match_fields
        and result.extracted_text.strip()
        and content == result.extracted_text.strip()
    ):
        return matched_excerpt or result.extracted_text.strip(), "PDF 文本层", False
    if result.ocr_text.strip():
        return result.ocr_text.strip(), "OCR 文本", True
    if result.extracted_text.strip():
        return result.extracted_text.strip(), "PDF 文本层", False
    return "（本页没有可用的原始文本，请核对页面图像。）", "页面图像", False


def _markdown_inline(value: str) -> str:
    """Escape user-controlled text used in Markdown headings and list metadata."""

    compact = " ".join((value or "").split())
    return _MARKDOWN_INLINE.sub(r"\\\1", compact)


def _markdown_block(value: str) -> str:
    """Use an indented block so backticks/headings in user text stay inert."""

    text = (value or "").rstrip()
    return "\n".join(f"    {line}" for line in text.splitlines() or [""])


__all__ = [
    "OCR_EVIDENCE_WARNING",
    "EvidenceBasketPackageBuilder",
    "EvidencePackageBuilder",
    "EvidencePackageError",
]
