"""Phase 3C KnowledgeSearchService behavior tests.

Covers query normalization, FTS5 MATCH recall, bm25 ordering, per-type
grouping, default status filters, snippets, provenance anchors, offline
operation and invalid-expression safety. Page-scope search is untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.database import Database
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.knowledge_search_service import KnowledgeSearchService
from src.models import KnowledgeSearchResultType

TS = "2026-08-01T00:00:00+00:00"


@pytest.fixture()
def search(
    tmp_path: Path,
) -> tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService]:
    database = Database(tmp_path / "knowledge.db")
    return (
        database,
        KnowledgeSearchService(database),
        KnowledgeObjectService(database),
        KnowledgeMemoryService(database),
    )


def _create_object(service: KnowledgeObjectService, title: str, content: str) -> int:
    return service.create(
        kind="concept",
        title=title,
        content=content,
        epistemic_basis="source_derived",
    ).knowledge_object.id


def _ids(results: tuple) -> list[int]:
    return [result.id for result in results]


# --- recall behavior --------------------------------------------------------


def test_token_or_recalls_every_matching_term(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    first = _create_object(objects, "Alpha object", "alpha only")
    second = _create_object(objects, "Beta object", "beta only")

    results = service.search("alpha beta")

    assert {first, second} <= set(_ids(results))


def test_quoted_phrase_matches_adjacent_indexed_tokens(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    phrase_object = _create_object(objects, "Phrase object", "cavitation analysis guide")
    other_object = _create_object(objects, "Other object", "cavitation unrelated analysis")

    phrase_results = service.search('"cavitation analysis"')
    token_results = service.search("cavitation analysis")

    assert phrase_object in _ids(phrase_results)
    assert other_object not in _ids(phrase_results)
    assert {phrase_object, other_object} <= set(_ids(token_results))


def test_chinese_quoted_phrase_matches_adjacent_jieba_tokens(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    object_id = _create_object(objects, "中文短语", "液压系统故障分析")

    results = service.search('"液压系统"')

    assert object_id in _ids(results)


def test_prefix_star_is_not_supported(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    _create_object(objects, "Prefix object", "cavitation")

    assert service.search("cavit*") == ()


def test_invalid_fts_expression_returns_empty_and_logs(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, _, objects, _ = search
    _create_object(objects, "Guard object", "guard content")
    with caplog.at_level("WARNING"):
        results = database.search_knowledge("guard AND")
    assert results == []
    assert any("无效的知识 FTS5 检索表达式" in record.message for record in caplog.records)


# --- ordering ---------------------------------------------------------------


def test_bm25_ranks_more_frequent_term_first(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    single = _create_object(objects, "Single", "term")
    triple = _create_object(objects, "Triple", "term term term")

    results = service.search("term")

    assert _ids(results)[0] == triple
    assert single in _ids(results)


def test_equal_bm25_orders_updated_at_desc_then_id_desc(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, service, objects, _ = search
    first = _create_object(objects, "First", "sameword")
    second = _create_object(objects, "Second", "sameword")
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE knowledge_objects SET updated_at = ?", (TS,)
        )
        connection.commit()

    results = service.search("sameword")

    assert [result.id for result in results] == [second, first]


def test_knowledge_object_block_precedes_memory_block(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, memories = search
    object_id = _create_object(objects, "Group object", "sharedterm")
    memory = memories.create_entry(kind="experience", title="Group memory", content="sharedterm")

    results = service.search("sharedterm")

    assert [result.result_type for result in results] == [
        KnowledgeSearchResultType.KNOWLEDGE_OBJECT,
        KnowledgeSearchResultType.KNOWLEDGE_MEMORY,
    ]
    assert [result.id for result in results] == [object_id, memory.id]


# --- snippet / provenance / offline ----------------------------------------


def test_snippet_contains_matched_term(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    _create_object(objects, "Snippet object", "snippet keyword appears here")

    result = service.search("keyword")[0]

    assert "keyword" in result.snippet.casefold()


def test_knowledge_object_provenance_anchors_are_complete(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, service, objects, _ = search
    document = database.create_document(
        title="来源文档",
        filename="source.pdf",
        source_path=Path("data/raw/source.pdf"),
        sha256="a" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=Path("data/pages/source/page_0001.png"),
        extracted_text="页面文本",
    )
    object_id = _create_object(objects, "Provenance object", "provenance keyword")
    objects.link_source(object_id, source_type="page", source_id=page.id, source_note="关键页")

    result = service.search("provenance")[0]

    assert result.result_type is KnowledgeSearchResultType.KNOWLEDGE_OBJECT
    assert result.stable_id.endswith(f":knowledge_object:{object_id}")
    assert result.source_anchors == (("page", page.id),)


def test_memory_provenance_anchors_are_complete(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, service, objects, memories = search
    document = database.create_document(
        title="记忆文档",
        filename="memory.pdf",
        source_path=Path("data/raw/memory.pdf"),
        sha256="b" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=Path("data/pages/memory/page_0001.png"),
        extracted_text="页面文本",
    )
    object_id = _create_object(objects, "Unrelated object", "unrelated content")
    memory = memories.create_entry(
        kind="experience",
        title="Memory anchor",
        content="memory anchor keyword",
        knowledge_object_id=object_id,
        document_id=document.id,
        page_id=page.id,
    )

    result = service.search("memory anchor")[0]

    assert result.result_type is KnowledgeSearchResultType.KNOWLEDGE_MEMORY
    assert result.stable_id.endswith(f":knowledge_memory:{memory.id}")
    assert result.knowledge_object_id == object_id
    assert result.document_id == document.id
    assert result.page_id == page.id


def test_search_runs_offline_without_ai_or_embedding(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    object_id = _create_object(objects, "Offline object", "offline keyword")

    results = service.search("offline")

    assert [result.id for result in results] == [object_id]


def test_empty_query_returns_empty_tuple(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, _ = search
    _create_object(objects, "Empty query object", "content")

    assert service.search("") == ()
    assert service.search('""') == ()


def test_limit_applies_per_type(
    search: tuple[Database, KnowledgeSearchService, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    _, service, objects, memories = search
    for index in range(3):
        _create_object(objects, f"Object {index}", "limitterm")
        memories.create_entry(kind="experience", title=f"Memory {index}", content="limitterm")

    results = service.search("limitterm", limit=2)

    object_ids = [
        result.id
        for result in results
        if result.result_type is KnowledgeSearchResultType.KNOWLEDGE_OBJECT
    ]
    memory_ids = [
        result.id
        for result in results
        if result.result_type is KnowledgeSearchResultType.KNOWLEDGE_MEMORY
    ]
    assert len(object_ids) == 2
    assert len(memory_ids) == 2
    assert [result.result_type for result in results[:2]] == [
        KnowledgeSearchResultType.KNOWLEDGE_OBJECT,
        KnowledgeSearchResultType.KNOWLEDGE_OBJECT,
    ]
