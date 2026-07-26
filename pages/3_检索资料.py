"""Search local engineering knowledge with fast, reversible filtering."""

from __future__ import annotations

import logging
from dataclasses import replace

import streamlit as st
import streamlit.components.v1 as components

from src.batch_selection import BatchSelectionSource, build_visible_page_scope
from src.batch_ui import (
    clear_inactive_visible_batch_state,
    render_visible_batch_feedback,
    render_visible_page_batch_ui,
)
from src.classification_metadata import ClassificationDocumentSort
from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
)
from src.evidence_service import EvidencePackageBuilder
from src.models import (
    PageStatus,
    SearchField,
    SearchFilters,
    SearchResult,
    SearchSort,
    SearchViewMode,
)
from src.prompt_builder import PromptBuilder
from src.runtime import (
    application_classification_metadata_service,
    application_database,
    application_evidence_basket_service,
    application_page_batch_service,
)
from src.search_history import search_history_reload_html
from src.search_navigation import (
    SearchNavigationError,
    group_search_results,
    reader_query_params,
    state_for_result,
    validate_search_target,
)
from src.search_service import SearchService
from src.search_state import (
    SearchPageState,
    active_filter_labels,
    clear_search_filters,
    filter_named_options,
    has_search_state_params,
    parse_search_state,
    remove_search_filter,
    search_state_query_params,
)

LOGGER = logging.getLogger(__name__)
RESULTS_PER_PAGE = 10

st.set_page_config(page_title="检索资料｜工程知识库 v0.2.2", page_icon="🔎", layout="wide")
st.title("检索资料")
st.caption("筛选候选页面、快速判断相关性，并连续阅读全局或当前文档中的命中。")
st.markdown(
    """
    <style>
    mark { background: #fde68a; color: inherit; padding: 0.05rem 0.12rem; border-radius: 0.2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)
components.html(search_history_reload_html(), height=0, width=0)

pending_search_params = st.session_state.pop("pending_search_query_params", None)
if isinstance(pending_search_params, dict):
    pending_search_state = parse_search_state(pending_search_params)
    st.query_params.from_dict(search_state_query_params(pending_search_state))
    st.rerun()


def _state_signature(state: SearchPageState) -> tuple[object, ...]:
    """Return a stable comparison key for URL/session synchronization."""

    filters = state.filters
    return (
        state.query,
        filters.document_ids,
        filters.project_ids,
        filters.tag_ids,
        filters.statuses,
        filters.match_fields,
        filters.has_note,
        filters.evidence_basket_id,
        state.sort,
        state.limit,
        state.result_page,
        state.filters_open,
        state.view_mode,
        state.expanded_document_id,
        state.preview_page_id,
        state.focus_result,
    )


def _set_widget_state(state: SearchPageState) -> None:
    """Hydrate widgets before they are instantiated in the current run."""

    filters = state.filters
    st.session_state["search_query_input"] = state.query
    st.session_state["search_document_ids"] = list(filters.document_ids)
    st.session_state["search_project_ids"] = list(filters.project_ids)
    st.session_state["search_tag_ids"] = list(filters.tag_ids)
    st.session_state["search_status_values"] = [value.value for value in filters.statuses]
    st.session_state["search_field_values"] = [
        value.value for value in filters.match_fields
    ]
    st.session_state["search_has_note"] = filters.has_note
    st.session_state["search_in_basket"] = filters.evidence_basket_id is not None
    st.session_state["search_sort_value"] = state.sort.value
    st.session_state["search_limit"] = state.limit
    st.session_state["search_filters_open"] = state.filters_open
    st.session_state["search_view_mode"] = state.view_mode.value


def _search_with_state(state: SearchPageState) -> None:
    """Execute the active state and keep v0.0.5 session keys compatible."""

    st.session_state["knowledge_query"] = state.query
    st.session_state["knowledge_filters"] = state.filters
    st.session_state["knowledge_sort"] = state.sort.value
    st.session_state["search_result_page"] = state.result_page
    try:
        st.session_state["knowledge_results"] = search_service.search(
            state.query,
            limit=state.limit,
            filters=state.filters,
            sort_by=state.sort,
        )
    except Exception as exc:
        LOGGER.exception("全文检索失败：query=%r", state.query)
        st.session_state["search_error"] = f"检索失败：{exc}"
        st.session_state["knowledge_results"] = []
    else:
        st.session_state.pop("search_error", None)
    st.session_state.pop("knowledge_prompt", None)


def _activate_state(
    state: SearchPageState,
    *,
    remember_previous: bool = True,
    rerun: bool = True,
    refresh_results: bool = True,
) -> None:
    """Activate, serialize, and execute one canonical search state."""

    current = st.session_state.get("search_active_state")
    if remember_previous and isinstance(current, SearchPageState):
        if _state_signature(current) != _state_signature(state):
            st.session_state["search_previous_state"] = current
    st.session_state["search_active_state"] = state
    st.session_state["search_url_signature"] = _state_signature(state)
    st.session_state["search_widgets_need_sync"] = True
    st.query_params.from_dict(search_state_query_params(state))
    if refresh_results:
        _search_with_state(state)
    if rerun:
        st.rerun()


def _state_from_widgets(active: SearchPageState, basket_id: int) -> SearchPageState:
    """Build a typed state using only whitelisted widget values."""

    try:
        statuses = tuple(
            PageStatus(value) for value in st.session_state["search_status_values"]
        )
        fields = tuple(
            SearchField(value) for value in st.session_state["search_field_values"]
        )
        sort = SearchSort(st.session_state["search_sort_value"])
    except ValueError:
        statuses = ()
        fields = ()
        sort = SearchSort.RELEVANCE
    filters = SearchFilters(
        document_ids=tuple(int(value) for value in st.session_state["search_document_ids"]),
        project_ids=tuple(int(value) for value in st.session_state["search_project_ids"]),
        tag_ids=tuple(int(value) for value in st.session_state["search_tag_ids"]),
        statuses=statuses,
        match_fields=fields,
        has_note=bool(st.session_state["search_has_note"]),
        evidence_basket_id=(
            basket_id if st.session_state["search_in_basket"] else None
        ),
    )
    query = str(st.session_state["search_query_input"])[:500]
    keep_page = query == active.query and filters == active.filters
    return SearchPageState(
        query=query,
        filters=filters,
        sort=sort,
        limit=int(st.session_state["search_limit"]),
        result_page=active.result_page if keep_page else 1,
        filters_open=active.filters_open,
        view_mode=active.view_mode,
        expanded_document_id=active.expanded_document_id if keep_page else None,
        preview_page_id=active.preview_page_id if keep_page else None,
        focus_result=active.focus_result if keep_page else None,
    )


def _has_formal_filters(filters: SearchFilters) -> bool:
    return bool(
        filters.document_ids
        or filters.project_ids
        or filters.tag_ids
        or filters.statuses
        or filters.match_fields
        or filters.has_note
        or filters.evidence_basket_id is not None
    )


def _sanitize_metadata_ids(
    state: SearchPageState,
    *,
    document_ids: set[int],
    project_ids: set[int],
    tag_ids: set[int],
) -> SearchPageState:
    """Drop stale URL metadata IDs before creating Streamlit widgets."""

    filters = state.filters
    return replace(
        state,
        filters=replace(
            filters,
            document_ids=tuple(value for value in filters.document_ids if value in document_ids),
            project_ids=tuple(value for value in filters.project_ids if value in project_ids),
            tag_ids=tuple(value for value in filters.tag_ids if value in tag_ids),
        ),
    )


try:
    database = application_database()
    basket_service = application_evidence_basket_service()
    page_batch_service = application_page_batch_service()
    classification_metadata_service = application_classification_metadata_service()
    search_service = SearchService(database)
    evidence_builder = EvidencePackageBuilder()
    classification_metadata = classification_metadata_service.load(
        document_sort=ClassificationDocumentSort.NAME_ASC
    )
    all_documents = classification_metadata.documents
    all_projects = classification_metadata.projects
    all_tags = classification_metadata.tags
    current_basket = basket_service.default_basket()
except Exception as exc:
    LOGGER.exception("初始化检索服务失败")
    st.error(f"初始化检索服务失败：{exc}")
    st.stop()

if not all_documents:
    st.info("知识库中还没有文档，因此暂无可检索内容。请先导入第一份 PDF。")
    if st.button("📥 前往导入资料", use_container_width=True):
        st.switch_page("pages/1_导入资料.py")

document_names = {
    document.id: document.title.strip() or document.filename.strip() or f"文档 {document.id}"
    for document in all_documents
}
document_lookup_names = {
    document.id: f"{document_names[document.id]} {document.filename}"
    for document in all_documents
}
project_names = {project.id: project.name for project in all_projects}
tag_names = {tag.id: tag.name for tag in all_tags}

# A changed URL is authoritative. This restores refresh and browser history state.
url_has_state = has_search_state_params(st.query_params)
url_state = _sanitize_metadata_ids(
    parse_search_state(st.query_params),
    document_ids=set(document_names),
    project_ids=set(project_names),
    tag_ids=set(tag_names),
)
url_signature = _state_signature(url_state)
if url_has_state and st.session_state.get("search_url_signature") != url_signature:
    st.session_state["search_active_state"] = url_state
    st.session_state["search_url_signature"] = url_signature
    _set_widget_state(url_state)
    _search_with_state(url_state)
    if dict(st.query_params) != search_state_query_params(url_state):
        st.query_params.from_dict(search_state_query_params(url_state))
elif "search_active_state" not in st.session_state:
    legacy_filters = st.session_state.get("knowledge_filters", SearchFilters())
    if not isinstance(legacy_filters, SearchFilters):
        legacy_filters = SearchFilters()
    try:
        legacy_sort = SearchSort(
            st.session_state.get("knowledge_sort", SearchSort.RELEVANCE.value)
        )
    except ValueError:
        legacy_sort = SearchSort.RELEVANCE
    initial_state = _sanitize_metadata_ids(
        SearchPageState(
            query=str(st.session_state.get("knowledge_query", ""))[:500],
            filters=legacy_filters,
            sort=legacy_sort,
            limit=int(st.session_state.get("search_limit", 50)),
            result_page=max(int(st.session_state.get("search_result_page", 1)), 1),
        ),
        document_ids=set(document_names),
        project_ids=set(project_names),
        tag_ids=set(tag_names),
    )
    st.session_state["search_active_state"] = initial_state
    st.session_state["search_url_signature"] = _state_signature(initial_state)
    _set_widget_state(initial_state)
    if initial_state.query:
        _search_with_state(initial_state)

active_state = st.session_state["search_active_state"]
if st.session_state.pop("search_widgets_need_sync", False):
    _set_widget_state(active_state)

for key, value in {
    "search_query_input": active_state.query,
    "search_document_ids": list(active_state.filters.document_ids),
    "search_project_ids": list(active_state.filters.project_ids),
    "search_tag_ids": list(active_state.filters.tag_ids),
    "search_status_values": [value.value for value in active_state.filters.statuses],
    "search_field_values": [value.value for value in active_state.filters.match_fields],
    "search_has_note": active_state.filters.has_note,
    "search_in_basket": active_state.filters.evidence_basket_id is not None,
    "search_sort_value": active_state.sort.value,
    "search_limit": active_state.limit,
    "search_filters_open": active_state.filters_open,
    "search_view_mode": active_state.view_mode.value,
    "search_document_finder": "",
    "search_project_finder": "",
    "search_tag_finder": "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = value.copy() if isinstance(value, list) else value

try:
    facet_counts = search_service.facet_counts(
        active_state.query, filters=active_state.filters
    )
    basket_items = basket_service.list_items(current_basket.id)
except Exception as exc:
    LOGGER.exception("读取筛选计数或证据篮失败")
    st.error(f"读取筛选计数或证据篮失败：{exc}")
    st.stop()

basket_items_by_page: dict[int, list[object]] = {}
for basket_item in basket_items:
    basket_items_by_page.setdefault(basket_item.page_id, []).append(basket_item)

entry_columns = st.columns([5, 1])
entry_columns[0].caption(
    f"当前上下文匹配 {facet_counts.total} 页；选项数字表示选择该候选后可保留的页面量。"
)
if entry_columns[1].button(
    f"查看证据篮（{len(basket_items)}）",
    use_container_width=True,
    key="open_evidence_basket",
):
    st.switch_page("pages/9_证据篮.py")

basket_flash = st.session_state.pop("basket_flash", "")
if basket_flash:
    st.success(basket_flash)
render_visible_batch_feedback()

# Active filters stay compact and only render when at least one condition exists.
active_labels = active_filter_labels(
    active_state,
    document_names=document_names,
    project_names=project_names,
    tag_names=tag_names,
)
if active_labels:
    with st.container(border=True):
        st.caption(f"当前生效筛选（{len(active_labels)} 项，可逐项移除）")
        for row_start in range(0, len(active_labels), 4):
            columns = st.columns(4)
            for column, item in zip(
                columns, active_labels[row_start : row_start + 4], strict=False
            ):
                if column.button(
                    f"移除 {item.label}",
                    key=f"remove_filter_{item.kind}_{item.value}",
                    help=f"只移除“{item.label}”，保留其他搜索状态",
                    use_container_width=True,
                ):
                    _activate_state(
                        remove_search_filter(active_state, item.kind, item.value)
                    )
        clear_columns = st.columns([1, 1, 2])
        if clear_columns[0].button(
            "仅清除筛选（保留关键词）",
            key="clear_filters_keep_query",
            use_container_width=True,
        ):
            _activate_state(clear_search_filters(active_state, keep_query=True))
        if clear_columns[1].button(
            "清除所有条件（含关键词）",
            key="clear_filters_and_query",
            use_container_width=True,
        ):
            _activate_state(clear_search_filters(active_state, keep_query=False))

shortcut_columns = st.columns(6)
shortcut_actions = (
    ("仅看已复核", replace(active_state.filters, statuses=(PageStatus.REVIEWED,)), None),
    (
        "仅看待复核",
        replace(
            active_state.filters,
            statuses=(PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED),
        ),
        None,
    ),
    ("仅看有笔记", replace(active_state.filters, has_note=True), None),
    (
        "仅看当前证据篮",
        replace(active_state.filters, evidence_basket_id=current_basket.id),
        None,
    ),
    ("最近查看", None, SearchSort.VIEWED_DESC),
    ("最近修改", None, SearchSort.UPDATED_DESC),
)
for index, (label, shortcut_filters, shortcut_sort) in enumerate(shortcut_actions):
    if shortcut_columns[index].button(
        label,
        key=f"search_shortcut_{index}",
        help="使用与普通筛选相同的查询状态，可继续叠加或逐项移除",
        use_container_width=True,
    ):
        next_state = replace(
            active_state,
            filters=shortcut_filters or active_state.filters,
            sort=shortcut_sort or active_state.sort,
            result_page=1,
        )
        _activate_state(next_state)

filters_open = st.toggle(
    "展开筛选条件",
    key="search_filters_open",
    help="展开时显示高频筛选和搜索范围；收起不会清除任何条件",
)
if filters_open != active_state.filters_open:
    _activate_state(
        replace(active_state, filters_open=filters_open), remember_previous=False
    )

st.text_input(
    "搜索内容",
    key="search_query_input",
    placeholder="例如：液压泵 异常噪声",
    help="多个搜索词使用 OR 查询；筛选项快速查找不会改变这里的关键词",
)

if active_state.filters_open:
    st.markdown("#### 高频筛选")
    finder_columns = st.columns(3)
    finder_columns[0].text_input(
        "快速查找文档",
        key="search_document_finder",
        placeholder="输入标题或文件名的一部分",
    )
    finder_columns[1].text_input(
        "快速查找项目",
        key="search_project_finder",
        placeholder="支持中文和特殊字符",
    )
    finder_columns[2].text_input(
        "快速查找标签",
        key="search_tag_finder",
        placeholder="清空后恢复全部选项",
    )
    visible_document_ids = filter_named_options(
        document_lookup_names,
        str(st.session_state["search_document_finder"]),
        selected_ids=st.session_state["search_document_ids"],
    )
    visible_project_ids = filter_named_options(
        project_names,
        str(st.session_state["search_project_finder"]),
        selected_ids=st.session_state["search_project_ids"],
    )
    visible_tag_ids = filter_named_options(
        tag_names,
        str(st.session_state["search_tag_finder"]),
        selected_ids=st.session_state["search_tag_ids"],
    )
    filter_columns = st.columns(4)
    filter_columns[0].multiselect(
        "文档",
        options=visible_document_ids,
        format_func=lambda value: (
            f"{document_names[value]}（{facet_counts.documents.get(value, 0)}）"
        ),
        key="search_document_ids",
    )
    filter_columns[1].multiselect(
        "项目（同时满足，AND）",
        options=visible_project_ids,
        format_func=lambda value: (
            f"{project_names[value]}（{facet_counts.projects.get(value, 0)}）"
        ),
        key="search_project_ids",
    )
    filter_columns[2].multiselect(
        "标签（同时满足，AND）",
        options=visible_tag_ids,
        format_func=lambda value: f"{tag_names[value]}（{facet_counts.tags.get(value, 0)}）",
        key="search_tag_ids",
    )
    filter_columns[3].multiselect(
        "复核状态",
        options=[status.value for status in PageStatus],
        format_func=lambda value: (
            f"{PageStatus(value).label}"
            f"（{facet_counts.statuses.get(PageStatus(value), 0)}）"
        ),
        key="search_status_values",
    )
    finder_messages = (
        ("文档", document_lookup_names, "search_document_finder"),
        ("项目", project_names, "search_project_finder"),
        ("标签", tag_names, "search_tag_finder"),
    )
    for kind, names, finder_key in finder_messages:
        finder_value = str(st.session_state[finder_key])
        if finder_value and not filter_named_options(names, finder_value):
            st.caption(f"{kind}快速查找没有匹配项；清空输入可恢复全部选项。")

    with st.expander("搜索范围与其他低频条件", expanded=False):
        low_frequency = st.columns(3)
        low_frequency[0].multiselect(
            "搜索范围 / 命中字段",
            options=[field.value for field in SearchField],
            format_func=lambda value: SearchField(value).label,
            key="search_field_values",
            help="不选择表示搜索全部正文、OCR、笔记和元数据字段",
        )
        with low_frequency[1]:
            st.checkbox("仅看有笔记", key="search_has_note")
            st.checkbox(
                "仅看已加入当前证据篮的页面",
                key="search_in_basket",
            )
        low_frequency[2].slider(
            "最多加载结果",
            min_value=10,
            max_value=100,
            step=10,
            key="search_limit",
            help="完整匹配总数仍会显示；此设置只限制当前加载的结果卡片",
        )

sort_columns = st.columns([2, 1, 3])
sort_columns[0].selectbox(
    "排序方式",
    options=[sort.value for sort in SearchSort],
    format_func=lambda value: SearchSort(value).label,
    key="search_sort_value",
)
apply_search = sort_columns[1].button(
    "搜索 / 应用筛选",
    type="primary",
    use_container_width=True,
    key="apply_search_filters",
)
sort_columns[2].caption(
    "筛选选项查找只缩小候选列表；点击“搜索 / 应用筛选”后才改变正式结果。"
)

if apply_search:
    _activate_state(_state_from_widgets(active_state, current_basket.id))

view_mode_value = st.radio(
    "搜索结果视图",
    options=[mode.value for mode in SearchViewMode],
    format_func=lambda value: SearchViewMode(value).label,
    key="search_view_mode",
    horizontal=True,
    help="页面视图保持原有分页卡片；文档视图按当前有序结果稳定分组。",
)
if SearchViewMode(view_mode_value) is not active_state.view_mode:
    next_mode = SearchViewMode(view_mode_value)
    _activate_state(
        replace(
            active_state,
            view_mode=next_mode,
            expanded_document_id=(
                active_state.expanded_document_id
                if next_mode is SearchViewMode.DOCUMENT
                else None
            ),
        ),
        remember_previous=False,
        refresh_results=False,
    )

search_error = st.session_state.pop("search_error", "")
if search_error:
    st.error(search_error)

results = st.session_state.get("knowledge_results", [])
batch_rerun_requested = False
if not results or active_state.view_mode is not SearchViewMode.PAGE:
    if clear_inactive_visible_batch_state():
        st.info("页面范围已变化，原批量选择已清除。")
active_query_terms = search_service.query_terms(active_state.query)
has_searched = bool(active_state.query.strip())
effective_total = facet_counts.total if active_query_terms else 0

if has_searched and not active_query_terms:
    st.warning("关键词没有可检索的文字，请输入中文、英文或数字后重试。")
elif has_searched and not results:
    unfiltered_count = search_service.facet_counts(active_state.query).total
    scope_count = 0
    if active_state.filters.match_fields:
        scope_count = search_service.facet_counts(
            active_state.query,
            filters=replace(active_state.filters, match_fields=()),
        ).total
    if unfiltered_count == 0:
        reason = "关键词在全部可搜索字段中都没有命中。"
    elif scope_count > 0:
        reason = "关键词存在命中，但当前搜索范围过窄。"
    elif _has_formal_filters(active_state.filters):
        reason = "关键词存在命中，但当前筛选组合过严。"
    else:
        reason = "当前最多加载结果设置或资料状态没有返回可显示页面。"
    st.info(f"没有结果：{reason}可撤销最近条件、仅清除筛选或调整关键词。")
    recovery_columns = st.columns(3)
    previous_state = st.session_state.get("search_previous_state")
    if recovery_columns[0].button(
        "撤销最近筛选",
        disabled=not isinstance(previous_state, SearchPageState),
        key="undo_empty_search",
        use_container_width=True,
    ):
        _activate_state(previous_state, remember_previous=False)
    if recovery_columns[1].button(
        "仅清除筛选（保留关键词）",
        key="clear_empty_search_filters",
        use_container_width=True,
    ):
        _activate_state(clear_search_filters(active_state, keep_query=True))
    if recovery_columns[2].button(
        "清除所有条件",
        key="clear_empty_search_all",
        use_container_width=True,
    ):
        _activate_state(clear_search_filters(active_state, keep_query=False))

if results and active_state.view_mode is SearchViewMode.PAGE:
    loaded_count = len(results)
    page_count = (loaded_count + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    current_page = min(max(active_state.result_page, 1), page_count)
    if current_page != active_state.result_page:
        active_state = replace(active_state, result_page=current_page)
        st.session_state["search_active_state"] = active_state
        st.session_state["search_result_page"] = current_page
        st.session_state["search_url_signature"] = _state_signature(active_state)
        st.query_params.from_dict(search_state_query_params(active_state))
    heading_columns = st.columns([4, 1, 1])
    heading_columns[0].subheader(f"搜索结果（共 {effective_total} 个页面）")
    if heading_columns[1].button(
        "← 上一组",
        disabled=current_page <= 1,
        use_container_width=True,
        key="search_previous_page",
    ):
        _activate_state(
            replace(active_state, result_page=current_page - 1),
            remember_previous=False,
            refresh_results=False,
        )
    if heading_columns[2].button(
        "下一组 →",
        disabled=current_page >= page_count,
        use_container_width=True,
        key="search_next_page",
    ):
        _activate_state(
            replace(active_state, result_page=current_page + 1),
            remember_previous=False,
            refresh_results=False,
        )
    st.caption(
        f"已加载 {loaded_count} 页；第 {current_page} / {page_count} 组；"
        f"排序：{active_state.sort.label}。"
    )
    if effective_total > loaded_count:
        st.caption(
            f"另有 {effective_total - loaded_count} 页未加载；"
            "可在低频条件中提高加载上限或继续筛选。"
        )

    start = (current_page - 1) * RESULTS_PER_PAGE
    visible_results = results[start : start + RESULTS_PER_PAGE]
    batch_scope = build_visible_page_scope(
        source=BatchSelectionSource.SEARCH,
        document_id=(
            active_state.filters.document_ids[0]
            if len(active_state.filters.document_ids) == 1
            else None
        ),
        filters={
            "document_ids": tuple(sorted(active_state.filters.document_ids)),
            "project_ids": tuple(sorted(active_state.filters.project_ids)),
            "tag_ids": tuple(sorted(active_state.filters.tag_ids)),
            "statuses": tuple(
                sorted(value.value for value in active_state.filters.statuses)
            ),
            "match_fields": tuple(
                sorted(value.value for value in active_state.filters.match_fields)
            ),
            "has_note": active_state.filters.has_note,
            "evidence_basket_id": active_state.filters.evidence_basket_id,
            "limit": active_state.limit,
            "view_mode": active_state.view_mode.value,
        },
        sort=active_state.sort.value,
        query=active_state.query,
        batch_number=current_page,
        visible_page_ids=[result.page_id for result in visible_results],
    )
    batch_rerun_requested = render_visible_page_batch_ui(
        scope=batch_scope,
        page_labels={
            result.page_id: (
                f"{result.document_title.strip() or '未命名文档'} · "
                f"第 {result.page_number} 页 · {result.status.label}"
            )
            for result in visible_results
        },
        service=page_batch_service,
        tags=all_tags,
        projects=all_projects,
        on_finished=lambda: _search_with_state(active_state),
    )
    evidence_packages = dict(st.session_state.get("evidence_packages", {}))
    for index, result in enumerate(visible_results, start=start + 1):
        snippet = result.snippet or result.content[:220] or "（该页没有可显示的文本摘要）"
        default_selection = (result.snippet or result.content).strip()
        default_selection = default_selection.removeprefix("“").removesuffix("”").strip()
        selection_key = f"basket_selection_{result.page_id}"
        note_key = f"basket_note_{result.page_id}"
        if selection_key not in st.session_state:
            st.session_state[selection_key] = default_selection
        if note_key not in st.session_state:
            st.session_state[note_key] = ""
        with st.container(border=True):
            heading, open_action, preview_action, basket_action, evidence_action = st.columns(
                [4.1, 1, 1.35, 1.1, 1.4]
            )
            title = result.document_title.strip() or "未命名文档"
            filename = result.filename.strip() or "未记录原始文件名"
            focus_label = "　← 返回位置" if active_state.focus_result == index else ""
            heading.markdown(
                f"### {index}. {title} · 第 {result.page_number} 页{focus_label}"
            )
            heading.caption(f"原始文件：{filename}　|　页面状态：{result.status.label}")
            current_page_items = basket_items_by_page.get(result.page_id, [])
            if current_page_items:
                heading.caption(f"已加入证据篮：本页 {len(current_page_items)} 条选区证据")
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
                    return_state = state_for_result(
                        active_state,
                        result_index=index,
                        document_id=result.document_id,
                        results_per_page=RESULTS_PER_PAGE,
                    )
                    st.session_state["search_result_page"] = current_page
                    navigation_params = reader_query_params(
                        result,
                        active_state.query,
                        return_state=return_state,
                    )
                    st.session_state["pending_reader_query_params"] = navigation_params
                    st.query_params.from_dict(navigation_params)
                    st.switch_page("pages/2_浏览资料.py")
            preview_open = active_state.preview_page_id == result.page_id
            if preview_action.button(
                "关闭快速预览" if preview_open else "打开快速预览",
                key=f"toggle_preview_{result.page_id}",
                use_container_width=True,
            ):
                _activate_state(
                    replace(
                        active_state,
                        preview_page_id=None if preview_open else result.page_id,
                        focus_result=index,
                    ),
                    remember_previous=False,
                    refresh_results=False,
                )
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
                        basket_id=current_basket.id,
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
                    _search_with_state(active_state)
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
            st.caption(
                f"本页关键词字面命中 {result.match_count} 次（重叠词组按最长项计一次）；"
                f"显示 {len(result.snippets)} 个去重片段。"
            )
            if result.snippets:
                for snippet_number, match_snippet in enumerate(result.snippets, start=1):
                    st.caption(
                        f"片段 {snippet_number} · {match_snippet.field.label} · "
                        f"该字段命中 {match_snippet.match_count} 次"
                    )
                    st.markdown(
                        search_service.highlighted_snippet(
                            replace(result, snippet=match_snippet.text), active_state.query
                        ),
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("该页没有可用命中上下文；仍可打开阅读页核对页面图像。")
            if preview_open:
                st.markdown("#### 快速预览（仅加载当前这一页）")
                preview_image, preview_text = st.columns([1, 2.4])
                with preview_image:
                    if result.image_path.is_file():
                        st.image(str(result.image_path), caption=f"第 {result.page_number} 页")
                    else:
                        st.warning(f"页面图像缺失：{result.image_path}")
                with preview_text:
                    longer_text = (
                        result.markdown_content.strip()
                        or result.ocr_text.strip()
                        or result.extracted_text.strip()
                        or result.content.strip()
                    )
                    if longer_text:
                        st.text(longer_text[:1800])
                        if len(longer_text) > 1800:
                            st.caption("快速预览最多显示 1800 字；完整内容请打开阅读页。")
                    else:
                        st.info("该页没有可用 Markdown、OCR 或提取文本。")
                    st.caption(
                        "页面笔记 / Markdown 已存在。"
                        if result.markdown_content.strip()
                        else "该页尚无 Markdown 笔记。"
                    )
                    st.caption(
                        f"复核状态：{result.status.label}；"
                        f"证据篮：{'已加入' if current_page_items else '未加入'}。"
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
            if current_page_items:
                with st.expander("管理本页已有证据", expanded=False):
                    for stored_item in current_page_items:
                        stored_note_key = f"search_evidence_note_{stored_item.id}"
                        if stored_note_key not in st.session_state:
                            st.session_state[stored_note_key] = stored_item.user_note
                        st.text_area(
                            f"证据 {stored_item.id} 的备注",
                            key=stored_note_key,
                            height=80,
                        )
                        note_action, remove_action = st.columns(2)
                        if note_action.button(
                            "保存证据备注",
                            key=f"save_search_evidence_note_{stored_item.id}",
                            use_container_width=True,
                        ):
                            try:
                                basket_service.update_note(
                                    stored_item.id,
                                    str(st.session_state[stored_note_key]),
                                    basket_id=current_basket.id,
                                )
                            except EvidenceBasketError as exc:
                                st.error(f"保存证据备注失败：{exc}")
                            else:
                                st.session_state["basket_flash"] = "证据备注已保存。"
                                _search_with_state(active_state)
                                st.rerun()
                        if remove_action.button(
                            "移除这条证据",
                            key=f"remove_search_evidence_{stored_item.id}",
                            help="只删除证据篮条目，不删除原始 PDF、页面图像或页面笔记",
                            use_container_width=True,
                        ):
                            try:
                                basket_service.remove_item(
                                    stored_item.id, basket_id=current_basket.id
                                )
                            except EvidenceBasketError as exc:
                                st.error(f"移除证据失败：{exc}")
                            else:
                                st.session_state["basket_flash"] = "已移除一条证据。"
                                _search_with_state(active_state)
                                st.rerun()
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


def _render_group_result_card(result: SearchResult, result_index: int) -> None:
    """Render one expanded document-group result with on-demand preview."""

    selection_key = f"basket_selection_{result.page_id}"
    note_key = f"basket_note_{result.page_id}"
    st.session_state.setdefault(
        selection_key, (result.snippet or result.content).strip(" …“”")
    )
    st.session_state.setdefault(note_key, "")
    current_items = basket_items_by_page.get(result.page_id, [])
    preview_open = active_state.preview_page_id == result.page_id
    with st.container(border=True):
        heading, open_action, preview_action, basket_action = st.columns(
            [4.4, 1, 1.35, 1.1]
        )
        heading.markdown(
            f"#### 全局第 {result_index} 项 · 第 {result.page_number} 页"
            + ("　← 返回位置" if active_state.focus_result == result_index else "")
        )
        heading.caption(
            f"状态：{result.status.label}　|　"
            f"证据：{'已加入 ' + str(len(current_items)) + ' 条' if current_items else '未加入'}"
        )
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
                return_state = state_for_result(
                    active_state,
                    result_index=result_index,
                    document_id=result.document_id,
                    results_per_page=RESULTS_PER_PAGE,
                )
                navigation_params = reader_query_params(
                    result, active_state.query, return_state=return_state
                )
                st.session_state["pending_reader_query_params"] = navigation_params
                st.query_params.from_dict(navigation_params)
                st.switch_page("pages/2_浏览资料.py")
        if preview_action.button(
            "关闭快速预览" if preview_open else "打开快速预览",
            key=f"toggle_preview_{result.page_id}",
            use_container_width=True,
        ):
            _activate_state(
                replace(
                    active_state,
                    preview_page_id=None if preview_open else result.page_id,
                    focus_result=result_index,
                ),
                remember_previous=False,
                refresh_results=False,
            )
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
                    basket_id=current_basket.id,
                )
            except DuplicateEvidenceError as exc:
                st.info(str(exc))
            except EvidenceBasketError as exc:
                st.error(f"加入证据篮失败：{exc}")
            else:
                st.session_state["basket_flash"] = "证据已持久化加入证据篮。"
                st.rerun()

        st.caption(
            f"命中字段：{result.match_type or '页面内容'}；"
            f"关键词字面命中 {result.match_count} 次；显示 {len(result.snippets)} 个去重片段。"
        )
        for snippet_number, match_snippet in enumerate(result.snippets, start=1):
            st.caption(f"片段 {snippet_number} · {match_snippet.field.label}")
            st.markdown(
                search_service.highlighted_snippet(
                    replace(result, snippet=match_snippet.text), active_state.query
                ),
                unsafe_allow_html=True,
            )
        if not result.snippets:
            st.caption("没有可用文本上下文；可打开阅读页核对原图。")

        if preview_open:
            st.markdown("##### 快速预览（图像与长文本按需加载）")
            preview_image, preview_text = st.columns([1, 2.4])
            with preview_image:
                if result.image_path.is_file():
                    st.image(str(result.image_path), caption=f"第 {result.page_number} 页")
                else:
                    st.warning(f"页面图像缺失：{result.image_path}")
            with preview_text:
                longer_text = (
                    result.markdown_content.strip()
                    or result.ocr_text.strip()
                    or result.extracted_text.strip()
                    or result.content.strip()
                )
                if longer_text:
                    st.text(longer_text[:1800])
                else:
                    st.info("该页没有可用 Markdown、OCR 或提取文本。")
                st.caption(
                    f"项目：{'、'.join(result.projects) or '未关联'}　|　"
                    f"标签：{'、'.join(result.tags) or '未添加'}"
                )
                st.caption(
                    "页面 Markdown 已存在。"
                    if result.markdown_content.strip()
                    else "该页尚无 Markdown。"
                )
            st.text_area("证据文本", key=selection_key, height=130)
            st.text_area("证据备注（可选）", key=note_key, height=80)
            evidence_packages = dict(st.session_state.get("evidence_packages", {}))
            if st.button(
                "生成 / 复制单页证据包",
                key=f"evidence_result_{result.page_id}",
                use_container_width=True,
            ):
                evidence_packages[result.page_id] = evidence_builder.build(result)
                st.session_state["evidence_packages"] = evidence_packages
            for stored_item in current_items:
                stored_note_key = f"search_evidence_note_{stored_item.id}"
                st.session_state.setdefault(stored_note_key, stored_item.user_note)
                st.text_area(
                    f"已存证据 {stored_item.id} 的备注",
                    key=stored_note_key,
                    height=75,
                )
                save_action, remove_action = st.columns(2)
                if save_action.button(
                    "保存证据备注",
                    key=f"save_search_evidence_note_{stored_item.id}",
                    use_container_width=True,
                ):
                    try:
                        basket_service.update_note(
                            stored_item.id,
                            str(st.session_state[stored_note_key]),
                            basket_id=current_basket.id,
                        )
                    except EvidenceBasketError as exc:
                        st.error(f"保存证据备注失败：{exc}")
                    else:
                        st.session_state["basket_flash"] = "证据备注已保存。"
                        st.rerun()
                if remove_action.button(
                    "从证据篮移除",
                    key=f"remove_search_evidence_{stored_item.id}",
                    use_container_width=True,
                ):
                    try:
                        basket_service.remove_item(
                            stored_item.id, basket_id=current_basket.id
                        )
                    except EvidenceBasketError as exc:
                        st.error(f"移除证据失败：{exc}")
                    else:
                        st.session_state["basket_flash"] = "已移除一条证据。"
                        st.rerun()
            if result.page_id in evidence_packages:
                st.code(evidence_packages[result.page_id], language="markdown")


if results and active_state.view_mode is SearchViewMode.DOCUMENT:
    loaded_count = len(results)
    st.subheader(f"搜索结果（共 {effective_total} 个页面）")
    st.caption(
        f"已加载 {loaded_count} 页；排序：{active_state.sort.label}；"
        "文档组按首个全局结果的位置排列，组内保持同一全局排序。"
    )
    if effective_total > loaded_count:
        st.warning(
            f"当前分组与导航覆盖前 {loaded_count} / {effective_total} 个结果；"
            "完整组计数仍按全部匹配页计算，未加载页不会被误作可导航项。"
        )
    try:
        exact_document_counts = search_service.document_counts(
            active_state.query, filters=active_state.filters
        )
    except Exception as exc:
        LOGGER.exception("读取文档分组计数失败")
        st.warning(f"完整文档计数暂不可用，将显示已加载数量：{exc}")
        exact_document_counts = {}
    groups = group_search_results(results, document_counts=exact_document_counts)
    result_positions = {
        result.page_id: index for index, result in enumerate(results, start=1)
    }
    for group_number, group in enumerate(groups, start=1):
        with st.container(border=True):
            group_heading, group_action = st.columns([5, 1])
            group_title = group.document_title.strip() or "异常文档记录"
            group_heading.markdown(f"### {group_number}. {group_title}")
            group_heading.caption(
                f"来源：{group.filename.strip() or '未记录来源文件名'}　|　"
                f"命中 {group.total_count} 个页面　|　"
                f"最相关页：第 {group.best_result.page_number} 页"
            )
            fields = tuple(
                dict.fromkeys(
                    field for result in group.results for field in result.match_fields
                )
            )
            projects = tuple(
                dict.fromkeys(name for result in group.results for name in result.projects)
            )
            tags = tuple(
                dict.fromkeys(name for result in group.results for name in result.tags)
            )
            group_heading.caption(
                f"主要命中字段：{'、'.join(field.label for field in fields) or '无可用字段'}"
            )
            group_heading.caption(
                f"项目：{'、'.join(projects) or '未关联'}　|　"
                f"标签：{'、'.join(tags) or '未添加'}"
            )
            expanded = active_state.expanded_document_id == group.document_id
            if group_action.button(
                "收起命中页" if expanded else "展开命中页",
                key=f"toggle_group_{group.document_id}",
                use_container_width=True,
            ):
                _activate_state(
                    replace(
                        active_state,
                        expanded_document_id=None if expanded else group.document_id,
                        preview_page_id=None,
                    ),
                    remember_previous=False,
                    refresh_results=False,
                )
            if expanded:
                if group.total_count > len(group.results):
                    st.info(
                        f"本组当前加载 {len(group.results)} / {group.total_count} 页。"
                    )
                for result in group.results:
                    _render_group_result_card(
                        result, result_positions[result.page_id]
                    )

if batch_rerun_requested:
    st.rerun()
