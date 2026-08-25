"""Build citation-grounded prompt packages from knowledge objects (v0.5.2).

The builder produces a copyable Chinese prompt for external AI tools from one
or more knowledge objects plus the text of their source links. It performs no
I/O and never calls any AI provider; source texts are supplied by the caller
(a UI page usually resolves them from the local database).

Grounding policy reuses :data:`src.prompt_builder.GROUNDING_RULES` and adds
knowledge-specific rules: knowledge-object content is user-organized knowledge
and must be checked against its sources, unconfirmed objects are explicitly
not confirmed facts, and confirmation that became stale after a content edit
is reported as such. The fingerprint state machine (Phase 2C) will extend the
source annotations; this Phase 2B version keeps the honest baseline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from src.models import (
    KnowledgeConfirmationStatus,
    KnowledgeLifecycle,
    KnowledgeObjectSource,
    KnowledgeObjectView,
)
from src.prompt_builder import DEFAULT_QUESTION, GROUNDING_RULES

MAX_SOURCE_TEXT_CHARS = 20_000

_KNOWLEDGE_EXTRA_RULES = (
    "6. “知识对象”的内容是用户长期整理的知识，可能包含个人判断；"
    "每个事实性结论都应优先依据其“来源”核验，无法核验时明确说明。\n"
    "7. 状态为“未确认”的知识对象仅供参考，不应视为已确认的事实；"
    "确认基于旧版的知识对象，其正文在确认后又被修改过，引用时需注明。"
)

_UNCONFIRMED_WARNING = (
    "复核提示：本知识对象尚未经用户确认，不应视为已确认事实。"
)

_STALE_CONFIRMATION_WARNING = (
    "复核提示：本知识对象的正文在用户确认之后又被修改过，"
    "当前内容尚未重新确认，引用时需注明。"
)

_MISSING_SOURCE_NOTICE = "（没有可用的有效来源）"


class KnowledgePromptError(ValueError):
    """Raised when a knowledge prompt package cannot be built safely."""


class KnowledgePromptBuilder:
    """Create a copyable grounded prompt from knowledge objects and sources."""

    def build(
        self,
        question: str,
        views: Sequence[KnowledgeObjectView],
        *,
        source_texts: Mapping[tuple[str, int], str] | None = None,
        generated_at: datetime | None = None,
    ) -> str:
        """Return a citation-grounded Chinese prompt package.

        ``source_texts`` maps ``(source_type.value, source_id)`` to the
        display text of that source. Sources missing from the mapping are
        still listed as citations but carry a "no text available" notice.
        Empty ``views`` are rejected: an empty prompt package would silently
        turn into an ungrounded chat, which this feature must never produce.
        """

        if not views:
            raise KnowledgePromptError("没有可生成提示词的知识对象。")
        cleaned_question = question.strip() or DEFAULT_QUESTION
        timestamp = generated_at or datetime.now().astimezone()
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        texts = source_texts or {}
        blocks = [
            self._format_object(number, view, texts)
            for number, view in enumerate(views, start=1)
        ]
        return (
            "# 任务\n"
            "请回答下方的用户问题。\n\n"
            "# 回答规则\n"
            f"{GROUNDING_RULES}\n"
            f"{_KNOWLEDGE_EXTRA_RULES}\n\n"
            f"# 用户问题\n{cleaned_question}\n\n"
            f"# 知识对象（知识片段）\n" + "\n\n".join(blocks) + "\n"
        )

    @classmethod
    def _format_object(
        cls,
        number: int,
        view: KnowledgeObjectView,
        source_texts: Mapping[tuple[str, int], str],
    ) -> str:
        knowledge_object = view.knowledge_object
        lines = [
            f"[知识对象 {number}]",
            f"标题：{knowledge_object.title}",
            f"类型：{knowledge_object.kind.label}",
            f"形成依据：{knowledge_object.epistemic_basis.label}",
            f"重要程度：{knowledge_object.importance.label}",
            f"生命周期：{knowledge_object.lifecycle.label}（{knowledge_object.lifecycle.value}）",
            (
                f"确认状态：已确认（第 {knowledge_object.confirmed_revision} 版，"
                f"当前第 {knowledge_object.current_revision} 版）"
                if knowledge_object.confirmation_is_current
                else (
                    f"确认状态：确认基于旧版（第 {knowledge_object.confirmed_revision} 版，"
                    f"当前第 {knowledge_object.current_revision} 版）"
                    if knowledge_object.confirmation_is_stale
                    else "确认状态：未确认"
                )
            ),
        ]
        if knowledge_object.confirmation_status is KnowledgeConfirmationStatus.UNCONFIRMED:
            lines.extend(["", f"> {_UNCONFIRMED_WARNING}"])
        elif knowledge_object.confirmation_is_stale:
            lines.extend(["", f"> {_STALE_CONFIRMATION_WARNING}"])
        if knowledge_object.lifecycle is not KnowledgeLifecycle.ACTIVE:
            lines.append(
                f"> 注意：该对象生命周期为“{knowledge_object.lifecycle.label}”，"
                "仅作历史参考。"
            )
        if knowledge_object.superseded_by_ko_id is not None:
            lines.append(f"> 该对象已被知识对象 {knowledge_object.superseded_by_ko_id} 替代。")
        lines.extend(["", "内容：", knowledge_object.content.strip(), "", "来源："])
        if not view.sources:
            lines.append(_MISSING_SOURCE_NOTICE)
        else:
            for source_view in view.sources:
                lines.extend(cls._source_lines(source_view.source, source_texts))
        return "\n".join(lines)

    @classmethod
    def _source_lines(
        cls,
        source: KnowledgeObjectSource,
        source_texts: Mapping[tuple[str, int], str],
    ) -> list[str]:
        key = (source.source_type.value, source.source_id)
        label = source.source_type.label
        note = f"（{source.source_note}）" if source.source_note.strip() else ""
        lines = [f"- {label} {source.source_id}{note}"]
        text = source_texts.get(key, "")
        if text.strip():
            truncated = _truncate_source_text(text)
            lines.append("  来源内容：")
            lines.extend(f"    {line}" for line in truncated.splitlines() or [""])
        else:
            lines.append("  （该来源没有可用的文本内容。）")
        return lines


def _truncate_source_text(text: str) -> str:
    if len(text) <= MAX_SOURCE_TEXT_CHARS:
        return text.rstrip()
    return (
        text[:MAX_SOURCE_TEXT_CHARS].rstrip()
        + f"\n（来源文本过长，仅保留前 {MAX_SOURCE_TEXT_CHARS} 个字符。）"
    )


__all__ = [
    "MAX_SOURCE_TEXT_CHARS",
    "KnowledgePromptBuilder",
    "KnowledgePromptError",
]
