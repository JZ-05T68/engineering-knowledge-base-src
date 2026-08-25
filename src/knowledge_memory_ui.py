"""Streamlit UI helpers for the v0.5.2 knowledge-memory page (Phase 2B).

The page is a personal-memory surface: user-authored problem-solving /
experience / decision entries can be created, edited, archived and deleted.
System change records live in ``knowledge_object_revisions`` and are never
shown here (Phase 5 will add a dedicated revision history view).
"""

from __future__ import annotations

import logging

import streamlit as st

from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
    KnowledgeMemoryValidationError,
)
from src.models import KnowledgeMemoryEntryKind, KnowledgeMemoryStatus

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 20

_USER_KINDS = (
    KnowledgeMemoryEntryKind.PROBLEM_SOLVING,
    KnowledgeMemoryEntryKind.EXPERIENCE,
    KnowledgeMemoryEntryKind.DECISION,
)

_STATUS_OPTIONS = [None, *KnowledgeMemoryStatus]


def render_knowledge_memory_page(service: KnowledgeMemoryService) -> None:
    """Render the knowledge-memory browsing and entry-creation surface."""

    _render_create_form(service)
    filter_columns = st.columns([2, 2])
    kind = filter_columns[0].selectbox(
        "类型",
        options=[None, *_USER_KINDS],
        format_func=lambda value: "全部" if value is None else value.label,
        key="km_filter_kind",
    )
    status = filter_columns[1].selectbox(
        "状态",
        options=_STATUS_OPTIONS,
        format_func=lambda value: "全部" if value is None else value.label,
        key="km_filter_status",
    )
    entries = service.list(kind=kind, status=status, limit=PAGE_SIZE)
    total = service.count(kind=kind, status=status)
    if total == 0:
        st.info(
            "还没有记忆条目。记录问题解决过程、经验和决策，让知识真正留下来。"
        )
        return
    st.caption(f"共 {total} 条记忆（本页显示前 {min(total, PAGE_SIZE)} 条）")
    for entry in entries:
        _render_entry(service, entry)


def _render_create_form(service: KnowledgeMemoryService) -> None:
    flash = st.session_state.pop("km_flash", "")
    if flash:
        st.success(flash)
    with st.expander("新建记忆条目", expanded=False):
        kind = st.selectbox(
            "类型",
            options=list(_USER_KINDS),
            format_func=lambda value: value.label,
            key="km_new_kind",
        )
        title = st.text_input("标题", key="km_new_title", max_chars=200)
        content = st.text_area("内容", key="km_new_content", height=140)
        root_cause = st.text_area(
            "最终原因（可选，问题解决类建议填写）",
            key="km_new_root_cause",
            height=80,
        )
        lesson = st.text_area(
            "经验教训（可选）", key="km_new_lesson", height=80
        )
        link_columns = st.columns(3)
        ko_id = link_columns[0].number_input(
            "关联知识对象 ID（可选）",
            min_value=0,
            step=1,
            key="km_new_ko_id",
        )
        document_id = link_columns[1].number_input(
            "关联文档 ID（可选）",
            min_value=0,
            step=1,
            key="km_new_document_id",
        )
        page_id = link_columns[2].number_input(
            "关联页面 ID（可选）",
            min_value=0,
            step=1,
            key="km_new_page_id",
        )
        if st.button("创建记忆条目", key="km_create", type="primary"):
            try:
                entry = service.create_entry(
                    kind=kind,
                    title=title,
                    content=content,
                    root_cause=root_cause,
                    lesson=lesson,
                    knowledge_object_id=int(ko_id) if int(ko_id) > 0 else None,
                    document_id=int(document_id) if int(document_id) > 0 else None,
                    page_id=int(page_id) if int(page_id) > 0 else None,
                )
            except (KnowledgeMemoryValidationError, ValueError) as exc:
                st.error(f"创建失败：{exc}")
            else:
                st.session_state["km_flash"] = f"已创建记忆条目「{entry.title}」。"
                st.rerun()


def _render_entry(service: KnowledgeMemoryService, entry) -> None:
    with st.container(border=True):
        st.markdown(
            f"**{entry.title}**　`{entry.kind.label}`　`{entry.status.label}`"
        )
        st.caption(f"ID {entry.id} · 更新于 {entry.updated_at:%Y-%m-%d %H:%M}")
        if entry.content.strip():
            st.write(entry.content)
        if entry.root_cause.strip():
            st.markdown(f"**最终原因**：{entry.root_cause}")
        if entry.lesson.strip():
            st.markdown(f"**经验教训**：{entry.lesson}")
        link_parts = []
        if entry.knowledge_object_id is not None:
            link_parts.append(f"知识对象 {entry.knowledge_object_id}")
        if entry.document_id is not None:
            link_parts.append(f"文档 {entry.document_id}")
        if entry.page_id is not None:
            link_parts.append(f"页面 {entry.page_id}")
        if link_parts:
            st.caption("关联：" + "、".join(link_parts))
        action_columns = st.columns([1, 1])
        if entry.status is KnowledgeMemoryStatus.ACTIVE:
            if action_columns[0].button("归档", key=f"km_archive_{entry.id}"):
                service.set_status(entry.id, status="archived")
                st.rerun()
        else:
            if action_columns[0].button("重新启用", key=f"km_unarchive_{entry.id}"):
                service.set_status(entry.id, status="active")
                st.rerun()
        if action_columns[1].button("删除记忆条目", key=f"km_delete_{entry.id}"):
            try:
                service.delete_entry(entry.id)
            except KnowledgeMemoryEntryNotFoundError as exc:
                st.error(str(exc))
            else:
                st.session_state["km_flash"] = "记忆条目已删除。"
                st.rerun()
