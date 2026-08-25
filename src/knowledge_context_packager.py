"""Fail-closed RAG context packager (v0.5.3 Phase 2-B, V53-ADR-02).

``KnowledgeContextPackager`` is not an agent and not a selector. It receives
already-chosen :class:`ContextItem` projections and assembles them into one
self-contained context package with two lossless views: machine-readable JSON
and human-readable Markdown.

Fail-closed rules (frozen in Phase 0):

- an empty selection is rejected;
- after default exclusions (archived / superseded) an empty remainder is
  rejected;
- missing sources are kept but explicitly annotated, never silently dropped;
- a projection failure is the caller's failure — the packager never repairs
  or fabricates data;
- any internal error aborts without emitting a half-built package.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from src.models import ContextItem, ContextItemType

DEFAULT_MAX_ITEMS = 100
DEFAULT_MAX_CHARS_PER_ITEM = 20_000
DEFAULT_MAX_TOTAL_CHARS = 120_000
_CONTENT_TRUNCATED_MARK = "\n（内容过长，已截断。）"


class KnowledgeContextError(ValueError):
    """Raised when a context package cannot be built safely."""


@dataclass(frozen=True, slots=True)
class ExcludedContextItem:
    """One item the packager excluded, with a stable, human-readable reason."""

    stable_id: str
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextWarning:
    """One non-fatal, explicitly surfaced concern for an included item."""

    stable_id: str
    message: str


@dataclass(frozen=True, slots=True)
class KnowledgeContextPackage:
    """The assembled, immutable RAG context package."""

    package_uuid: str
    generated_at: str
    kb_uuid: str
    app_version: str
    question: str
    items: tuple[ContextItem, ...]
    citations: tuple[tuple[str, str], ...]
    excluded: tuple[ExcludedContextItem, ...]
    warnings: tuple[ContextWarning, ...]

    def to_json(self) -> str:
        """Return the machine-readable JSON view of this package."""

        payload = {
            "manifest": {
                "package_uuid": self.package_uuid,
                "generated_at": self.generated_at,
                "kb_uuid": self.kb_uuid,
                "app_version": self.app_version,
                "question": self.question,
                "item_count": len(self.items),
                "excluded_count": len(self.excluded),
                "enums": {
                    "type": [item.value for item in ContextItemType],
                },
            },
            "items": [_item_to_dict(item) for item in self.items],
            "citations": {stable_id: index for stable_id, index in self.citations},
            "excluded": [
                {"stable_id": item.stable_id, "title": item.title, "reason": item.reason}
                for item in self.excluded
            ],
            "warnings": [
                {"stable_id": warning.stable_id, "message": warning.message}
                for warning in self.warnings
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def to_markdown(self) -> str:
        """Return the human-readable Markdown view of this package."""

        lines = ["# 知识上下文包", ""]
        lines.append(f"- 生成时间：{self.generated_at}")
        lines.append(f"- 知识库稳定标识：{self.kb_uuid}")
        if self.question.strip():
            lines.append(f"- 用户问题：{self.question.strip()}")
        lines.append(f"- 包含：{len(self.items)} 项 / 排除：{len(self.excluded)} 项")
        for index, item in enumerate(self.items, start=1):
            lines.extend(_item_markdown_block(index, item))
        for warning in self.warnings:
            lines.append(f"> 复核提示【{warning.stable_id}】：{warning.message}")
        if self.excluded:
            lines.append("")
            lines.append("## 排除项")
            for excluded in self.excluded:
                lines.append(
                    f"- {excluded.title}（{excluded.stable_id}）：{excluded.reason}"
                )
        return "\n".join(lines)


def _item_to_dict(item: ContextItem) -> dict[str, object]:
    return {
        "type": item.type.value,
        "local_id": item.local_id,
        "stable_id": item.stable_id,
        "title": item.title,
        "content": item.content,
        "kind": item.kind,
        "kind_label": item.kind_label,
        "status": item.status,
        "status_label": item.status_label,
        "importance": item.importance,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "revision_ref": item.revision_ref,
        "source_anchors": [
            {
                "anchor_type": anchor.anchor_type,
                "anchor_id": anchor.anchor_id,
                "anchor_label": anchor.anchor_label,
                "fingerprint_state": anchor.fingerprint_state,
            }
            for anchor in item.source_anchors
        ],
        "relation_refs": [
            {
                "relation_type": relation.relation_type,
                "relation_label": relation.relation_label,
                "direction": relation.direction,
                "target_stable_id": relation.target_stable_id,
            }
            for relation in item.relation_refs
        ],
    }


def _item_markdown_block(index: int, item: ContextItem) -> list[str]:
    lines = ["", f"## [{index}] {item.title}"]
    lines.append(f"- 类型：{item.type.label}（{item.type.value}）")
    if item.kind_label:
        lines.append(f"- 类别：{item.kind_label}")
    lines.append(f"- 状态：{item.status_label}（{item.status}）")
    lines.append(f"- 稳定标识：{item.stable_id}")
    if item.revision_ref:
        lines.append(f"- 修订：{item.revision_ref}")
    if item.importance:
        lines.append(f"- 重要程度：{item.importance}")
    if item.updated_at is not None:
        lines.append(f"- 更新时间：{item.updated_at.isoformat()}")
    if item.source_anchors:
        lines.append("- 来源：")
        for anchor in item.source_anchors:
            lines.append(
                f"  - {anchor.anchor_label}（指纹状态：{anchor.fingerprint_state}）"
            )
    else:
        lines.append("- 来源：**无来源**")
    if item.relation_refs:
        lines.append("- 引用关系：")
        for relation in item.relation_refs:
            lines.append(
                f"  - {relation.direction} {relation.relation_label} "
                f"→ {relation.target_stable_id}"
            )
    lines.append("")
    lines.append("正文：")
    lines.append(item.content)
    return lines


class KnowledgeContextPackager:
    """Assemble explicitly chosen ContextItems into a RAG context package."""

    def __init__(
        self,
        *,
        kb_uuid: str = "",
        app_version: str = "",
        max_items: int = DEFAULT_MAX_ITEMS,
        max_chars_per_item: int = DEFAULT_MAX_CHARS_PER_ITEM,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items 必须大于 0")
        if max_chars_per_item < 100:
            raise ValueError("max_chars_per_item 不能小于 100")
        if max_total_chars < max_chars_per_item:
            raise ValueError("max_total_chars 不能小于 max_chars_per_item")
        self._kb_uuid = kb_uuid
        self._app_version = app_version
        self._max_items = max_items
        self._max_chars_per_item = max_chars_per_item
        self._max_total_chars = max_total_chars

    def build(
        self,
        items: Sequence[ContextItem],
        *,
        question: str = "",
        include_archived: bool = False,
        include_superseded: bool = False,
    ) -> KnowledgeContextPackage:
        """Assemble the package or raise ``KnowledgeContextError`` (fail-closed)."""

        if not items:
            raise KnowledgeContextError("空上下文：没有选择任何知识，拒绝生成。")
        if len(items) > self._max_items:
            raise KnowledgeContextError(
                f"上下文项数量 {len(items)} 超过上限 {self._max_items}。"
            )

        kept: list[ContextItem] = []
        excluded: list[ExcludedContextItem] = []
        for item in items:
            reason = _exclusion_reason(item, include_archived, include_superseded)
            if reason is not None:
                excluded.append(
                    ExcludedContextItem(
                        stable_id=item.stable_id, title=item.title, reason=reason
                    )
                )
            else:
                kept.append(item)
        if not kept:
            raise KnowledgeContextError(
                "空上下文：所有知识项均被排除（归档或已替代），拒绝生成。"
            )

        truncated = [_truncate_item(item, self._max_chars_per_item) for item in kept]
        total_chars = sum(len(item.title) + len(item.content) for item in truncated)
        if total_chars > self._max_total_chars:
            raise KnowledgeContextError(
                f"上下文总字符数 {total_chars} 超过上限 {self._max_total_chars}。"
            )

        warnings = tuple(
            ContextWarning(stable_id=item.stable_id, message=message)
            for item in truncated
            for message in _item_warnings(item)
        )
        citations = tuple(
            (item.stable_id, f"#{index}") for index, item in enumerate(truncated, start=1)
        )
        return KnowledgeContextPackage(
            package_uuid=str(uuid.uuid4()),
            generated_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            kb_uuid=self._kb_uuid,
            app_version=self._app_version,
            question=question.strip(),
            items=tuple(truncated),
            citations=citations,
            excluded=tuple(excluded),
            warnings=warnings,
        )


def _exclusion_reason(
    item: ContextItem, include_archived: bool, include_superseded: bool
) -> str | None:
    if item.type is ContextItemType.KNOWLEDGE_OBJECT:
        if item.status == "archived" and not include_archived:
            return "已归档（默认排除）"
        if item.status == "superseded" and not include_superseded:
            return "已被替代（默认排除）"
        return None
    if item.type is ContextItemType.KNOWLEDGE_MEMORY:
        if item.status == "archived" and not include_archived:
            return "已归档（默认排除）"
    return None


def _truncate_item(item: ContextItem, max_chars: int) -> ContextItem:
    if len(item.content) <= max_chars:
        return item
    truncated = item.content[:max_chars].rstrip() + _CONTENT_TRUNCATED_MARK
    return replace(item, content=truncated)


def _item_warnings(item: ContextItem) -> tuple[str, ...]:
    warnings: list[str] = []
    if not item.source_anchors:
        warnings.append("无来源：该知识项没有可回源的来源。")
    for anchor in item.source_anchors:
        if anchor.fingerprint_state == "missing":
            warnings.append(f"来源缺失：{anchor.anchor_label}。")
        elif anchor.fingerprint_state == "changed":
            warnings.append(f"来源已变化：{anchor.anchor_label}。")
        elif anchor.fingerprint_state == "unknown":
            warnings.append(f"来源状态未知：{anchor.anchor_label}。")
    return tuple(warnings)


__all__ = [
    "DEFAULT_MAX_CHARS_PER_ITEM",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_TOTAL_CHARS",
    "ContextWarning",
    "ExcludedContextItem",
    "KnowledgeContextError",
    "KnowledgeContextPackage",
    "KnowledgeContextPackager",
]
