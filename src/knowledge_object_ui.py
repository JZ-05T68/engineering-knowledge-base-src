"""Streamlit UI helpers for the v0.5.2 knowledge-object page (Phase 2B).

The page turns the knowledge-object service into a local, offline working
surface: create, filter, confirm, archive, link sources, build typed
relations, and generate a citation-grounded prompt package for external AI
tools. Every write goes through :class:`KnowledgeObjectService`; the page never
calls any AI provider and never touches original PDFs, page images, notes or
evidence rows.

This is a mechanical compatibility pass for schema v10. Final UI design
(source picker, supersession guidance, revision history) lands in Phase 5.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.database import Database
from src.knowledge_object_service import (
    KnowledgeObjectNotFoundError,
    KnowledgeObjectService,
    KnowledgeObjectValidationError,
    KnowledgeSourceLinkError,
)
from src.knowledge_prompt_builder import KnowledgePromptBuilder
from src.models import (
    KnowledgeEpistemicBasis,
    KnowledgeLifecycle,
    KnowledgeObjectKind,
    KnowledgeObjectSourceType,
    KnowledgeRelationType,
    NoteImportance,
)

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 20

_KIND_OPTIONS = [None, *KnowledgeObjectKind]
_IMPORTANCE_OPTIONS = [None, *NoteImportance]
_LIFECYCLE_OPTIONS = [None, *KnowledgeLifecycle]
_BASIS_OPTIONS = [
    basis
    for basis in KnowledgeEpistemicBasis
    if basis is not KnowledgeEpistemicBasis.UNKNOWN_LEGACY
]
_RELATION_OPTIONS = list(KnowledgeRelationType)
_SOURCE_TYPE_OPTIONS = list(KnowledgeObjectSourceType)


def render_knowledge_object_page(
    service: KnowledgeObjectService, database: Database
) -> None:
    """Render the knowledge-object browsing and editing surface."""

    _render_create_form(service)
    filters = _render_filters(service)
    objects = service.list(
        kind=filters["kind"],
        importance=filters["importance"],
        lifecycle=filters["lifecycle"],
        query=filters["query"],
        sort_by=filters["sort_by"],
        limit=PAGE_SIZE,
        offset=(int(st.session_state.get("ko_page", 1)) - 1) * PAGE_SIZE,
    )
    total = service.count(
        kind=filters["kind"],
        importance=filters["importance"],
        lifecycle=filters["lifecycle"],
        query=filters["query"],
    )
    if total == 0:
        st.info(
            "还没有知识对象。点击上方“新建知识对象”，把散落在页面、笔记和证据中的"
            "知识提炼为可复用、可关联的知识资产。"
        )
        return
    _render_pagination(total)
    for knowledge_object in objects:
        _render_object_card(service, database, knowledge_object)


def _render_create_form(service: KnowledgeObjectService) -> None:
    with st.expander("新建知识对象", expanded=False):
        kind = st.selectbox(
            "类型",
            options=list(KnowledgeObjectKind),
            format_func=lambda value: value.label,
            key="ko_new_kind",
        )
        title = st.text_input("标题", key="ko_new_title", max_chars=200)
        content = st.text_area("内容", key="ko_new_content", height=160)
        importance = st.selectbox(
            "重要程度",
            options=list(NoteImportance),
            format_func=lambda value: value.label,
            key="ko_new_importance",
        )
        basis = st.selectbox(
            "形成依据",
            options=_BASIS_OPTIONS,
            format_func=lambda value: value.label,
            key="ko_new_basis",
        )
        st.caption("新对象以“现行 / 未确认”状态创建，作者固定为“用户”。")
        if st.button("创建知识对象", key="ko_create", type="primary"):
            try:
                created = service.create(
                    kind=kind,
                    title=title,
                    content=content,
                    importance=importance,
                    epistemic_basis=basis,
                )
            except (KnowledgeObjectValidationError, ValueError) as exc:
                st.error(f"创建失败：{exc}")
            else:
                st.session_state["ko_flash"] = (
                    f"已创建知识对象「{created.knowledge_object.title}」。"
                )
                st.session_state["ko_page"] = 1
                st.rerun()


def _render_filters(service: KnowledgeObjectService) -> dict:
    flash = st.session_state.pop("ko_flash", "")
    if flash:
        st.success(flash)
    columns = st.columns([2, 2, 2, 3, 2])
    kind = columns[0].selectbox(
        "类型",
        options=_KIND_OPTIONS,
        format_func=lambda value: "全部" if value is None else value.label,
        key="ko_filter_kind",
    )
    importance = columns[1].selectbox(
        "重要程度",
        options=_IMPORTANCE_OPTIONS,
        format_func=lambda value: "全部" if value is None else value.label,
        key="ko_filter_importance",
    )
    lifecycle = columns[2].selectbox(
        "生命周期",
        options=_LIFECYCLE_OPTIONS,
        format_func=lambda value: "全部" if value is None else value.label,
        key="ko_filter_lifecycle",
    )
    query = columns[3].text_input(
        "关键词过滤", key="ko_filter_query", placeholder="匹配标题或内容"
    )
    sort_by = columns[4].selectbox(
        "排序",
        options=["updated_desc", "created_desc", "title_asc"],
        format_func={
            "updated_desc": "最近更新",
            "created_desc": "最近创建",
            "title_asc": "标题",
        }.get,
        key="ko_filter_sort",
    )
    return {
        "kind": kind,
        "importance": importance,
        "lifecycle": lifecycle,
        "query": query,
        "sort_by": sort_by,
    }


def _render_pagination(total: int) -> None:
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = int(st.session_state.get("ko_page", 1))
    if page > page_count:
        page = page_count
        st.session_state["ko_page"] = page
        st.rerun()
    columns = st.columns([1, 2, 1])
    if columns[0].button("上一页", key="ko_prev", disabled=page <= 1):
        st.session_state["ko_page"] = page - 1
        st.rerun()
    columns[1].caption(f"第 {page} / {page_count} 页，共 {total} 个知识对象")
    if columns[2].button("下一页", key="ko_next", disabled=page >= page_count):
        st.session_state["ko_page"] = page + 1
        st.rerun()


def _confirmation_badge(knowledge_object) -> str:
    if knowledge_object.confirmation_is_current:
        return f"已确认（第 {knowledge_object.confirmed_revision} 版）"
    if knowledge_object.confirmation_is_stale:
        return (
            f"确认基于旧版（第 {knowledge_object.confirmed_revision} 版，"
            f"当前第 {knowledge_object.current_revision} 版）"
        )
    return "未确认"


def _render_object_card(
    service: KnowledgeObjectService,
    database: Database,
    knowledge_object,
) -> None:
    with st.container(border=True):
        st.markdown(
            f"**{knowledge_object.title}**　`{knowledge_object.kind.label}`　"
            f"`{knowledge_object.importance.label}`　"
            f"`{knowledge_object.lifecycle.label}`"
        )
        st.caption(
            f"ID {knowledge_object.id} · {knowledge_object.epistemic_basis.label} · "
            f"{_confirmation_badge(knowledge_object)} · "
            f"更新于 {knowledge_object.updated_at:%Y-%m-%d %H:%M}"
        )
        if knowledge_object.superseded_by_ko_id is not None:
            st.caption(f"已替代 → 知识对象 {knowledge_object.superseded_by_ko_id}")
        with st.expander("详情 / 编辑", expanded=False):
            try:
                view = service.get_view(knowledge_object.id)
            except KnowledgeObjectNotFoundError:
                st.info("该知识对象已被删除。")
                return
            st.markdown(view.knowledge_object.content)
            _render_sources(service, view)
            _render_relations(service, view)
            _render_state_actions(service, view)
            _render_delete_action(service, view)
            _render_prompt_builder(service, view)


def _render_sources(service: KnowledgeObjectService, view) -> None:
    st.markdown("#### 来源")
    if view.sources:
        for source_view in view.sources:
            source = source_view.source
            status_label = source_view.status.label
            columns = st.columns([5, 1])
            columns[0].caption(
                f"{source.source_type.label} {source.source_id}"
                + (f"（{source.source_note}）" if source.source_note.strip() else "")
                + f" · {status_label}"
            )
            if columns[1].button(
                "解除", key=f"ko_unlink_{source.id}", help="只解除关联，不删除来源材料"
            ):
                try:
                    service.unlink_source(source.id)
                except KnowledgeSourceLinkError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
    else:
        st.caption("尚无来源。")
    with st.expander("添加来源", expanded=False):
        source_type = st.selectbox(
            "来源类型",
            options=_SOURCE_TYPE_OPTIONS,
            format_func=lambda value: value.label,
            key=f"ko_source_type_{view.knowledge_object.id}",
        )
        source_id = st.number_input(
            "来源 ID",
            min_value=1,
            step=1,
            key=f"ko_source_id_{view.knowledge_object.id}",
        )
        source_note = st.text_input(
            "来源说明（可选）", key=f"ko_source_note_{view.knowledge_object.id}"
        )
        if st.button("关联来源", key=f"ko_link_{view.knowledge_object.id}"):
            try:
                service.link_source(
                    view.knowledge_object.id,
                    source_type=source_type,
                    source_id=int(source_id),
                    source_note=source_note,
                )
            except KnowledgeSourceLinkError as exc:
                st.error(str(exc))
            else:
                st.rerun()


def _render_relations(service: KnowledgeObjectService, view) -> None:
    st.markdown("#### 关系")
    relations = service.relations(view.knowledge_object.id)
    if relations:
        for relation in relations:
            other_id = (
                relation.target_ko_id
                if relation.source_ko_id == view.knowledge_object.id
                else relation.source_ko_id
            )
            direction = "指向" if relation.source_ko_id == view.knowledge_object.id else "来自"
            columns = st.columns([5, 1])
            columns[0].caption(
                f"{relation.relation_type.label} · {direction} 知识对象 {other_id}"
                + (f"（{relation.description}）" if relation.description.strip() else "")
            )
            if columns[1].button("移除", key=f"ko_unrelate_{relation.id}"):
                service.remove_relation(relation.id)
                st.rerun()
    else:
        st.caption("尚无关系。")
    with st.expander("添加关系", expanded=False):
        target_id = st.number_input(
            "目标知识对象 ID",
            min_value=1,
            step=1,
            key=f"ko_relation_target_{view.knowledge_object.id}",
        )
        relation_type = st.selectbox(
            "关系类型",
            options=_RELATION_OPTIONS,
            format_func=lambda value: value.label,
            key=f"ko_relation_type_{view.knowledge_object.id}",
        )
        if st.button("建立关系", key=f"ko_relate_{view.knowledge_object.id}"):
            try:
                service.add_relation(
                    view.knowledge_object.id,
                    int(target_id),
                    relation_type=relation_type,
                )
            except (KnowledgeObjectValidationError, KnowledgeObjectNotFoundError) as exc:
                st.error(str(exc))
            else:
                st.rerun()


def _render_state_actions(service: KnowledgeObjectService, view) -> None:
    knowledge_object = view.knowledge_object
    st.markdown("#### 状态操作")
    if knowledge_object.confirmation_is_current:
        if st.button("取消确认", key=f"ko_unconfirm_{knowledge_object.id}"):
            service.unconfirm(knowledge_object.id)
            st.rerun()
    else:
        if st.button("确认当前内容", key=f"ko_confirm_{knowledge_object.id}"):
            service.confirm(knowledge_object.id)
            st.rerun()
    if knowledge_object.lifecycle is KnowledgeLifecycle.ACTIVE:
        if st.button("归档", key=f"ko_archive_{knowledge_object.id}"):
            service.archive(knowledge_object.id)
            st.rerun()
    elif knowledge_object.lifecycle is KnowledgeLifecycle.ARCHIVED:
        if st.button("重新启用", key=f"ko_unarchive_{knowledge_object.id}"):
            service.unarchive(knowledge_object.id)
            st.rerun()
    elif knowledge_object.lifecycle is KnowledgeLifecycle.SUPERSEDED:
        if st.button("重新启用（解除替代）", key=f"ko_reactivate_{knowledge_object.id}"):
            service.reactivate(knowledge_object.id)
            st.rerun()


def _render_delete_action(service: KnowledgeObjectService, view) -> None:
    if st.button(
        "删除知识对象",
        key=f"ko_delete_{view.knowledge_object.id}",
        help="只删除知识对象及其来源和关系，不删除任何原始材料",
    ):
        try:
            service.delete(view.knowledge_object.id)
        except KnowledgeObjectValidationError as exc:
            st.error(str(exc))
        else:
            st.session_state["ko_flash"] = "知识对象已删除。"
            st.rerun()


def _render_prompt_builder(service: KnowledgeObjectService, view) -> None:
    st.markdown("#### 生成外部 AI 提示词")
    st.caption("本功能只生成本地文本，不连接任何 AI 服务，也不读取 API Key。")
    question = st.text_area(
        "要交给外部 AI 回答的问题",
        key=f"ko_prompt_question_{view.knowledge_object.id}",
        height=90,
    )
    if st.button("生成引用提示词", key=f"ko_prompt_{view.knowledge_object.id}"):
        source_texts = _resolve_source_texts(service, view)
        try:
            prompt = KnowledgePromptBuilder().build(
                question, [view], source_texts=source_texts
            )
        except ValueError as exc:
            st.error(f"生成提示词失败：{exc}")
        else:
            st.session_state[f"ko_prompt_result_{view.knowledge_object.id}"] = prompt
    prompt = st.session_state.get(f"ko_prompt_result_{view.knowledge_object.id}", "")
    if prompt:
        st.text_area("提示词", value=prompt, height=360)


def _resolve_source_texts(service: KnowledgeObjectService, view) -> dict[tuple[str, int], str]:
    """Resolve display text for each source without touching original files."""

    database = service._database  # noqa: SLF001 - UI resolver needs source facts
    texts: dict[tuple[str, int], str] = {}
    for source_view in view.sources:
        source = source_view.source
        key = (source.source_type.value, source.source_id)
        if source.source_type is KnowledgeObjectSourceType.DOCUMENT:
            document = database.get_document(source.source_id)
            texts[key] = (
                f"{document.title}（{document.filename}）" if document else ""
            )
        elif source.source_type is KnowledgeObjectSourceType.PAGE:
            page = database.get_page(source.source_id)
            if page is not None:
                texts[key] = page.searchable_content
        elif source.source_type is KnowledgeObjectSourceType.NOTE:
            texts[key] = _note_text(database, source.source_id)
        elif source.source_type is KnowledgeObjectSourceType.EVIDENCE:
            texts[key] = _evidence_text(database, source.source_id)
    return texts


def _note_text(database: Database, note_id: int) -> str:
    with database._connection() as connection:  # noqa: SLF001 - read-only resolve
        row = connection.execute(
            "SELECT personal_note, user_excerpt FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
    if row is None:
        return ""
    personal_note = str(row["personal_note"])
    user_excerpt = row["user_excerpt"]
    if user_excerpt:
        return f"{personal_note}\n原文摘录：{user_excerpt}"
    return personal_note


def _evidence_text(database: Database, evidence_id: int) -> str:
    with database._connection() as connection:  # noqa: SLF001 - read-only resolve
        row = connection.execute(
            "SELECT evidence_text, user_note FROM evidence_items WHERE id = ?",
            (evidence_id,),
        ).fetchone()
    if row is None:
        return ""
    evidence_text = str(row["evidence_text"])
    user_note = str(row["user_note"])
    return evidence_text + (f"\n用户备注：{user_note}" if user_note.strip() else "")
