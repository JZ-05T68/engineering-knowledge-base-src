"""Streamlit UI helpers for the cross-document knowledge aggregation page.

The aggregation page is a read-only knowledge browsing layer: pick an
organization axis (project or tag), optionally filter by importance or
note type, browse the unified entries, and navigate back to the source.
It offers no editing, no deletion, no basket operations — those stay on
their own pages. All data comes from :class:`AggregationService` queries;
the page never caches results across runs and never writes to the
database.
"""

from __future__ import annotations

import logging

import streamlit as st

from src.aggregation_service import AggregationService
from src.database import Database
from src.models import (
    AggregationItem,
    AggregationResult,
    AggregationSourceKind,
    NoteImportance,
    NoteType,
)

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE_OPTIONS = (20, 50, 100)
_PAGE_SIZE_DEFAULT = 20


def build_source_params(item: AggregationItem) -> dict[str, str]:
    """Reader-page query params for one entry (document-level notes land on page 1)."""

    page_number = item.page_number if item.page_number is not None else 1
    return {
        "document": str(item.document_id),
        "page": str(page_number),
        "from_search": "0",
    }


def render_aggregation_page(
    aggregation_service: AggregationService,
    database: Database,
) -> None:
    """Render the full aggregation view: axis, filters, summary, entries."""

    projects = database.list_projects()
    tags = database.list_tags()
    axis_label = st.radio(
        "知识轴",
        ["项目", "标签"],
        horizontal=True,
        key="agg_axis",
        help="按项目或标签汇总分散在多份资料中的笔记与证据。",
    )
    if axis_label == "项目":
        _render_axis(
            aggregation_service,
            options=projects,
            empty_hint="还没有项目。可以先在“项目管理”页创建项目，并把文档或页面关联进去。",
            axis="project",
            axis_name="项目",
        )
    else:
        _render_axis(
            aggregation_service,
            options=tags,
            empty_hint="还没有标签。可以先在“标签管理”页创建标签，并标到文档或页面上。",
            axis="tag",
            axis_name="标签",
        )


def _render_axis(
    aggregation_service: AggregationService,
    *,
    options: list,
    empty_hint: str,
    axis: str,
    axis_name: str,
) -> None:
    if not options:
        st.info(empty_hint)
        return
    option_ids = [entity.id for entity in options]
    name_by_id = {entity.id: entity.name for entity in options}

    requested_id = _safe_int(st.query_params.get("agg_id"))
    stale_requested = requested_id is not None and requested_id not in option_ids
    default_index = (
        option_ids.index(requested_id) if requested_id in option_ids else 0
    )
    selected_id = st.selectbox(
        f"选择{axis_name}",
        options=option_ids,
        index=default_index,
        # The widget may briefly hold a value that is no longer an option
        # (stale session state); fall back to the raw id instead of raising.
        format_func=lambda value: name_by_id.get(value, str(value)),
        key=f"agg_{axis}_id",
    )
    if stale_requested:
        st.info(f"此前选择的{axis_name}已不存在，已回到默认选择。")
    st.query_params["agg_axis"] = axis
    st.query_params["agg_id"] = str(selected_id)

    filters = _render_filters()
    result = _load_result(
        aggregation_service,
        axis=axis,
        axis_id=int(selected_id),
        importance=filters["importance"],
        note_type=filters["note_type"],
    )
    if result is None:
        return
    _render_summary(result)
    if result.total_count == 0:
        if filters["importance"] is not None or filters["note_type"] is not None:
            st.info("当前筛选条件下没有条目，试试放宽筛选条件。")
        else:
            st.info(
                f"这个{axis_name}目前还没有可聚合的笔记或证据。"
                "可以先在资料页添加笔记、证据，或把文档、页面关联进来。"
            )
        return
    page_items = _render_pagination(result)
    for item in page_items:
        _render_item(item)


def _render_filters() -> dict:
    """Importance and note-type filters; filter changes reset to page one."""

    columns = st.columns(3)
    importance = columns[0].selectbox(
        "重要性",
        options=[None, *NoteImportance],
        format_func=lambda value: "全部" if value is None else value.label,
        key="agg_importance",
    )
    note_type = columns[1].selectbox(
        "笔记类型",
        options=[None, *NoteType],
        format_func=lambda value: "全部" if value is None else value.label,
        key="agg_note_type",
    )
    page_size = columns[2].selectbox(
        "每页条数",
        options=list(_PAGE_SIZE_OPTIONS),
        index=_PAGE_SIZE_OPTIONS.index(_PAGE_SIZE_DEFAULT),
        key="agg_page_size",
    )
    if importance is not None or note_type is not None:
        st.caption("证据条目没有重要性等级和笔记类型，筛选时仅显示结构化笔记。")
    signature = (
        st.session_state.get("agg_axis"),
        st.session_state.get("agg_project_id"),
        st.session_state.get("agg_tag_id"),
        importance,
        note_type,
        page_size,
    )
    if st.session_state.get("agg_filter_signature") != signature:
        st.session_state["agg_filter_signature"] = signature
        st.session_state["agg_page"] = 1
    return {"importance": importance, "note_type": note_type, "page_size": page_size}


def _load_result(
    aggregation_service: AggregationService,
    *,
    axis: str,
    axis_id: int,
    importance: NoteImportance | None,
    note_type: NoteType | None,
) -> AggregationResult | None:
    """Query the current page of results; stale selections fail soft."""

    page = int(st.session_state.get("agg_page", 1))
    page_size = int(st.session_state.get("agg_page_size", _PAGE_SIZE_DEFAULT))
    try:
        if axis == "project":
            return aggregation_service.aggregate_by_project(
                axis_id,
                importance=importance,
                note_type=note_type,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
        return aggregation_service.aggregate_by_tag(
            axis_id,
            importance=importance,
            note_type=note_type,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    except Exception as exc:
        LOGGER.exception("聚合查询失败：axis=%s id=%s", axis, axis_id)
        st.error(f"聚合查询失败：{exc}")
        return None


def _render_summary(result: AggregationResult) -> None:
    metrics = st.columns(4)
    metrics[0].metric("涉及文档", result.document_count)
    metrics[1].metric("笔记", result.note_count)
    metrics[2].metric("证据", result.evidence_count)
    metrics[3].metric("总条目", result.total_count)
    st.caption(
        f"笔记等级分布：{NoteImportance.PRIMARY.label} {result.primary_count} 条 · "
        f"{NoteImportance.SECONDARY.label} {result.secondary_count} 条 · "
        f"{NoteImportance.NORMAL.label} {result.normal_count} 条"
    )


def _render_pagination(result: AggregationResult) -> tuple[AggregationItem, ...]:
    """Prev/next pagination over the service-level limit/offset contract."""

    page_size = result.limit
    page_count = max(1, (result.total_count + page_size - 1) // page_size)
    page = int(st.session_state.get("agg_page", 1))
    if page > page_count:
        page = page_count
        st.session_state["agg_page"] = page
        st.rerun()
    columns = st.columns([1, 2, 1])
    if columns[0].button("上一页", key="agg_prev", disabled=page <= 1):
        st.session_state["agg_page"] = page - 1
        st.rerun()
    columns[1].markdown(
        f"<p style='text-align:center'>第 {page} / {page_count} 页，"
        f"共 {result.total_count} 条</p>",
        unsafe_allow_html=True,
    )
    if columns[2].button("下一页", key="agg_next", disabled=page >= page_count):
        st.session_state["agg_page"] = page + 1
        st.rerun()
    return result.items


def _render_item(item: AggregationItem) -> None:
    """One entry: content first, then source identity, then navigation."""

    with st.container(border=True):
        if item.source_kind is AggregationSourceKind.NOTE:
            badge = f"结构化笔记 · {item.note_type.label} · {item.importance.label}"
        else:
            badge = "证据（来自证据篮）"
        location = (
            f"第 {item.page_number} 页" if item.page_number is not None else "文档级"
        )
        st.markdown(f"**{item.document_title}** · {location}　`{badge}`")
        st.write(item.content)
        if item.user_note:
            st.caption(f"批注：{item.user_note}")
        context_parts = []
        if item.tags:
            context_parts.append("标签：" + "、".join(item.tags))
        if item.projects:
            context_parts.append("项目：" + "、".join(item.projects))
        if context_parts:
            st.caption("　".join(context_parts))
        if st.button(
            "查看原文",
            key=f"agg_open_{item.source_kind.value}_{item.source_id}",
        ):
            source_params = build_source_params(item)
            # 与笔记列表、证据篮一致的一次性移交；阅读页会重新校验参数。
            st.session_state["pending_reader_query_params"] = source_params
            st.query_params.clear()
            st.query_params.update(source_params)
            st.switch_page("pages/2_浏览资料.py")
        if (
            item.source_kind is AggregationSourceKind.EVIDENCE
            and st.button("查看证据篮", key=f"agg_basket_{item.source_id}")
        ):
            st.switch_page("pages/9_证据篮.py")


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
