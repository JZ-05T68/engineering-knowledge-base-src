"""Pure Streamlit render helpers for the personal-knowledge search scope.

Phase 3D keeps these helpers deliberately independent from the page-scope
``SearchResult`` cards: knowledge results carry different provenance anchors
and lifecycle status badges, and Phase 5 citation will reuse the anchor labels
built here. No Prompt Builder and no AI path live in this module.
"""

from __future__ import annotations

import streamlit as st

from src.models import (
    KnowledgeObjectSourceType,
    KnowledgeSearchResult,
    KnowledgeSearchResultType,
)

_SOURCE_TYPE_LABELS = {
    source_type.value: source_type.label for source_type in KnowledgeObjectSourceType
}


def status_badge(result: KnowledgeSearchResult) -> str:
    """Return a compact status badge like ```ACTIVE` 现行``.

    The raw ``status`` value is the stable badge surface (ACTIVE / ARCHIVED /
    SUPERSEDED); the Chinese label is the current product-facing explanation.
    """

    raw = (result.status or "").upper() or "UNKNOWN"
    label = result.status_label or result.status or "状态未知"
    return f"`{raw}` {label}"


def provenance_labels(result: KnowledgeSearchResult) -> tuple[str, ...]:
    """Return human-readable source anchors for one knowledge result.

    Knowledge objects expose their ``source_anchors`` (document/page/note/
    evidence links); memory entries expose their knowledge-object / document /
    page links. These labels are the extension surface for Phase 5 citation
    without exposing raw database rows.
    """

    if result.result_type is KnowledgeSearchResultType.KNOWLEDGE_OBJECT:
        return tuple(
            f"{_SOURCE_TYPE_LABELS.get(source_type, source_type)} #{source_id}"
            for source_type, source_id in result.source_anchors
        )
    labels: list[str] = []
    if result.knowledge_object_id is not None:
        labels.append(f"知识对象 #{result.knowledge_object_id}")
    if result.document_id is not None:
        labels.append(f"文档 #{result.document_id}")
    if result.page_id is not None:
        labels.append(f"页面 #{result.page_id}")
    return tuple(labels)


def render_knowledge_result_card(result: KnowledgeSearchResult, index: int) -> None:
    """Render one personal-knowledge search result as a knowledge asset card."""

    type_label = result.result_type.label
    with st.container(border=True):
        st.markdown(f"### {index}. {type_label} · {result.title}")
        st.markdown(f"状态徽标：{status_badge(result)}")
        st.caption(f"稳定标识：{result.stable_id}")
        snippet = (
            (result.snippet or "").strip()
            or result.content[:220].strip()
            or "（该知识没有可显示的文本摘要）"
        )
        st.markdown(snippet)
        anchors = provenance_labels(result)
        if anchors:
            st.caption("来源锚点：" + "、".join(anchors))
        else:
            st.caption("来源锚点：暂无")


__all__ = [
    "provenance_labels",
    "render_knowledge_result_card",
    "status_badge",
]
