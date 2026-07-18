"""Filter documents and use the two-column page reader and Markdown editor."""

from __future__ import annotations

import logging
from pathlib import Path

import streamlit as st

from src.models import ImportStatus, SearchResult
from src.runtime import application_database, application_document_service
from src.search_service import SearchService

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="浏览资料｜工程知识库 v0.0.4", page_icon="📖", layout="wide")
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
    all_tags = database.list_tags()
    all_projects = database.list_projects()
except Exception as exc:
    LOGGER.exception("读取资料失败")
    st.error(f"读取资料失败：{exc}")
    st.stop()

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
    st.info("没有符合筛选条件的文档。")
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

if from_search:
    search_query = (
        st.query_params.get("search_query")
        or st.session_state.get("knowledge_query", "")
    )
    search_banner, return_action = st.columns([5, 1])
    search_banner.info(f"当前页面来自检索：{search_query or '（未记录关键词）'}")
    if return_action.button("返回检索结果", type="primary", use_container_width=True):
        st.query_params.clear()
        st.switch_page("pages/3_检索资料.py")
        st.stop()

document_tags = database.get_document_tags(document.id)
document_projects = database.get_document_projects(document.id)
summary_columns = st.columns([2, 1, 1, 1])
summary_columns[0].markdown(f"### {document.title}")
summary_columns[0].caption(f"原文件：{document.filename}")
summary_columns[1].metric("总页数", document.page_count)
summary_columns[2].metric("已处理", document.processed_page_count)
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
    st.switch_page("pages/4_待整理页面.py")
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

document_pages = database.list_pages(document.id)
if not document_pages:
    st.warning("该文档还没有页面记录，可能在导入时发生了错误。")
    st.stop()

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

navigation = st.columns([1, 1, 3, 1, 1])
page_numbers = list(page_by_number)
initial_page_index = page_numbers.index(initial_page)
if navigation[0].button(
    "← 上一页", disabled=initial_page_index <= 0, use_container_width=True
):
    st.query_params["page"] = str(page_numbers[initial_page_index - 1])
    st.rerun()
if navigation[1].button(
    "下一页 →",
    disabled=initial_page_index >= len(page_numbers) - 1,
    use_container_width=True,
):
    st.query_params["page"] = str(page_numbers[initial_page_index + 1])
    st.rerun()
page_number = navigation[2].selectbox(
    "页码",
    options=page_numbers,
    index=initial_page_index,
    format_func=lambda value: f"第 {value} 页",
    label_visibility="collapsed",
)
st.query_params["page"] = str(page_number)
page = page_by_number[page_number]
try:
    page = database.mark_page_viewed(page.id)
except Exception as exc:
    LOGGER.warning("记录页面查看时间失败：page_id=%s", page.id, exc_info=True)
    st.warning(f"页面已打开，但无法记录最近查看时间：{exc}")
navigation[3].metric("状态", page.status.label)
navigation[4].metric("笔记", "有" if page.has_note else "无")

if from_search and search_query:
    search_service = SearchService(database)
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

image_column, editor_column = st.columns([1.08, 1], gap="large")
with image_column:
    st.subheader("原始页面")
    zoom_columns = st.columns([2, 1])
    image_width = zoom_columns[0].slider("页面缩放", 500, 1400, 850, 50)
    fit_width = zoom_columns[1].checkbox("适应宽度", value=True)
    with st.container(height=760, border=True):
        if page.image_path.exists():
            st.image(
                str(page.image_path),
                caption=f"{document.title} · 第 {page.page_number} 页",
                width="stretch" if fit_width else image_width,
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
    st.subheader("页面 Markdown 笔记")
    editor_tab, preview_tab = st.tabs(["编辑", "预览"])
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
        save_column, clear_column = st.columns(2)
        if save_column.button("保存笔记", type="primary", use_container_width=True):
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
        confirm_clear = clear_column.checkbox("确认清空", key=f"confirm_clear_{page.id}")
        if clear_column.button(
            "清空笔记",
            disabled=not confirm_clear,
            use_container_width=True,
            key=f"clear_note_{page.id}",
        ):
            try:
                document_service.clear_page_markdown(document.id, page.page_number)
            except Exception as exc:
                st.error(f"清空失败：{exc}")
            else:
                st.success("本页笔记已清空。")
                st.rerun()
    with preview_tab:
        if markdown_content.strip():
            st.markdown(markdown_content)
        else:
            st.info("输入 Markdown 后可在这里预览。")

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
                    st.image(str(thumbnail_page.image_path), width="stretch")
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

with st.expander("危险操作：删除文档"):
    st.warning("删除会清理该文档的数据库记录、独占原 PDF、页面图片和笔记文件。")
    confirmation = st.text_input(
        f"请输入文档标题“{document.title}”进行二次确认",
        key=f"delete_confirmation_{document.id}",
    )
    if st.button(
        "永久删除此文档",
        disabled=confirmation != document.title,
        key=f"delete_document_{document.id}",
    ):
        try:
            document_service.delete_document(document.id, confirmed=True)
        except Exception as exc:
            LOGGER.exception("删除文档失败：document_id=%s", document.id)
            st.error(f"删除失败：{exc}")
        else:
            st.query_params.clear()
            st.success("文档及其独占文件已删除。")
            st.rerun()
