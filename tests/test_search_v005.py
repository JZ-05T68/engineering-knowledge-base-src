"""v0.0.5 facet-count and explainable relevance tests."""

from __future__ import annotations

from pathlib import Path

from src.database import Database
from src.models import PageStatus, SearchFilters, SearchSort
from src.search_service import SearchService


def _document(database: Database, suffix: str, title: str, filename: str):
    return database.create_document(
        title=title,
        filename=filename,
        source_path=Path("data/raw") / filename,
        sha256=suffix * 64,
    )


def test_facet_counts_without_query_and_with_full_current_conditions(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    first_document = _document(database, "1", "A 文档", "a.pdf")
    second_document = _document(database, "2", "B 文档", "b.pdf")
    third_document = _document(database, "3", "C 文档", "c.pdf")
    first = database.create_page(
        document_id=first_document.id,
        page_number=1,
        image_path=tmp_path / "1.png",
        extracted_text="公共词 alpha",
        status=PageStatus.REVIEWED,
    )
    second = database.create_page(
        document_id=first_document.id,
        page_number=2,
        image_path=tmp_path / "2.png",
        extracted_text="公共词 beta",
        status=PageStatus.PENDING,
    )
    third = database.create_page(
        document_id=second_document.id,
        page_number=1,
        image_path=tmp_path / "3.png",
        extracted_text="公共词 gamma",
        status=PageStatus.DRAFT,
    )
    database.create_page(
        document_id=third_document.id,
        page_number=1,
        image_path=tmp_path / "4.png",
        extracted_text="only delta",
        status=PageStatus.SKIPPED,
    )
    tag_a = database.create_tag("标签 A")
    tag_b = database.create_tag("标签 B")
    project_a = database.create_project("项目 A")
    project_b = database.create_project("项目 B")
    database.set_document_tags(first_document.id, [tag_a.id])
    database.set_page_tags(first.id, [tag_b.id])
    database.set_page_tags(third.id, [tag_a.id])
    database.set_document_projects(first_document.id, [project_a.id])
    database.set_page_projects(first.id, [project_b.id])
    database.set_document_projects(second_document.id, [project_b.id])
    service = SearchService(database)

    all_counts = service.facet_counts("")
    query_counts = service.facet_counts("公共词")

    assert all_counts.total == 4
    assert all_counts.statuses == {
        PageStatus.PENDING: 1,
        PageStatus.DRAFT: 1,
        PageStatus.REVIEWED: 1,
        PageStatus.SKIPPED: 1,
        PageStatus.FAILED: 0,
    }
    assert query_counts.total == 3
    assert query_counts.total == len(service.search("公共词"))
    assert query_counts.tags == {tag_a.id: 3, tag_b.id: 1}
    assert query_counts.projects == {project_a.id: 2, project_b.id: 2}

    tag_a_counts = service.facet_counts(
        "公共词", filters=SearchFilters(tag_ids=(tag_a.id,))
    )
    both_tag_counts = service.facet_counts(
        "公共词", filters=SearchFilters(tag_ids=(tag_a.id, tag_b.id))
    )
    both_project_counts = service.facet_counts(
        "公共词",
        filters=SearchFilters(project_ids=(project_a.id, project_b.id)),
    )

    # Facets include their own selected condition: every count describes a
    # subset of the same currently filtered result set.
    assert tag_a_counts.total == 3
    assert tag_a_counts.tags == {tag_a.id: 3, tag_b.id: 1}
    assert both_tag_counts.total == 1
    assert both_tag_counts.tags == {tag_a.id: 1, tag_b.id: 1}
    assert both_project_counts.total == 1
    assert both_project_counts.projects == {project_a.id: 1, project_b.id: 1}
    assert [result.page_id for result in service.search(
        "公共词", filters=SearchFilters(tag_ids=(tag_a.id, tag_b.id))
    )] == [first.id]
    assert second.id != third.id


def test_facet_counts_keep_multi_term_or_and_status_document_filters(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    first_document = _document(database, "4", "第一文档", "first.pdf")
    second_document = _document(database, "5", "第二文档", "second.pdf")
    first = database.create_page(
        document_id=first_document.id,
        page_number=1,
        image_path=tmp_path / "first.png",
        extracted_text="alpha only",
        status=PageStatus.REVIEWED,
    )
    second = database.create_page(
        document_id=second_document.id,
        page_number=1,
        image_path=tmp_path / "second.png",
        extracted_text="gamma only",
        status=PageStatus.PENDING,
    )
    service = SearchService(database)

    or_counts = service.facet_counts("alpha gamma")
    reviewed_counts = service.facet_counts(
        "alpha gamma",
        filters=SearchFilters(statuses=(PageStatus.REVIEWED,)),
    )
    document_counts = service.facet_counts(
        "alpha gamma",
        filters=SearchFilters(document_ids=(second_document.id,)),
    )

    assert or_counts.total == 2
    assert set(result.page_id for result in service.search("alpha gamma")) == {
        first.id,
        second.id,
    }
    assert reviewed_counts.total == 1
    assert reviewed_counts.statuses[PageStatus.REVIEWED] == 1
    assert reviewed_counts.statuses[PageStatus.PENDING] == 0
    assert document_counts.total == 1


def test_relevance_prefers_title_filename_multi_field_and_exact_phrase(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    body_document = _document(database, "6", "普通资料", "body.pdf")
    title_document = _document(database, "7", "Cavitation 说明", "title.pdf")
    multi_document = _document(
        database, "8", "Cavitation analysis", "cavitation-notes.pdf"
    )
    body = database.create_page(
        document_id=body_document.id,
        page_number=1,
        image_path=tmp_path / "body.png",
        extracted_text="pump cavitation should be checked",
    )
    title = database.create_page(
        document_id=title_document.id,
        page_number=1,
        image_path=tmp_path / "title.png",
        extracted_text="unrelated page text",
    )
    multi = database.create_page(
        document_id=multi_document.id,
        page_number=1,
        image_path=tmp_path / "multi.png",
        extracted_text="cavitation diagnosis",
    )
    service = SearchService(database)

    ranked = service.search("cavitation")

    assert [result.page_id for result in ranked] == [multi.id, title.id, body.id]
    assert ranked[0].rank < ranked[1].rank < ranked[2].rank

    continuous_document = _document(database, "9", "连续短语", "continuous.pdf")
    scattered_document = _document(database, "a", "分散短语", "scattered.pdf")
    continuous = database.create_page(
        document_id=continuous_document.id,
        page_number=1,
        image_path=tmp_path / "continuous.png",
        extracted_text="液压系统故障需要检查。",
    )
    scattered = database.create_page(
        document_id=scattered_document.id,
        page_number=1,
        image_path=tmp_path / "scattered.png",
        extracted_text="液压 系统 的多个独立 故障。",
    )

    phrase_ranked = service.search("液压系统故障")
    assert [result.page_id for result in phrase_ranked[:2]] == [
        continuous.id,
        scattered.id,
    ]


def test_legacy_document_page_sort_ignores_relevance_boosts(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    second_document = _document(database, "b", "B 文档", "b.pdf")
    first_document = _document(database, "c", "A keyword 文档", "a.pdf")
    second = database.create_page(
        document_id=second_document.id,
        page_number=1,
        image_path=tmp_path / "b.png",
        extracted_text="keyword",
    )
    first = database.create_page(
        document_id=first_document.id,
        page_number=1,
        image_path=tmp_path / "a.png",
        extracted_text="keyword",
    )

    results = SearchService(database).search(
        "keyword", sort_by=SearchSort.DOCUMENT_PAGE
    )

    assert [result.page_id for result in results] == [first.id, second.id]
