"""Phase 6A tests: read-only embedding coverage classification.

Fully offline: a temporary in-memory SQLite database, no provider, no network,
no database writes beyond the fixture setup. Confirms the coverage service
reuses the shared freshness policy (missing / stale / fresh / skipped-empty)
and never triggers indexing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ai.coverage_service import CoverageSummary, PageEmbeddingCoverageService
from src.ai.page_indexer import prepare_page_text
from src.database import Database

MODEL = "qwen3.7-text-embedding"
DIMENSIONS = 1024
CONFIG_VERSION = 1


def _library(tmp_path: Path) -> tuple[Database, int]:
    database = Database(tmp_path / "coverage.db")
    document = database.create_document(
        title="测试文档",
        filename="test.pdf",
        source_path=tmp_path / "raw" / "test.pdf",
        sha256="a" * 64,
    )
    return database, document.id


def _vector(dim: int = DIMENSIONS) -> tuple[float, ...]:
    return tuple(1.0 for _ in range(dim))


def test_all_negative_when_no_pages(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary == CoverageSummary(indexable=0, indexed=0, missing=0, stale=0, skipped_empty=0)
    assert summary.coverage_ratio == 0.0


def test_empty_page_is_skipped(tmp_path: Path) -> None:
    database, doc_id = _library(tmp_path)
    database.create_page(
        document_id=doc_id,
        page_number=1,
        image_path=tmp_path / "p1.png",
        extracted_text="",
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.skipped_empty == 1
    assert summary.indexable == 0
    assert summary.coverage_ratio == 0.0


def test_nonempty_page_without_embedding_is_missing(tmp_path: Path) -> None:
    database, doc_id = _library(tmp_path)
    database.create_page(
        document_id=doc_id,
        page_number=1,
        image_path=tmp_path / "p1.png",
        extracted_text="定时器预分频器",
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexable == 1
    assert summary.missing == 1
    assert (summary.indexed, summary.stale) == (0, 0)
    assert summary.coverage_ratio == 0.0


def test_fresh_embedding_is_indexed(tmp_path: Path) -> None:
    database, doc_id = _library(tmp_path)
    page = database.create_page(
        document_id=doc_id,
        page_number=1,
        image_path=tmp_path / "p1.png",
        extracted_text="定时器预分频器",
    )
    prepared = prepare_page_text(page.searchable_content)
    assert prepared is not None
    database.upsert_page_embedding(
        page_id=page.id,
        source_text_sha256=prepared.sha256,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexed == 1
    assert summary.missing == 0
    assert summary.coverage_ratio == 1.0


def test_stale_embedding_is_stale_not_indexed(tmp_path: Path) -> None:
    database, doc_id = _library(tmp_path)
    page = database.create_page(
        document_id=doc_id,
        page_number=1,
        image_path=tmp_path / "p1.png",
        extracted_text="旧文本",
    )
    # Persist a vector under a fingerprint that no longer matches current text.
    database.upsert_page_embedding(
        page_id=page.id,
        source_text_sha256="0" * 64,  # wrong fingerprint -> stale
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexed == 0
    assert summary.stale == 1
    assert summary.missing == 0
    assert summary.coverage_ratio == 0.0


def test_mixed_classification(tmp_path: Path) -> None:
    database, doc_id = _library(tmp_path)
    pages = [
        database.create_page(
            document_id=doc_id,
            page_number=n,
            image_path=tmp_path / f"p{n}.png",
            extracted_text=f"页面 {n} 的文本内容",
        )
        for n in (1, 2, 3)
    ]
    # p1 = fresh, p2 = stale, p3 = missing
    prepared1 = prepare_page_text(pages[0].searchable_content)
    assert prepared1 is not None
    database.upsert_page_embedding(
        page_id=pages[0].id,
        source_text_sha256=prepared1.sha256,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    database.upsert_page_embedding(
        page_id=pages[1].id,
        source_text_sha256="1" * 64,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexable == 3
    assert summary.indexed == 1
    assert summary.stale == 1
    assert summary.missing == 1
    assert summary.coverage_ratio == pytest.approx(1 / 3)


def test_coverage_ratio_excludes_skipped_empty(tmp_path: Path) -> None:
    """skipped_empty must not reduce coverage: 1 indexed + N empty = 100%."""

    database, doc_id = _library(tmp_path)
    good = database.create_page(
        document_id=doc_id,
        page_number=1,
        image_path=tmp_path / "p1.png",
        extracted_text="定时器预分频器",
    )
    for n in range(2, 6):
        database.create_page(
            document_id=doc_id,
            page_number=n,
            image_path=tmp_path / f"p{n}.png",
            extracted_text="",
        )
    prepared = prepare_page_text(good.searchable_content)
    assert prepared is not None
    database.upsert_page_embedding(
        page_id=good.id,
        source_text_sha256=prepared.sha256,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexable == 1
    assert summary.indexed == 1
    assert summary.skipped_empty == 4
    assert summary.coverage_ratio == 1.0


def test_coverage_ratio_mixed_with_skipped_empty(tmp_path: Path) -> None:
    """indexed + missing + stale + skipped_empty: denominator = indexable only."""

    database, doc_id = _library(tmp_path)
    pages = [
        database.create_page(
            document_id=doc_id,
            page_number=n,
            image_path=tmp_path / f"p{n}.png",
            extracted_text=f"页面 {n}",
        )
        for n in range(1, 5)
    ]
    # p1 fresh, p2 stale, p3/p4 missing, plus one empty page
    prepared1 = prepare_page_text(pages[0].searchable_content)
    assert prepared1 is not None
    database.upsert_page_embedding(
        page_id=pages[0].id,
        source_text_sha256=prepared1.sha256,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    database.upsert_page_embedding(
        page_id=pages[1].id,
        source_text_sha256="2" * 64,
        model=MODEL,
        dimensions=DIMENSIONS,
        config_version=CONFIG_VERSION,
        vector=_vector(),
    )
    database.create_page(
        document_id=doc_id,
        page_number=5,
        image_path=tmp_path / "p5.png",
        extracted_text="",
    )
    service = PageEmbeddingCoverageService(database=database, model=MODEL)
    summary = service.coverage_summary()
    assert summary.indexable == 4
    assert summary.indexed == 1
    assert summary.stale == 1
    assert summary.missing == 2
    assert summary.skipped_empty == 1
    assert summary.coverage_ratio == pytest.approx(1 / 4)


def test_coverage_service_rejects_bad_config(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    with pytest.raises(ValueError):
        PageEmbeddingCoverageService(database=database, model=MODEL, dimensions=0)
