"""v0.0.4 search query, filtering, source detection, and safety tests."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from src.database import Database
from src.models import PageStatus, SearchField, SearchFilters, SearchSort
from src.search_service import SearchService
from src.text_utils import extract_search_terms, highlight_html


def _document(database: Database, suffix: str, title: str, filename: str):
    return database.create_document(
        title=title,
        filename=filename,
        source_path=Path("data/raw") / filename,
        sha256=suffix * 64,
    )


def test_english_chinese_continuous_and_multiple_keyword_search(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, "1", "液压系统故障手册", "pump-manual.pdf")
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page.png",
        extracted_text="前言。液压系统故障诊断时，pump pressure 需要保持稳定。",
    )
    service = SearchService(database)

    assert service.search("pump")[0].page_id == page.id
    assert service.search("液压")[0].page_id == page.id
    assert service.search("液压系统故障")[0].page_id == page.id
    assert service.search("pump 不存在词")[0].page_id == page.id
    assert service.search("完全无关") == []


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   \t\n",
        "() + - / \\",
        "AND OR NOT NEAR",
    ],
)
def test_empty_whitespace_punctuation_and_fts_operators_are_safe(
    tmp_path: Path, query: str
) -> None:
    database = Database(tmp_path / "knowledge.db")
    service = SearchService(database)

    assert service.search(query) == []


def test_quotes_parentheses_plus_minus_slash_and_long_query_are_safe(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, "2", "电源 (DC/DC) 手册", "power+loop.pdf")
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page.png",
        extracted_text='电源环路出现振荡时检查 gain/phase，并记录 "warning"。',
    )
    service = SearchService(database)

    results = service.search('+电源 - (gain/phase) "warning" AND')
    assert results and results[0].page_id == page.id
    assert service.search("x" * 10_000) == []
    assert len(service.normalize_query("x" * 10_000)) < 200


def test_same_keyword_reports_every_matching_field(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, "3", "泵站控制说明", "pump-control.pdf")
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page.png",
        extracted_text="泵站压力控制。",
        ocr_text="泵站铭牌。",
        markdown_content="用户记录：泵站已检查。",
        status=PageStatus.REVIEWED,
    )
    tag = database.create_tag("泵站")
    project = database.create_project("泵站改造")
    database.set_page_tags(page.id, [tag.id])
    database.set_page_projects(page.id, [project.id])

    result = SearchService(database).search("泵站")[0]

    assert {
        SearchField.MARKDOWN,
        SearchField.OCR_TEXT,
        SearchField.EXTRACTED_TEXT,
        SearchField.DOCUMENT_TITLE,
        SearchField.TAG,
        SearchField.PROJECT,
    } <= set(result.match_fields)
    assert result.content == page.markdown_content
    assert "用户 Markdown 笔记" in result.match_type


def test_context_excerpt_is_near_match_and_has_stable_fallback(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    service = SearchService(database, snippet_length=60)
    content = "开头说明。" * 50 + "目标关键词附近的工程上下文。" + "末尾说明。" * 50

    matched = service.build_snippet(content, ["目标关键词"])
    fallback = service.build_snippet(content, ["不存在"])

    assert "目标关键词附近" in matched
    assert matched.startswith("…") and matched.endswith("…")
    assert "开头说明" in fallback
    assert fallback.endswith("…")


def test_highlight_preserves_text_and_escapes_html_script_content() -> None:
    source = '<script>alert("x")</script> 液压+泵 & <b>pressure</b>'

    rendered = highlight_html(source, ["液压", "+", "pressure"])
    reconstructed = html.unescape(re.sub(r"</?mark>", "", rendered))

    assert reconstructed == source
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "<b>" not in rendered and "&lt;b&gt;" in rendered
    assert rendered.count("<mark>") == 3


def test_filter_combinations_tag_and_semantics_and_missing_metadata(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    first_document = _document(database, "4", "A 文档", "a.pdf")
    second_document = _document(database, "5", "B 文档", "b.pdf")
    third_document = _document(database, "6", "C 文档", "c.pdf")
    first = database.create_page(
        document_id=first_document.id,
        page_number=1,
        image_path=tmp_path / "first.png",
        extracted_text="公共词 first",
        markdown_content="公共词 note",
        status=PageStatus.REVIEWED,
    )
    second = database.create_page(
        document_id=second_document.id,
        page_number=1,
        image_path=tmp_path / "second.png",
        ocr_text="公共词 second",
        status=PageStatus.PENDING,
    )
    third = database.create_page(
        document_id=third_document.id,
        page_number=1,
        image_path=tmp_path / "third.png",
        extracted_text="公共词 third",
        status=PageStatus.DRAFT,
    )
    tag_a = database.create_tag("标签 A")
    tag_b = database.create_tag("标签 B")
    database.set_document_tags(first_document.id, [tag_a.id])
    database.set_page_tags(first.id, [tag_b.id])
    database.set_page_tags(second.id, [tag_a.id])
    project_a = database.create_project("项目 A")
    project_b = database.create_project("项目 B")
    database.set_page_projects(first.id, [project_a.id])
    database.set_page_projects(second.id, [project_b.id])
    service = SearchService(database)

    def ids(filters: SearchFilters | None = None) -> list[int]:
        return [
            result.page_id
            for result in service.search("公共词", filters=filters or SearchFilters())
        ]

    assert ids(SearchFilters(document_ids=(first_document.id,))) == [first.id]
    assert ids(SearchFilters(project_ids=(project_b.id,))) == [second.id]
    assert set(ids(SearchFilters(tag_ids=(tag_a.id,)))) == {first.id, second.id}
    assert ids(SearchFilters(tag_ids=(tag_a.id, tag_b.id))) == [first.id]
    assert ids(SearchFilters(statuses=(PageStatus.PENDING,))) == [second.id]
    assert ids(SearchFilters(match_fields=(SearchField.OCR_TEXT,))) == [second.id]
    assert ids(
        SearchFilters(
            document_ids=(first_document.id,),
            project_ids=(project_a.id,),
            tag_ids=(tag_a.id, tag_b.id),
            statuses=(PageStatus.REVIEWED,),
            match_fields=(SearchField.EXTRACTED_TEXT,),
        )
    ) == [first.id]
    assert set(ids()) == {first.id, second.id, third.id}
    assert ids(
        SearchFilters(project_ids=(project_a.id,), statuses=(PageStatus.PENDING,))
    ) == []
    missing_metadata = next(
        result for result in service.search("third") if result.page_id == third.id
    )
    assert missing_metadata.tags == () and missing_metadata.projects == ()


def test_filter_values_are_parameterized_and_sort_is_whitelisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = _document(database, "7", "参数化", "safe.pdf")
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page.png",
        extracted_text="安全查询",
    )
    service = SearchService(database)

    with pytest.raises(ValueError):
        service.search(
            "安全",
            filters=SearchFilters(document_ids=("1) OR 1=1",)),  # type: ignore[arg-type]
        )
    results = service.search("安全", sort_by="rank; DROP TABLE pages")

    assert len(results) == 1
    assert len(database.list_pages(document.id)) == 1
    assert service.search("%_' --") == []


def test_document_page_sort_is_stable(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    second_document = _document(database, "8", "B 文档", "b.pdf")
    first_document = _document(database, "9", "A 文档", "a.pdf")
    second_page = database.create_page(
        document_id=second_document.id,
        page_number=2,
        image_path=tmp_path / "b.png",
        extracted_text="排序词",
    )
    first_page = database.create_page(
        document_id=first_document.id,
        page_number=1,
        image_path=tmp_path / "a.png",
        extracted_text="排序词",
    )

    results = SearchService(database).search(
        "排序词", sort_by=SearchSort.DOCUMENT_PAGE
    )

    assert [result.page_id for result in results] == [first_page.id, second_page.id]


def test_extract_search_terms_retains_chinese_continuous_text_and_deduplicates() -> None:
    terms = extract_search_terms(' 液压系统故障，液压系统故障 + "pump" ')

    assert terms[0] == "液压系统故障"
    assert terms.count("pump") == 1
    assert len(terms) == len(set(terms))
