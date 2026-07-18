"""Tests for exact, fail-safe search-to-reader navigation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database
from src.models import PageStatus, SearchResult
from src.search_navigation import (
    SearchNavigationError,
    reader_query_params,
    validate_search_target,
)


def _search_result(
    *, page_id: int, document_id: int, page_number: int, image_path: Path
) -> SearchResult:
    return SearchResult(
        page_id=page_id,
        document_id=document_id,
        document_title="导航测试",
        filename="navigation.pdf",
        page_number=page_number,
        image_path=image_path,
        content="目标页面",
        snippet="目标页面",
        rank=0.0,
        status=PageStatus.PENDING,
    )


def test_valid_target_opens_exact_document_and_page_and_passes_query(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    source_path = tmp_path / "raw" / "navigation.pdf"
    image_path = tmp_path / "pages" / "page_0002.png"
    source_path.parent.mkdir(parents=True)
    image_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"pdf")
    image_path.write_bytes(b"png")
    document = database.create_document(
        title="导航测试",
        filename="navigation.pdf",
        source_path=source_path,
        sha256="b" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=image_path,
        extracted_text="目标页面",
    )
    result = _search_result(
        page_id=page.id,
        document_id=document.id,
        page_number=2,
        image_path=image_path,
    )

    target = validate_search_target(database, result)
    params = reader_query_params(result, "目标 关键词")

    assert target.document.id == document.id
    assert target.page.id == page.id and target.page.page_number == 2
    assert target.warnings == ()
    assert params == {
        "document": str(document.id),
        "page": "2",
        "from_search": "1",
        "search_query": "目标 关键词",
    }


def test_missing_document_page_and_mismatched_record_fail_safely(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="导航测试",
        filename="navigation.pdf",
        source_path=tmp_path / "missing.pdf",
        sha256="c" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "missing.png",
    )

    with pytest.raises(SearchNavigationError, match="文档已不存在"):
        validate_search_target(
            database,
            _search_result(
                page_id=page.id,
                document_id=999,
                page_number=1,
                image_path=page.image_path,
            ),
        )
    with pytest.raises(SearchNavigationError, match="页面已不存在"):
        validate_search_target(
            database,
            _search_result(
                page_id=999,
                document_id=document.id,
                page_number=1,
                image_path=page.image_path,
            ),
        )
    with pytest.raises(SearchNavigationError, match="不一致"):
        validate_search_target(
            database,
            _search_result(
                page_id=page.id,
                document_id=document.id,
                page_number=2,
                image_path=page.image_path,
            ),
        )


def test_missing_files_are_reported_without_changing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    database = Database(database_path)
    document = database.create_document(
        title="缺失文件",
        filename="missing.pdf",
        source_path=tmp_path / "missing source.pdf",
        sha256="d" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=3,
        image_path=tmp_path / "缺失 页面.png",
    )
    result = _search_result(
        page_id=page.id,
        document_id=document.id,
        page_number=3,
        image_path=page.image_path,
    )

    target = validate_search_target(database, result)
    reopened = Database(database_path)

    assert len(target.warnings) == 2
    assert "原始 PDF 文件缺失" in target.warnings[0]
    assert "页面图像缺失" in target.warnings[1]
    assert reopened.get_page(page.id) == page


def test_query_hint_is_bounded_but_coordinates_are_not_changed(tmp_path: Path) -> None:
    result = _search_result(
        page_id=8,
        document_id=9,
        page_number=10,
        image_path=tmp_path / "page.png",
    )

    params = reader_query_params(result, "词" * 1000)

    assert params["document"] == "9" and params["page"] == "10"
    assert len(params["search_query"]) == 500
