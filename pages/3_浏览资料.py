"""Filter documents and use the two-column page reader and Markdown editor."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import streamlit as st

from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
)
from src.models import ImportStatus, Page, SearchResult
from src.note_service import NoteService
from src.note_ui import render_structured_notes_tab
from src.runtime import (
    application_database,
    application_document_service,
    application_evidence_basket_service,
)
from src.search_navigation import (
    document_hit_results,
    locate_result,
    reader_query_params,
    state_for_result,
    unique_ordered_results,
)
from src.search_service import SearchService
from src.search_state import (
    SearchPageState,
    active_filter_labels,
    decode_return_state,
    encode_return_state,
    search_state_query_params,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="浏览资料｜工程知识库 v0.6.0", page_icon="📖", layout="wide")
st.title("文档与页面")
st.caption("筛选本地文档，在同一界面阅读原图、编辑笔记并组织标签与项目。")


def decode_markdown(file_bytes: bytes) -> str:
    """Decode an uploaded Markdown file with common UTF encodings."""

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Markdown 文件不是可识别的 UTF-8 或 GB18030 文本。")


try:
    database = application_database()
    document_service = application_document_service()
    basket_service = application_evidence_basket_service()
    search_service = SearchService(database)
    note_service = NoteService(database)
    all_tags = database.list_tags()
    all_projects = database.list_projects()
except Exception as exc:
    LOGGER.exception("读取资料失败")
    st.error(f"读取资料失败：{exc}")
    st.stop()


def _open_search_result(
    result: SearchResult,
    *,
    result_index: int,
    return_state: SearchPageState,
) -> None:
    """Navigate to a loaded search result while retaining compact return state."""

    next_return_state = state_for_result(
        return_state,
        result_index=result_index,
        document_id=result.document_id,
    )
    st.query_params.from_dict(
        reader_query_params(
            result,
            next_return_state.query,
            return_state=next_return_state,
        )
    )
    st.rerun()

basket_flash = st.session_state.pop("basket_flash", "")
if basket_flash:
    st.success(basket_flash)

pending_reader_params = st.session_state.pop("pending_reader_query_params", None)
if isinstance(pending_reader_params, dict):
    try:
        pending_document_id = int(pending_reader_params.get("document", 0))
        pending_page_number = int(pending_reader_params.get("page", 0))
    except (TypeError, ValueError):
        pending_document_id = 0
        pending_page_number = 0
    if pending_document_id > 0 and pending_page_number > 0:
        safe_pending_reader_params = {
            "document": str(pending_document_id),
            "page": str(pending_page_number),
        }
        if str(pending_reader_params.get("from_search", "1")) == "1":
            pending_reader_state = decode_return_state(
                str(pending_reader_params.get("search_return", ""))
            )
            safe_pending_reader_params.update(
                {
                    "from_search": "1",
                    "search_query": pending_reader_state.query[:500],
                    "search_return": encode_return_state(pending_reader_state),
                }
            )
        st.query_params.from_dict(safe_pending_reader_params)
        st.rerun()

with st.expander("文档筛选与排序", expanded=True):
    filter_columns = st.columns(4)
    sort_by = filter_columns[0].selectbox(
        "排序",
        options=["imported_desc", "updated_desc", "name_asc", "name_desc", "imported_asc"],
        format_func=lambda value: {
            "imported_desc": "导入时间（新到旧）",
            "updated_desc": "更新时间（新到旧）",
            "name_asc": "名称（A→Z）",
            "name_desc": "名称（Z→A）",
            "imported_asc": "导入时间（旧到新）",
        }[value],
    )
    selected_tag_ids = filter_columns[1].multiselect(
        "标签（同时满足）",
        options=[tag.id for tag in all_tags],
        format_func=lambda value: next(tag.name for tag in all_tags if tag.id == value),
    )
    selected_project_ids = filter_columns[2].multiselect(
        "项目（同时满足）",
        options=[project.id for project in all_projects],
        format_func=lambda value: next(
            project.name for project in all_projects if project.id == value
        ),
    )
    status_value = filter_columns[3].selectbox(
        "导入状态",
        options=[None, *list(ImportStatus)],
        format_func=lambda value: "全部" if value is None else value.value,
    )

documents = database.list_documents(
    sort_by=sort_by,
    tag_ids=selected_tag_ids,
    project_ids=selected_project_ids,
    import_status=status_value,
)
query_document = st.query_params.get("document")
from_search = st.query_params.get("from_search") == "1"
search_return = str(st.query_params.get("search_return", ""))[:8_000]
search_query_hint = str(st.query_params.get("search_query", ""))[:500]
search_return_state: SearchPageState | None = None
search_results: tuple[SearchResult, ...] = ()
search_total = 0
search_document_counts: dict[int, int] = {}
search_context_error = ""
if from_search:
    search_return_state = (
        decode_return_state(search_return)
        if search_return
        else SearchPageState(query=search_query_hint)
    )
    if not search_return_state.query and search_query_hint:
        search_return_state = replace(
            search_return_state, query=search_query_hint
        )
    try:
        search_results = unique_ordered_results(
            search_service.search(
                search_return_state.query,
                limit=search_return_state.limit,
                filters=search_return_state.filters,
                sort_by=search_return_state.sort,
            )
        )
        search_total = search_service.facet_counts(
            search_return_state.query,
            filters=search_return_state.filters,
        ).total
        search_document_counts = search_service.document_counts(
            search_return_state.query,
            filters=search_return_state.filters,
        )
    except Exception as exc:
        LOGGER.exception("重建阅读页搜索上下文失败")
        search_context_error = str(exc)
requested_document_id: int | None = None
if query_document:
    try:
        requested_document_id = int(query_document)
    except ValueError:
        st.error(f"无法打开指定文档：文档编号“{query_document}”无效。")
        st.stop()
    requested_document = database.get_document(requested_document_id)
    if requested_document is None:
        st.error(f"无法打开指定文档：数据库中不存在文档 {requested_document_id}。")
        st.stop()
    if all(document.id != requested_document.id for document in documents):
        documents = [requested_document, *documents]
if not documents:
    if database.list_documents():
        st.info("没有符合当前筛选条件的文档。请调整或清除上方筛选。")
    else:
        st.info("还没有可浏览的文档。请先导入第一份 PDF。")
        if st.button("📥 前往导入资料", use_container_width=True):
            st.switch_page("pages/1_导入资料.py")
    st.stop()

document_by_id = {document.id: document for document in documents}
initial_document = requested_document_id or documents[0].id
document_id = st.selectbox(
    "选择文档",
    options=list(document_by_id),
    index=list(document_by_id).index(initial_document),
    format_func=lambda value: (
        f"{document_by_id[value].title}（{document_by_id[value].page_count} 页）"
    ),
)
document = document_by_id[document_id]
document_changed = requested_document_id is not None and document_id != requested_document_id
st.query_params["document"] = str(document.id)
if document_changed:
    if "page" in st.query_params:
        del st.query_params["page"]

search_query = search_return_state.query if search_return_state is not None else ""
if from_search and search_return_state is not None:
    document_names = {
        item.id: item.title.strip() or item.filename for item in documents
    }
    project_names = {item.id: item.name for item in all_projects}
    tag_names = {item.id: item.name for item in all_tags}
    context_filters = active_filter_labels(
        search_return_state,
        document_names=document_names,
        project_names=project_names,
        tag_names=tag_names,
    )
    search_banner, return_action = st.columns([5, 1])
    compact_filters = "；".join(item.label for item in context_filters[:4])
    if len(context_filters) > 4:
        compact_filters += f"；另 {len(context_filters) - 4} 项"
    search_banner.info(
        f"当前页面来自检索：{search_query or '（未记录关键词）'}　|　"
        f"筛选：{compact_filters or '无'}　|　"
        f"排序：{search_return_state.sort.label}"
    )
    if len(context_filters) > 4:
        with search_banner.expander("查看全部搜索条件", expanded=False):
            for item in context_filters:
                st.caption(item.label)
    if search_context_error:
        search_banner.warning(
            f"搜索上下文暂时无法重建，命中导航已安全停用：{search_context_error}"
        )
    if return_action.button("返回检索结果", type="primary", use_container_width=True):
        if search_return:
            return_params = search_state_query_params(search_return_state)
            st.session_state["pending_search_query_params"] = return_params
            st.query_params.from_dict(return_params)
        else:
            # Preserve the v0.0.5 behavior for old reader URLs.
            st.query_params.clear()
        st.switch_page("pages/4_检索资料.py")
        st.stop()

document_tags = database.get_document_tags(document.id)
document_projects = database.get_document_projects(document.id)
summary_columns = st.columns([2, 1, 1, 1])
summary_columns[0].markdown(f"### {document.title}")
summary_columns[0].caption(f"原文件：{document.filename}")
summary_columns[1].metric("总页数", document.page_count)
summary_columns[2].metric("导入已处理页", document.processed_page_count)
summary_columns[3].metric("待复核", document.review_page_count)
st.caption(
    f"导入时间：{(document.imported_at or document.created_at).astimezone():%Y-%m-%d %H:%M}　|　"
    f"状态：{document.status_label}　|　SHA-256：{document.sha256[:12]}…"
)
if not document.source_path.is_file():
    st.warning(f"原始 PDF 文件缺失：{document.source_path}。页面记录仍保留，未执行自动修复。")

next_document_review_page = next(iter(database.list_review_pages(document.id)), None)
if st.button(
    "继续处理下一待复核页",
    type="primary",
    disabled=next_document_review_page is None,
    use_container_width=True,
):
    st.query_params.clear()
    st.query_params["page_id"] = str(next_document_review_page.id)
    st.switch_page("pages/5_待整理页面.py")
if next_document_review_page is None:
    st.caption("这份文档当前没有待处理、草稿待复核或处理失败的页面。")

with st.expander("文档标签与所属项目"):
    association_columns = st.columns(2)
    selected_document_tags = association_columns[0].multiselect(
        "文档标签",
        options=[tag.id for tag in all_tags],
        default=[tag.id for tag in document_tags],
        format_func=lambda value: next(tag.name for tag in all_tags if tag.id == value),
        key=f"document_tags_{document.id}",
    )
    selected_document_projects = association_columns[1].multiselect(
        "所属项目",
        options=[project.id for project in all_projects],
        default=[project.id for project in document_projects],
        format_func=lambda value: next(
            project.name for project in all_projects if project.id == value
        ),
        key=f"document_projects_{document.id}",
    )
    if st.button("保存文档分类", key=f"save_document_associations_{document.id}"):
        try:
            database.set_document_tags(document.id, selected_document_tags)
            database.set_document_projects(document.id, selected_document_projects)
        except Exception as exc:
            LOGGER.exception("保存文档分类失败：document_id=%s", document.id)
            st.error(f"保存文档分类失败：{exc}")
        else:
            st.success("文档分类已保存。")

with st.expander("文档管理"):
    st.caption("文档删除及生命周期管理已迁移至「文档管理」页面。")
    if st.button("前往「文档管理」", key=f"goto_document_management_{document.id}"):
        st.switch_page("pages/11_文档管理.py")

document_pages = sorted(
    database.list_pages(document.id),
    key=lambda item: (item.page_number, item.id),
)
if not document_pages:
    st.warning("该文档还没有页面记录，可能在导入时发生了错误。")
    st.stop()

try:
    all_basket_items = basket_service.list_items()
except Exception as exc:
    LOGGER.exception("读取证据篮状态失败")
    st.warning(f"暂时无法读取相邻页的证据状态：{exc}")
    all_basket_items = []
basket_page_ids = {item.page_id for item in all_basket_items}
search_hit_page_ids = {result.page_id for result in search_results}

page_by_number = {page.page_number: page for page in document_pages}
query_page = st.query_params.get("page")
if document_changed:
    query_page = None
if query_page:
    try:
        initial_page = int(query_page)
    except ValueError:
        st.error(f"无法打开指定页面：页码“{query_page}”无效。")
        st.stop()
    if initial_page not in page_by_number:
        st.error(
            f"无法打开指定页面：文档“{document.title}”中不存在第 {initial_page} 页。"
        )
        st.stop()
else:
    initial_page = document_pages[0].page_number

page_numbers = list(page_by_number)
requested_page_index = page_numbers.index(initial_page)
navigation = st.columns([1, 1, 3, 1, 1])
page_number = navigation[2].selectbox(
    "页码",
    options=page_numbers,
    index=requested_page_index,
    format_func=lambda value: f"第 {value} 页",
    label_visibility="collapsed",
)
st.query_params["page"] = str(page_number)
page = page_by_number[page_number]
current_page_index = page_numbers.index(page.page_number)
previous_page = (
    page_by_number[page_numbers[current_page_index - 1]]
    if current_page_index > 0
    else None
)
next_page = (
    page_by_number[page_numbers[current_page_index + 1]]
    if current_page_index + 1 < len(page_numbers)
    else None
)


def _open_adjacent_page(adjacent_page: Page) -> None:
    """Open an ordinary neighbour and update focus if it is also a search hit."""

    matched_result = next(
        (result for result in search_results if result.page_id == adjacent_page.id),
        None,
    )
    if matched_result is not None and search_return_state is not None:
        result_index = next(
            index
            for index, result in enumerate(search_results, start=1)
            if result.page_id == matched_result.page_id
        )
        _open_search_result(
            matched_result,
            result_index=result_index,
            return_state=search_return_state,
        )
    st.query_params["page"] = str(adjacent_page.page_number)
    st.rerun()


if navigation[0].button(
    "← 普通上一页", disabled=current_page_index <= 0, use_container_width=True
) and previous_page is not None:
    _open_adjacent_page(previous_page)
if navigation[1].button(
    "普通下一页 →",
    disabled=current_page_index >= len(page_numbers) - 1,
    use_container_width=True,
) and next_page is not None:
    _open_adjacent_page(next_page)
try:
    page = database.mark_page_viewed(page.id)
except Exception as exc:
    LOGGER.warning("记录页面查看时间失败：page_id=%s", page.id, exc_info=True)
    st.warning(f"页面已打开，但无法记录最近查看时间：{exc}")
navigation[3].metric("状态", page.status.label)
navigation[4].metric("笔记", "有" if page.has_note else "无")
st.caption(
    f"当前文档记录位置：第 {current_page_index + 1} / {len(page_numbers)} 页"
    f"（PDF 页码 {page.page_number}）。"
)


def _adjacent_feedback(label: str, adjacent_page: object | None) -> str:
    if adjacent_page is None:
        return f"{label}：无"
    hit_label = "命中当前搜索" if adjacent_page.id in search_hit_page_ids else "未命中当前搜索"
    basket_label = "已加入证据篮" if adjacent_page.id in basket_page_ids else "未加入证据篮"
    return f"{label}：第 {adjacent_page.page_number} 页 · {hit_label} · {basket_label}"


st.caption(
    _adjacent_feedback("普通上一页", previous_page)
    + "　|　"
    + _adjacent_feedback("普通下一页", next_page)
)

if from_search and search_return_state is not None:
    st.markdown("### 搜索命中连续导航")
    result_positions = {
        result.page_id: index for index, result in enumerate(search_results, start=1)
    }
    global_position = locate_result(search_results, page.id)
    if global_position is None:
        st.info(
            "当前页面不在重建后的搜索结果范围内。搜索条件可能已变化，"
            "或该页位于当前加载上限之外；命中导航已安全停用。"
        )
    else:
        if search_total > global_position.total:
            st.caption(
                f"搜索“{search_query}”完整匹配 {search_total} 页；"
                f"当前为已加载导航范围第 {global_position.index} / "
                f"{global_position.total} 个结果。"
            )
        else:
            st.caption(
                f"搜索“{search_query}”共命中 {search_total} 页；"
                f"当前为第 {global_position.index} / {global_position.total} 个结果。"
            )
    global_actions = st.columns([1.2, 1.2, 1.2, 2.4])
    previous_global = global_position.previous if global_position else None
    next_global = global_position.next if global_position else None
    if global_actions[0].button(
        "全文结果上一项",
        disabled=previous_global is None,
        key="reader_global_previous_hit",
        use_container_width=True,
    ) and previous_global is not None:
        _open_search_result(
            previous_global,
            result_index=result_positions[previous_global.page_id],
            return_state=search_return_state,
        )
    if global_actions[1].button(
        "全文结果下一项",
        disabled=next_global is None,
        key="reader_global_next_hit",
        use_container_width=True,
    ) and next_global is not None:
        _open_search_result(
            next_global,
            result_index=result_positions[next_global.page_id],
            return_state=search_return_state,
        )
    if global_actions[2].button(
        "返回检索结果",
        key="reader_return_search_near_navigation",
        use_container_width=True,
    ):
        return_params = search_state_query_params(search_return_state)
        st.session_state["pending_search_query_params"] = return_params
        st.query_params.from_dict(return_params)
        st.switch_page("pages/4_检索资料.py")
        st.stop()

    current_document_hits = document_hit_results(search_results, document.id)
    current_document_total = search_document_counts.get(
        document.id, len(current_document_hits)
    )
    current_document_index = next(
        (
            index
            for index, result in enumerate(current_document_hits)
            if result.page_id == page.id
        ),
        None,
    )
    st.markdown("#### 当前文档命中页（按页码顺序）")
    st.caption(
        f"本文件完整命中 {current_document_total} 页；"
        f"当前导航范围内有 {len(current_document_hits)} 页。"
    )
    document_actions = st.columns([1.4, 1.4, 3.2])
    previous_document_hit = (
        current_document_hits[current_document_index - 1]
        if current_document_index is not None and current_document_index > 0
        else None
    )
    next_document_hit = (
        current_document_hits[current_document_index + 1]
        if current_document_index is not None
        and current_document_index + 1 < len(current_document_hits)
        else None
    )
    if document_actions[0].button(
        "本文件上一命中页",
        disabled=previous_document_hit is None,
        key="reader_document_previous_hit",
        use_container_width=True,
    ) and previous_document_hit is not None:
        _open_search_result(
            previous_document_hit,
            result_index=result_positions[previous_document_hit.page_id],
            return_state=search_return_state,
        )
    if document_actions[1].button(
        "本文件下一命中页",
        disabled=next_document_hit is None,
        key="reader_document_next_hit",
        use_container_width=True,
    ) and next_document_hit is not None:
        _open_search_result(
            next_document_hit,
            result_index=result_positions[next_document_hit.page_id],
            return_state=search_return_state,
        )
    if current_document_index is None and current_document_hits:
        document_actions[2].info("当前页不是本文件命中页，可从下方列表跳转。")
    elif not current_document_hits:
        document_actions[2].info("当前搜索条件下，本文件没有已加载的命中页。")

    if current_document_hits:
        with st.expander("本文件命中页列表", expanded=True):
            hit_batch_size = 10
            hit_batch_count = (
                len(current_document_hits) + hit_batch_size - 1
            ) // hit_batch_size
            hit_batch = 1
            if hit_batch_count > 1:
                hit_batch = st.selectbox(
                    "命中页分组",
                    options=list(range(1, hit_batch_count + 1)),
                    format_func=lambda value: f"第 {value} / {hit_batch_count} 组",
                    key=f"reader_hit_batch_{document.id}",
                )
            hit_start = (int(hit_batch) - 1) * hit_batch_size
            with st.container(height=360, border=True):
                for hit in current_document_hits[
                    hit_start : hit_start + hit_batch_size
                ]:
                    current_label = "（当前页）" if hit.page_id == page.id else ""
                    summary = " ".join((hit.snippet or "无可用文本摘要").split())
                    button_label = (
                        f"第 {hit.page_number} 页{current_label}｜{summary[:86]}"
                    )
                    if st.button(
                        button_label,
                        key=f"reader_open_hit_{hit.page_id}",
                        disabled=hit.page_id == page.id,
                        use_container_width=True,
                    ):
                        _open_search_result(
                            hit,
                            result_index=result_positions[hit.page_id],
                            return_state=search_return_state,
                        )

if from_search and search_query:
    search_source = (
        page.markdown_content.strip()
        or page.ocr_text.strip()
        or page.extracted_text.strip()
    )
    if search_source:
        search_terms = search_service.query_terms(search_query)
        page_snippet = search_service.build_snippet(search_source, search_terms)
        with st.expander("当前页关键词提示", expanded=True):
            st.markdown(
                search_service.highlighted_snippet(
                    SearchResult(
                        page_id=page.id,
                        document_id=document.id,
                        document_title=document.title,
                        filename=document.filename,
                        page_number=page.page_number,
                        image_path=page.image_path,
                        content=search_source,
                        snippet=page_snippet,
                        rank=0.0,
                        status=page.status,
                    ),
                    search_query,
                ),
                unsafe_allow_html=True,
            )

current_basket_items = [item for item in all_basket_items if item.page_id == page.id]

if current_basket_items:
    with st.expander("管理当前页已有证据", expanded=False):
        for stored_item in current_basket_items:
            stored_note_key = f"reader_evidence_note_{stored_item.id}"
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
                key=f"reader_save_evidence_note_{stored_item.id}",
                use_container_width=True,
            ):
                try:
                    basket_service.update_note(
                        stored_item.id,
                        str(st.session_state[stored_note_key]),
                    )
                except EvidenceBasketError as exc:
                    st.error(f"保存证据备注失败：{exc}")
                else:
                    st.session_state["basket_flash"] = (
                        "证据备注已保存，搜索返回状态保持不变。"
                    )
                    st.rerun()
            if remove_action.button(
                "移除这条证据",
                key=f"reader_remove_evidence_{stored_item.id}",
                help="只删除证据篮条目，不删除原始资料或页面笔记",
                use_container_width=True,
            ):
                try:
                    basket_service.remove_item(stored_item.id)
                except EvidenceBasketError as exc:
                    st.error(f"移除证据失败：{exc}")
                else:
                    st.session_state["basket_flash"] = (
                        "已移除证据，搜索返回状态保持不变。"
                    )
                    st.rerun()

evidence_entry, basket_entry = st.columns([4, 1])
evidence_entry.caption(
    f"当前页已加入 {len(current_basket_items)} 条选区证据；证据篮在服务重启后仍会保留。"
)
if basket_entry.button(
    f"查看证据篮（{len(all_basket_items)}）",
    key="open_basket_from_reader",
    use_container_width=True,
):
    st.switch_page("pages/7_证据篮.py")

with st.expander("将当前页选区加入证据篮", expanded=False):
    original_source = page.extracted_text.strip() or page.ocr_text.strip()
    suggestion_source = original_source or page.markdown_content.strip()
    suggestion_terms = (
        SearchService(database).query_terms(search_query)
        if from_search and search_query
        else ()
    )
    default_selection = SearchService(database).build_snippet(
        suggestion_source,
        suggestion_terms,
        max_chars=600,
    )
    default_selection = default_selection.removeprefix("…").removesuffix("…").strip()
    selection_key = f"reader_basket_selection_{page.id}"
    note_key = f"reader_basket_note_{page.id}"
    if selection_key not in st.session_state:
        st.session_state[selection_key] = default_selection
    if note_key not in st.session_state:
        st.session_state[note_key] = ""
    st.text_area(
        "证据文本",
        key=selection_key,
        height=150,
        placeholder="从原始提取文本或页面 Markdown 中复制具体段落",
        help=(
            "系统会核对该文本能否在 PDF 文本层或 OCR 文本中找到；无法匹配时会明确"
            "标记为用户摘录，绝不会伪装成已验证原文。"
        ),
    )
    st.text_area("用户备注（可选）", key=note_key, height=90)
    if st.button(
        "加入证据篮",
        key=f"reader_add_basket_{page.id}",
        type="primary",
        use_container_width=True,
    ):
        try:
            basket_service.add_item(
                document_id=document.id,
                page_id=page.id,
                evidence_text=str(st.session_state[selection_key]),
                user_note=str(st.session_state[note_key]),
            )
        except DuplicateEvidenceError as exc:
            st.info(str(exc))
        except EvidenceBasketError as exc:
            st.error(f"加入证据篮失败：{exc}")
        except Exception as exc:
            LOGGER.exception("加入证据篮失败：page_id=%s", page.id)
            st.error(f"加入证据篮失败：{exc}")
        else:
            st.session_state["basket_flash"] = "当前页选区已持久化加入证据篮。"
            st.rerun()

if st.button(
    "将当前整页加入证据篮",
    key=f"reader_add_page_basket_{page.id}",
    help="整页证据引用当前页图像与文本，不复制选区；同一页面只能加入一次。",
    use_container_width=True,
):
    try:
        basket_service.add_page_item(
            basket_service.default_basket().id,
            document.id,
            page.id,
        )
    except DuplicateEvidenceError as exc:
        st.info(str(exc))
    except EvidenceBasketError as exc:
        st.error(f"加入证据篮失败：{exc}")
    except Exception as exc:
        LOGGER.exception("加入整页证据失败：page_id=%s", page.id)
        st.error(f"加入证据篮失败：{exc}")
    else:
        st.session_state["basket_flash"] = "当前页整页证据已持久化加入证据篮。"
        st.rerun()

# FIX-A：图片区域框选 workbench 的全宽渲染区（active 时才写入内容）。
region_selector_area = st.container()
image_column, editor_column = st.columns([1.08, 1], gap="large")
with image_column:
    st.subheader("原始页面")
    zoom_columns = st.columns([2, 1])
    image_width = zoom_columns[0].slider("页面缩放", 500, 1400, 850, 50)
    fit_width = zoom_columns[1].checkbox("适应宽度", value=True)
    with st.container(height=760, border=True):
        if page.image_path.exists():
            try:
                st.image(
                    str(page.image_path),
                    caption=f"{document.title} · 第 {page.page_number} 页",
                    width="stretch" if fit_width else image_width,
                )
            except (OSError, ValueError) as exc:
                # 单张损坏图片只降级本栏显示，绝不让整页崩溃。
                LOGGER.warning(
                    "页面图片无法显示：%s（%s）", page.image_path, exc
                )
                st.error(
                    f"页面图片无法显示：{page.image_path}。"
                    "文件可能已损坏，请在“系统维护”检查引用和备份。"
                )
        else:
            st.error(f"页面图片缺失：{page.image_path}")
    with st.expander("查看原始提取文本"):
        st.text_area(
            "原始提取文本",
            value=page.extracted_text or "（没有提取到文本）",
            height=280,
            disabled=True,
            label_visibility="collapsed",
        )
        if page.ocr_text.strip():
            st.text_area("OCR 文本", value=page.ocr_text, height=220, disabled=True)
        if page.processing_error:
            st.error(f"失败原因：{page.processing_error}")

with editor_column:
    st.subheader("页面整理稿（Markdown）")
    if page.markdown_content.strip() and (
        page.markdown_path is None or not page.markdown_path.exists()
    ):
        st.warning(
            "页面笔记仍保存在数据库中，但对应 Markdown 文件缺失或未记录。"
            "系统不会自动覆盖资料；请先在“系统维护”检查引用和备份。"
            "确认内容无误后再次保存笔记，可显式重建本地 Markdown 文件。"
        )
    editor_tab, preview_tab, notes_tab = st.tabs(
        ["整理稿-编辑", "整理稿-预览", "结构化笔记"]
    )
    with editor_tab:
        uploaded_markdown = st.file_uploader(
            "可选：导入 Markdown 文件",
            type=["md", "markdown", "txt"],
            key=f"markdown_upload_{page.id}",
        )
        initial_markdown = page.markdown_content
        upload_token = "current"
        if uploaded_markdown is not None:
            try:
                uploaded_bytes = uploaded_markdown.getvalue()
                initial_markdown = decode_markdown(uploaded_bytes)
                upload_token = f"{Path(uploaded_markdown.name).name}_{len(uploaded_bytes)}"
            except ValueError as exc:
                st.error(str(exc))
        markdown_content = st.text_area(
            "Markdown 内容",
            value=initial_markdown,
            height=510,
            key=f"markdown_editor_{page.id}_{upload_token}",
            placeholder=(
                "支持标题、列表、**粗体**、代码块、引用和表格。\n\n"
                "# 本页主题\n\n- 要点"
            ),
        )
        if markdown_content != page.markdown_content:
            st.warning("● 未保存")
        else:
            st.success("● 已保存")
        if st.button("保存笔记", type="primary", use_container_width=True):
            try:
                with st.spinner("正在保存……"):
                    document_service.save_page_markdown(
                        document_id=document.id,
                        page_number=page.page_number,
                        markdown_content=markdown_content,
                    )
            except Exception as exc:
                LOGGER.exception("保存页面笔记失败：page_id=%s", page.id)
                st.error(f"保存失败：{exc}")
            else:
                st.success("已保存")
                st.rerun()
    with preview_tab:
        if markdown_content.strip():
            st.markdown(markdown_content)
        else:
            st.info("输入 Markdown 后可在这里预览。")
    with notes_tab:
        render_structured_notes_tab(
            note_service,
            document_id=document.id,
            page_id=page.id,
            region_selector_area=region_selector_area,
            basket_service=basket_service,
        )

    st.subheader("页面分类")
    page_tags = database.get_page_tags(page.id)
    page_projects = database.get_page_projects(page.id)
    selected_page_tags = st.multiselect(
        "页面标签",
        options=[tag.id for tag in all_tags],
        default=[tag.id for tag in page_tags],
        format_func=lambda value: next(tag.name for tag in all_tags if tag.id == value),
        key=f"page_tags_{page.id}",
    )
    selected_page_projects = st.multiselect(
        "页面所属项目",
        options=[project.id for project in all_projects],
        default=[project.id for project in page_projects],
        format_func=lambda value: next(
            project.name for project in all_projects if project.id == value
        ),
        key=f"page_projects_{page.id}",
    )
    if st.button("保存页面分类", key=f"save_page_associations_{page.id}"):
        try:
            database.set_page_tags(page.id, selected_page_tags)
            database.set_page_projects(page.id, selected_page_projects)
        except Exception as exc:
            st.error(f"保存页面分类失败：{exc}")
        else:
            st.success("页面分类已保存。")

with st.expander("页面列表与缩略图"):
    batch_size = 12
    batch_count = (len(document_pages) + batch_size - 1) // batch_size
    batch = st.number_input("缩略图分组", min_value=1, max_value=batch_count, value=1)
    start = (int(batch) - 1) * batch_size
    visible_pages = document_pages[start : start + batch_size]
    for row_start in range(0, len(visible_pages), 4):
        columns = st.columns(4)
        for column, thumbnail_page in zip(
            columns, visible_pages[row_start : row_start + 4], strict=False
        ):
            with column:
                if thumbnail_page.image_path.exists():
                    try:
                        st.image(str(thumbnail_page.image_path), width="stretch")
                    except (OSError, ValueError):
                        st.caption("缩略图无法显示（文件可能已损坏）")
                st.caption(
                    f"第 {thumbnail_page.page_number} 页 · {thumbnail_page.status.label} · "
                    f"{'有笔记' if thumbnail_page.has_note else '无笔记'}"
                )
                tag_names = [tag.name for tag in database.get_page_tags(thumbnail_page.id)]
                if tag_names:
                    st.caption("标签：" + "、".join(tag_names))
                if st.button(
                    "打开",
                    key=f"open_thumbnail_{thumbnail_page.id}",
                    use_container_width=True,
                ):
                    st.query_params["page"] = str(thumbnail_page.page_number)
                    st.rerun()
