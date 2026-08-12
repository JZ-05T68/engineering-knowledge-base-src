"""Build citation-grounded prompt packages from confirmed basket evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.evidence_service import EvidencePackageError
from src.models import EvidenceConfirmationStatus, EvidenceItem, EvidenceType
from src.prompt_builder import DEFAULT_QUESTION, GROUNDING_RULES

MAX_PAGE_TEXT_CHARS = 20_000

NO_CONFIRMED_EVIDENCE_MESSAGE = (
    "当前没有已确认的证据。请先确认至少一条证据后再生成引用提示词包。"
)

_EVIDENCE_EXTRA_RULES = (
    "6. 只有标为“来源内容”的文字可以作为事实依据；“用户备注”仅用于理解"
    "用户意图，不等同于来源事实。\n"
    "7. 图片区域证据不包含图片像素；不得根据区域坐标、图像尺寸或用户备注"
    "猜测图片内容。"
)

_REGION_NO_PIXEL_NOTICE = (
    "这是一条图片区域证据；当前纯文本提示词包不包含图片像素。"
    "如需模型分析该图片区域，应由用户另行提供对应图片；"
    "不得根据区域坐标、图像尺寸或用户备注猜测图片内容。"
)

_PAGE_NO_TEXT_NOTICE = "该整页证据没有可用于纯文本提示词包的文本内容。"


class EvidencePromptBuilder:
    """Create a copyable grounded prompt from validated, confirmed evidence.

    The grounding policy is the one shared with ``PromptBuilder``; this builder
    only adds the evidence-specific rules for user notes and image regions.
    It never fabricates region content and never treats user notes as source
    material.
    """

    def build(
        self,
        question: str,
        items: Sequence[EvidenceItem],
        *,
        page_texts: Mapping[int, str] | None = None,
    ) -> str:
        """Return a copyable Chinese prompt built only from confirmed evidence.

        ``page_texts`` maps a whole-page evidence item id to the page's current
        source text, already re-fetched after source validation by the caller.
        Unconfirmed items are excluded even if the caller passes them.
        """

        confirmed = [
            item
            for item in items
            if item.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
        ]
        if not confirmed:
            raise EvidencePackageError(NO_CONFIRMED_EVIDENCE_MESSAGE)
        ordered = sorted(confirmed, key=lambda item: (item.position, item.id))
        texts = page_texts or {}

        cleaned_question = question.strip() or DEFAULT_QUESTION
        blocks = [
            self._format_item(number, item, texts)
            for number, item in enumerate(ordered, start=1)
        ]
        return (
            "# 任务\n"
            "请回答下方的用户问题。\n\n"
            "# 回答规则\n"
            f"{GROUNDING_RULES}\n"
            f"{_EVIDENCE_EXTRA_RULES}\n\n"
            f"# 用户问题\n{cleaned_question}\n\n"
            f"# 已确认的证据（知识片段）\n" + "\n\n".join(blocks) + "\n"
        )

    @classmethod
    def _format_item(
        cls,
        number: int,
        item: EvidenceItem,
        page_texts: Mapping[int, str],
    ) -> str:
        title = item.document_title.strip() or item.filename.strip() or "未命名文档"
        lines = [
            f"[证据 {number}]",
            f"类型：{item.evidence_type.label}",
            f"来源：【{title}，第{item.page_number}页】"
            f"（原始文件：{item.filename.strip() or '未记录原始文件名'}）",
            f"确认状态：{item.confirmation_status.label}",
        ]
        if item.evidence_type is EvidenceType.IMAGE_REGION:
            lines.extend(cls._region_lines(item))
        elif item.evidence_type is EvidenceType.PAGE:
            lines.extend(cls._page_lines(item, page_texts.get(item.id, "")))
        else:
            lines.extend(cls._text_selection_lines(item))
        if item.user_note.strip():
            lines.extend(["用户备注：", item.user_note.strip()])
        return "\n".join(lines)

    @staticmethod
    def _text_selection_lines(item: EvidenceItem) -> list[str]:
        return [
            f"可信度：{item.text_kind.label}",
            "来源内容：",
            item.evidence_text.strip(),
        ]

    @staticmethod
    def _page_lines(item: EvidenceItem, page_text: str) -> list[str]:
        text = page_text.strip()
        if not text:
            return [f"说明：{_PAGE_NO_TEXT_NOTICE}"]
        if len(text) > MAX_PAGE_TEXT_CHARS:
            text = (
                text[:MAX_PAGE_TEXT_CHARS].rstrip()
                + f"\n（整页文本过长，仅保留前 {MAX_PAGE_TEXT_CHARS} 个字符。）"
            )
        return ["来源内容（当前整页文本）：", text]

    @staticmethod
    def _region_lines(item: EvidenceItem) -> list[str]:
        return [
            "来源定位：页面图像 "
            f"{item.region_image_width}×{item.region_image_height} 像素；"
            f"区域坐标（原图像素）：({item.region_x0}, {item.region_y0}) - "
            f"({item.region_x1}, {item.region_y1})",
            f"说明：{_REGION_NO_PIXEL_NOTICE}",
        ]


__all__ = [
    "MAX_PAGE_TEXT_CHARS",
    "NO_CONFIRMED_EVIDENCE_MESSAGE",
    "EvidencePromptBuilder",
]
