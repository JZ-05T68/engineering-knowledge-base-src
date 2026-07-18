"""Search local engineering knowledge with traceable, page-level results."""

from __future__ import annotations

import logging

import streamlit as st

from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
)
from src.evidence_service import EvidencePackageBuilder
from src.models import PageStatus, SearchField, SearchFilters, SearchSort
from src.prompt_builder import PromptBuilder
from src.runtime import application_database, application_evidence_basket_service
from src.search_navigation import (
    SearchNavigationError,
    reader_query_params,
    validate_search_target,
)
from src.search_service import SearchService

LOGGER = logging.getLogger(__name__)
RESULTS_PER_PAGE = 10

st.set_page_config(page_title="检索资料｜工程知识库 v0.0.5", page_icon="🔎", layout="wide")
st.title("检索资料")
st.caption("本地检索目标页面，解释命中原因，并生成可追溯原始资料的引用证据包。")
st.markdown(
    """
    <style>
    mark { background: #fde68a; color: inherit; padding: 0.05rem 0.12rem; border-radius: 0.2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _clear_filters() -> None:
    """Reset only filters and ordering; keep the user's current query."""

    st.session_state["search_document_ids"] = []
    st.session_state["search_project_ids"] = []
    st.session_state["search_tag_ids"] = []
    st.session_state["search_status_values"] = []
    st.session_state["search_field_values"] = []
    st.session_state["search_sort_value"] = SearchSort.RELEVANCE.value
    st.session_state["search_result_page"] = 1


try:
    database = application_database()
    basket_service = application_evidence_basket_service()
    search_service = SearchService(database)
    evidence_builder = EvidencePackageBuilder()
    all_documents = database.list_documents(sort_by="name_asc")
    all_projects = database.list_projects()
    all_tags = database.list_tags()
except Exception as exc:
    LOGGER.exception("初始化检索服务失败")
    st.error(f"初始化检索服务失败：{exc}")
    st.stop()

defaults: dict[str, object] = {
    "search_query_input": st.session_state.get("knowledge_query", ""),
    "search_document_ids": [],
    "search_project_ids": [],
    "search_tag_ids": [],
    "search_status_values": [],
    "search_field_values": [],
    "search_sort_value": SearchSort.RELEVANCE.value,
    "search_limit": 50,
    "search_result_page": 1,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value.copy() if isinstance(value, list) else value

document_names = {
    document.id: document.title.strip() or document.filename.strip() or f"文档 {document.id}"
    for document in all_documents
}
project_names = {project.id: project.name for project in all_projects}
tag_names = {tag.id: tag.name for tag in all_tags}

active_filter_state = SearchFilters(
    document_ids=tuple(st.session_state["search_document_ids"]),
    project_ids=tuple(st.session_state["search_project_ids"]),
    tag_ids=tuple(st.session_state["search_tag_ids"]),
    statuses=tuple(
        PageStatus(value) for value in st.session_state["search_status_values"]
    ),
    match_fields=tuple(
        SearchField(value) for value in st.session_state["search_field_values"]
    ),
)
try:
    facet_counts = search_service.facet_counts(
        str(st.session_state["search_query_input"]), filters=active_filter_state
    )
    basket_items = basket_service.list_items()
except Exception as exc:
    LOGGER.exception("读取筛选计数或证据篮失败")
    st.error(f"读取筛选计数或证据篮失败：{exc}")
    st.stop()

page_evidence_counts: dict[int, int] = {}
for basket_item in basket_items:
    page_evidence_counts[basket_item.page_id] = (
        page_evidence_counts.get(basket_item.page_id, 0) + 1
    )

entry_columns = st.columns([5, 1])
entry_columns[0].caption(
    f"当前组合条件匹配 {facet_counts.total} 页；计数包含各筛选维度自身的已选条件。"
)
if entry_columns[1].button(
    f"查看证据篮（{len(basket_items)}）",
    use_container_width=True,
):
    st.switch_page("pages/9_证据篮.py")

basket_flash = st.session_state.pop("basket_flash", "")
if basket_flash:
    st.success(basket_flash)

with st.form("search_form"):
    query = st.text_input(
        "搜索内容",
        key="search_query_input",
        placeholder="例如：液压泵 异常噪声",
    )
    with st.expander("筛选与排序", expanded=True):
        first_row = st.columns(3)
        first_row[0].multiselect(
            "文档",
            options=list(document_names),
            format_func=lambda value: document_names[value],
            key="search_document_ids",
        )
        first_row[1].multiselect(
            "项目（同时满足）",
            options=list(project_names),
            format_func=lambda value: (
                f"{project_names[value]}（{facet_counts.projects.get(value, 0)}）"
            ),
            key="search_project_ids",
        )
        first_row[2].multiselect(
            "标签（同时满足，AND）",
            options=list(tag_names),
            format_func=lambda value: (
                f"{tag_names[value]}（{facet_counts.tags.get(value, 0)}）"
            ),
            key="search_tag_ids",
            help="选择多个标签时，只返回同时具备全部所选标签的页面。",
        )
        second_row = st.columns(4)
        second_row[0].multiselect(
            "页面复核状态",
            options=[status.value for status in PageStatus],
            format_func=lambda value: (
                f"{PageStatus(value).label}"
                f"（{facet_counts.statuses.get(PageStatus(value), 0)}）"
            ),
            key="search_status_values",
        )
        second_row[1].multiselect(
            "命中字段 / 内容来源",
            options=[field.value for field in SearchField],
            format_func=lambda value: SearchField(value).label,
            key="search_field_values",
        )
        second_row[2].selectbox(
            "排序",
            options=[sort.value for sort in SearchSort],
            format_func=lambda value: SearchSort(value).label,
            key="search_sort_value",
        )
        second_row[3].slider(
            "最多返回结果",
            min_value=10,
            max_value=100,
            step=10,
            key="search_limit",
        )
    submit_column, clear_column = st.columns([3, 1])
    submitted = submit_column.form_submit_button(
        "检索", type="primary", use_container_width=True
    )
    cleared = clear_column.form_submit_button(
        "清空筛选",
        on_click=_clear_filters,
        use_container_width=True,
    )

if submitted or cleared:
    st.session_state["knowledge_query"] = query
    st.session_state["prompt_question"] = query
    st.session_state["search_result_page"] = 1
    filters = SearchFilters(
        document_ids=tuple(st.session_state["search_document_ids"]),
        project_ids=tuple(st.session_state["search_project_ids"]),
        tag_ids=tuple(st.session_state["search_tag_ids"]),
        statuses=tuple(
            PageStatus(value) for value in st.session_state["search_status_values"]
        ),
        match_fields=tuple(
            SearchField(value) for value in st.session_state["search_field_values"]
        ),
    )
    st.session_state["knowledge_filters"] = filters
    st.session_state["knowledge_sort"] = st.session_state["search_sort_value"]
    try:
        st.session_state["knowledge_results"] = search_service.search(
            query,
            limit=int(st.session_state["search_limit"]),
            filters=filters,
            sort_by=st.session_state["search_sort_value"],
        )
    except Exception as exc:
        LOGGER.exception("全文检索失败：query=%r", query)
        st.error(f"检索失败：{exc}")
        st.session_state["knowledge_results"] = []
    st.session_state.pop("knowledge_prompt", None)

results = st.session_state.get("knowledge_results", [])
active_query = st.session_state.get("knowledge_query", query)
has_searched = "knowledge_results" in st.session_state
if has_searched and not active_query.strip():
    st.warning("请输入要检索的内容。")
elif has_searched and not results:
    st.info("没有符合关键词与筛选条件的页面。可清空筛选、缩短关键词，或补充页面笔记。")

if results:
    result_count = len(results)
    page_count = (result_count + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    current_page = min(
        max(int(st.session_state.get("search_result_page", 1)), 1), page_count
    )
    st.session_state["search_result_page"] = current_page
    heading_columns = st.columns([4, 1, 1])
    heading_columns[0].subheader(f"检索结果（{result_count} 条）")
    if heading_columns[1].button(
        "← 上一组",
        disabled=current_page <= 1,
        use_container_width=True,
    ):
        st.session_state["search_result_page"] = current_page - 1
        st.rerun()
    if heading_columns[2].button(
        "下一组 →",
        disabled=current_page >= page_count,
        use_container_width=True,
    ):
        st.session_state["search_result_page"] = current_page + 1
        st.rerun()
    st.caption(f"第 {current_page} / {page_count} 组；每组最多 {RESULTS_PER_PAGE} 条。")

    start = (current_page - 1) * RESULTS_PER_PAGE
    visible_results = results[start : start + RESULTS_PER_PAGE]
    evidence_packages = dict(st.session_state.get("evidence_packages", {}))
    for index, result in enumerate(visible_results, start=start + 1):
        snippet = result.snippet or result.content[:220] or "（该页没有可显示的文本摘要）"
        default_selection = (result.snippet or result.content).strip()
        default_selection = default_selection.removeprefix("…").removesuffix("…").strip()
        selection_key = f"basket_selection_{result.page_id}"
        note_key = f"basket_note_{result.page_id}"
        if selection_key not in st.session_state:
            st.session_state[selection_key] = default_selection
        if note_key not in st.session_state:
            st.session_state[note_key] = ""
        with st.container(border=True):
            heading, open_action, basket_action, evidence_action = st.columns(
                [4.2, 1, 1.1, 1.4]
            )
            title = result.document_title.strip() or "未命名文档"
            filename = result.filename.strip() or "未记录原始文件名"
            heading.markdown(f"### {index}. {title} · 第 {result.page_number} 页")
            heading.caption(
                f"原始文件：{filename}　|　页面状态：{result.status.label}"
            )
            existing_count = page_evidence_counts.get(result.page_id, 0)
            if existing_count:
                heading.caption(f"已加入证据篮：本页 {existing_count} 条选区证据")
            if open_action.button(
                "打开页面",
                key=f"open_result_{result.page_id}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    target = validate_search_target(database, result)
                    if target.warnings:
                        raise SearchNavigationError("；".join(target.warnings))
                except SearchNavigationError as exc:
                    st.error(f"无法打开该搜索结果：{exc}")
                else:
                    st.session_state["search_result_page"] = current_page
                    st.query_params.clear()
                    st.query_params.update(reader_query_params(result, active_query))
                    st.switch_page("pages/2_浏览资料.py")
            if basket_action.button(
                "加入证据篮",
                key=f"add_basket_result_{result.page_id}",
                use_container_width=True,
            ):
                try:
                    basket_service.add_item(
                        document_id=result.document_id,
                        page_id=result.page_id,
                        evidence_text=str(st.session_state[selection_key]),
                        user_note=str(st.session_state[note_key]),
                    )
                except DuplicateEvidenceError as exc:
                    st.info(str(exc))
                except EvidenceBasketError as exc:
                    st.error(f"加入证据篮失败：{exc}")
                except Exception as exc:
                    LOGGER.exception("加入证据篮失败：page_id=%s", result.page_id)
                    st.error(f"加入证据篮失败：{exc}")
                else:
                    st.session_state["basket_flash"] = "证据已持久化加入证据篮。"
                    st.rerun()
            if evidence_action.button(
                "生成 / 复制证据包",
                key=f"evidence_result_{result.page_id}",
                use_container_width=True,
            ):
                evidence_packages[result.page_id] = evidence_builder.build(result)
                st.session_state["evidence_packages"] = evidence_packages

            projects = "、".join(result.projects) if result.projects else "未关联项目"
            tags = "、".join(result.tags) if result.tags else "未添加标签"
            match_sources = result.match_type or "页面内容"
            st.caption(f"所属项目：{projects}　|　标签：{tags}")
            st.caption(f"命中字段 / 来源：{match_sources}")
            st.markdown(
                search_service.highlighted_snippet(result, active_query)
                if result.snippet or result.content
                else snippet,
                unsafe_allow_html=True,
            )
            with st.expander("查看完整命中内容"):
                st.text(result.content or "（没有可用文本）")
            with st.expander("编辑加入证据篮的选区与备注"):
                st.text_area(
                    "证据文本",
                    key=selection_key,
                    height=140,
                    help=(
                        "可从当前结果复制或编辑具体段落。无法在 PDF/OCR 原文中匹配时，"
                        "系统会明确保存为未经原文匹配确认的用户摘录。"
                    ),
                )
                st.text_area("用户备注（可选）", key=note_key, height=90)
            if result.page_id in evidence_packages:
                with st.expander("引用证据包（右上角可复制）", expanded=True):
                    st.code(evidence_packages[result.page_id], language="markdown")

    st.divider()
    st.subheader("生成外部 AI 提示词（可选、手动复制）")
    st.caption("本功能只生成本地文本，不连接任何 AI 服务，也不读取 API Key。")
    question = st.text_area(
        "要交给外部 AI 回答的问题",
        height=100,
        key="prompt_question",
    )
    if st.button("生成引用提示词"):
        try:
            st.session_state["knowledge_prompt"] = PromptBuilder().build(question, results)
        except Exception as exc:
            LOGGER.exception("生成外部 AI 提示词失败")
            st.error(f"生成提示词失败：{exc}")
    prompt = st.session_state.get("knowledge_prompt")
    if prompt:
        st.text_area("提示词", value=prompt, height=500)
