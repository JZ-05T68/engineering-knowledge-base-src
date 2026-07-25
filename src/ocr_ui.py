"""Pure feedback mapping for the single-page OCR controls on the review page.

This module keeps the Streamlit page thin: every OCR outcome maps to one
stable ``(level, message)`` pair here, with no I/O, no state, and no
knowledge of widgets. Levels are Streamlit message names (``success``,
``info``, ``warning``, ``error``) so the page can dispatch with
``getattr(st, level)``.
"""

from __future__ import annotations

from src.document_service import PageOcrOutcome

__all__ = [
    "OCR_DRAFT_HEADING",
    "OCR_DRAFT_HINT",
    "OCR_RUNNING_HINT",
    "page_ocr_feedback",
    "page_ocr_unavailable_feedback",
]

OCR_DRAFT_HEADING = "OCR 初稿（未经人工核验）"
OCR_DRAFT_HINT = "本段内容由本地 OCR 自动识别，可能存在错字、漏字或顺序错误，请以原始页面图像为准。"
OCR_RUNNING_HINT = "正在执行本地 OCR，首次加载可能较慢……"


def page_ocr_feedback(outcome: PageOcrOutcome, ocr_text: str) -> tuple[str, str]:
    """Map one ``run_page_ocr`` outcome to a Streamlit message level and text.

    ``ocr_text`` is the page's recognized text after the attempt; an empty
    completed result is still a completion but must say so plainly. No
    message claims accuracy, review completion, or contains local paths.
    """

    if outcome is PageOcrOutcome.COMPLETED:
        if ocr_text.strip():
            return "success", "本页 OCR 已完成。"
        return "info", "本页 OCR 已执行完成，但未识别到有效文字。"
    if outcome is PageOcrOutcome.NOT_ELIGIBLE:
        return (
            "info",
            "当前页面不符合 OCR 条件：可能已有可靠文本层、已有 OCR 结果，或页面图像不可用。",
        )
    return "error", "本页 OCR 执行失败，错误已记录；原始资料和人工内容未被修改。"


def page_ocr_unavailable_feedback() -> tuple[str, str]:
    """Return the stable message for a missing or unusable local OCR engine."""

    return "warning", "本地 OCR 引擎不可用，请确认项目 OCR 依赖已完整安装。"
