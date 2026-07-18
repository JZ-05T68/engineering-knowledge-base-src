"""Unit tests for local search orchestration and manual prompt generation."""

from __future__ import annotations

from pathlib import Path

from src.models import PageStatus, SearchResult
from src.prompt_builder import PromptBuilder, build_prompt
from src.search_service import SearchService


class FakeDatabase:
    """Small in-memory stand-in for the Database search contract."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.results = results or []
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 20, **options) -> list[SearchResult]:
        del options
        self.calls.append((query, limit))
        return self.results[:limit]


def make_result(
    *,
    content: str = "液压泵出现异常噪声时，应检查吸油管路和油液状态。",
    snippet: str = "",
) -> SearchResult:
    return SearchResult(
        page_id=7,
        document_id=3,
        document_title="液压设备维护手册",
        filename="hydraulics.pdf",
        page_number=12,
        image_path=Path("data/pages/3/page_0012.png"),
        content=content,
        snippet=snippet,
        rank=-1.5,
        status=PageStatus.READY,
    )


def test_normalize_query_is_segmented_deduplicated_and_fts_safe() -> None:
    service = SearchService(FakeDatabase())

    normalized = service.normalize_query('  pump，pump AND "noise"  ')

    assert normalized == '"pump" OR "noise"'
    assert service.normalize_query("液压系统故障").startswith('"')


def test_empty_or_punctuation_only_query_does_not_call_database() -> None:
    database = FakeDatabase()
    service = SearchService(database)

    assert service.search(" \n ，！？ ") == []
    assert database.calls == []


def test_search_passes_normalized_query_and_builds_natural_snippet() -> None:
    content = "前置说明。" * 35 + "pump：液压泵异常噪声需要检查吸油管路。" + "后续说明。" * 35
    database = FakeDatabase([make_result(content=content)])
    service = SearchService(database, snippet_length=80)

    results = service.search("pump noise", limit=5)

    assert database.calls == [('"pump" OR "noise"', 5)]
    assert len(results) == 1
    assert "液压泵异常噪声" in results[0].snippet
    assert results[0].snippet.startswith("…")
    assert results[0].snippet.endswith("…")
    assert "<mark>" not in results[0].snippet


def test_prompt_contains_numbered_sources_and_strict_citation_rules() -> None:
    prompt = PromptBuilder().build("异常噪声应检查什么？", [make_result()])

    assert "只能根据“知识片段”" in prompt
    assert "信息不足" in prompt
    assert "每个事实性结论后都必须引用来源" in prompt
    assert "【文档名，第N页】" in prompt
    assert "[来源 1] 【液压设备维护手册，第12页】" in prompt
    assert "异常噪声应检查什么？" in prompt


def test_prompt_without_results_is_still_safe_and_copyable() -> None:
    prompt = build_prompt("未知问题", [])

    assert "（未提供知识片段）" in prompt
    assert "只能根据" in prompt
    assert "根据提供的知识片段，信息不足" in prompt
