"""Runtime hybrid search assembly tests (Phase 10E-B).

Fully offline: zero real Qwen API calls, zero LLM, zero rerank, zero
staging/production writes. The real ``PersistentVectorRecallSource`` and the
real SQLite ``Database`` are exercised against fake providers only.

The canonical ``EMBEDDING_DIMENSIONS`` contract is verified through
constructor spies so no new public API is exposed just for tests: the runtime
factory must hand the recall source exactly ``EMBEDDING_DIMENSIONS == 1024``
and ``EMBEDDING_CONFIG_VERSION``, matching the dimensions every embedding is
persisted with under the current config version.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.runtime as runtime
from src.ai.hybrid_search import HybridSearchService, VectorPathStatus
from src.ai.page_indexer import (
    EMBEDDING_CONFIG_VERSION,
    EMBEDDING_DIMENSIONS,
    prepare_page_text,
)
from src.ai.provider import AIUnavailableError, EmbeddingResult
from src.config import Settings
from src.database import Database
from src.models import Page
from src.text_utils import extract_search_terms

MODEL = "qwen3.7-text-embedding"
PAGE1_TEXT = "定时器框图：PSC 预分频器、CNT 计数器、ARR 自动重装载寄存器。"
PAGE2_TEXT = "GPIO 引脚配置与上拉下拉电阻说明。"


@pytest.fixture(autouse=True)
def _clear_runtime_caches() -> None:
    """Isolate the process-wide runtime caches between tests.

    Tests may replace the cached factories with plain lambdas, so clearing is
    defensive: only the original ``lru_cache`` wrappers expose ``cache_clear``.
    """

    def _clear() -> None:
        for target in (
            runtime.application_hybrid_search_service,
            runtime.application_ai_provider,
            runtime.application_database,
        ):
            if hasattr(target, "cache_clear"):
                target.cache_clear()

    _clear()
    yield
    _clear()


def _settings(database_path: Path, **overrides: object) -> Settings:
    """Fully isolated temp settings rooted around a temp database path."""
    base: dict[str, object] = {
        "_env_file": None,
        "data_dir": database_path.parent.parent,
        "raw_dir": database_path.parent.parent / "raw",
        "pages_dir": database_path.parent.parent / "pages",
        "markdown_dir": database_path.parent.parent / "markdown",
        "database_dir": database_path.parent,
        "database_path": database_path,
        "backups_dir": database_path.parent.parent.parent / "backups",
        "logs_dir": database_path.parent.parent.parent / "logs",
        "log_path": database_path.parent.parent.parent / "logs" / "test.log",
        "runtime_dir": database_path.parent.parent.parent / "runtime",
        "pid_path": database_path.parent.parent.parent / "runtime" / "pid.json",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _library(database_path: Path) -> tuple[Database, tuple[Page, ...]]:
    """Create one document with two pages; returns the live database and pages."""
    database = Database(database_path)
    document = database.create_document(
        title="STM32入门",
        filename="stm32.pdf",
        source_path=database_path.parent / "stm32.pdf",
        sha256="e" * 64,
    )
    pages: list[Page] = []
    for number, text in enumerate((PAGE1_TEXT, PAGE2_TEXT), start=1):
        pages.append(
            database.create_page(
                document_id=document.id,
                page_number=number,
                image_path=database_path.parent / f"page_{number:04d}.png",
                extracted_text=text,
            )
        )
    return database, tuple(pages)


def _stub_settings(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, **overrides: object
) -> Settings:
    settings = _settings(database_path, **overrides)
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    return settings


def _stub_database(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> None:
    monkeypatch.setattr(runtime, "application_database", lambda: Database(database_path))


class FakeEmbeddingProvider:
    """Deterministic embedding fake returning unit axis-0 vectors."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None, int | None]] = []

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        self.calls.append((tuple(texts), model, dimensions))
        dims = dimensions if dimensions is not None else EMBEDDING_DIMENSIONS
        vector = (1.0,) + (0.0,) * (dims - 1)
        return EmbeddingResult(
            embeddings=tuple(vector for _ in texts),
            model=model or MODEL,
        )


class UnavailableEmbeddingProvider:
    """Embedding fake that always reports AI unavailability."""

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        raise AIUnavailableError("未配置 API Key")


# ------------------------------------------------------------------ dimensions


def test_canonical_embedding_dimensions_constant() -> None:
    """The canonical contract constant is exactly the persisted vector size."""
    assert EMBEDDING_DIMENSIONS == 1024
    assert isinstance(EMBEDDING_DIMENSIONS, int)
    assert EMBEDDING_DIMENSIONS > 0


def test_factory_wires_canonical_dimensions_into_recall_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime factory hands the recall source the canonical contract."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path)
    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-contract-test")
    _stub_database(monkeypatch, database_path)
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: FakeEmbeddingProvider())

    captured: dict[str, object] = {}

    class RecordingRecallSource:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(runtime, "PersistentVectorRecallSource", RecordingRecallSource)

    runtime.application_hybrid_search_service()

    assert captured["dimensions"] == EMBEDDING_DIMENSIONS == 1024
    assert captured["config_version"] == EMBEDDING_CONFIG_VERSION
    assert captured["model"] == MODEL


def test_factory_dimensions_match_persisted_embedding_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory's dimensions round-trip with what the database persists."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, pages = _library(database_path)
    prepared = prepare_page_text(pages[0].searchable_content)
    assert prepared is not None
    database.upsert_page_embedding(
        page_id=pages[0].id,
        source_text_sha256=prepared.sha256,
        model=MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        config_version=EMBEDDING_CONFIG_VERSION,
        vector=(1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1),
    )

    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-contract-test")
    _stub_database(monkeypatch, database_path)
    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: fake)

    outcome = runtime.application_hybrid_search_service().search("定时器")

    assert outcome.vector_status is VectorPathStatus.OK
    assert len(fake.calls) == 1
    assert fake.calls[0][2] == EMBEDDING_DIMENSIONS


# ------------------------------------------------------- assembly without AI


def test_manual_mode_assembles_lexical_only_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API key: the factory degrades to the exact offline lexical search."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path)
    _stub_settings(monkeypatch, database_path)  # default manual, no key
    _stub_database(monkeypatch, database_path)

    service = runtime.application_hybrid_search_service()

    assert isinstance(service, HybridSearchService)
    outcome = service.search("定时器")
    # DISABLED is only reachable when no vector source is configured, so the
    # observable status proves the assembly degraded to lexical-only.
    assert outcome.vector_status is VectorPathStatus.DISABLED
    assert outcome.results


def test_manual_mode_factory_never_initializes_qwen_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the hybrid service in manual mode must not touch the adapter."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path)
    _stub_settings(monkeypatch, database_path)
    _stub_database(monkeypatch, database_path)

    constructed: list[object] = []

    def _blocked_constructor(*args: object, **kwargs: object) -> object:
        constructed.append(kwargs)
        raise AssertionError("manual 模式不应构造 QwenProvider")

    monkeypatch.setattr(runtime, "QwenProvider", _blocked_constructor)

    runtime.application_hybrid_search_service()

    assert constructed == []


# --------------------------------------------------- natural-language matrix


@pytest.mark.parametrize(
    ("query", "page_index"),
    [
        ("定时器预分频器和自动重装载寄存器的作用", 0),
        ("定时器", 0),
        ('"定时器"', 0),
        ("预分频器 自动重装载", 0),
        ("GPIO 引脚", 1),
    ],
)
def test_natural_language_matrix_lexical_non_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    page_index: int,
) -> None:
    """Free-form queries get real FTS terms through the runtime assembly.

    This closes the Phase 10D gap: injecting the raw ``Database`` as the
    lexical source made natural-language queries lexically empty; the runtime
    factory uses ``SearchService``, whose tokenization turns them into real
    FTS5 OR terms.
    """
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, pages = _library(database_path)
    _stub_settings(monkeypatch, database_path)
    _stub_database(monkeypatch, database_path)
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: None)

    assert extract_search_terms(query)  # the query genuinely yields terms
    outcome = runtime.application_hybrid_search_service().search(query)

    assert outcome.vector_status is VectorPathStatus.DISABLED
    assert {item.result.page_id for item in outcome.results} == {pages[page_index].id}
    assert all(item.lexical_rank is not None for item in outcome.results)


def test_natural_language_no_match_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query whose terms exist nowhere still returns an empty result set."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path)
    _stub_settings(monkeypatch, database_path)
    _stub_database(monkeypatch, database_path)
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: None)

    outcome = runtime.application_hybrid_search_service().search("不存在词xyzabc")

    assert outcome.vector_status is VectorPathStatus.DISABLED
    assert outcome.results == ()


# ------------------------------------------------- fake-provider regression


def test_fake_provider_hybrid_regression_full_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real recall source + real SQLite + fake provider fuse both branches."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, pages = _library(database_path)
    page1, _page2 = pages
    prepared = prepare_page_text(page1.searchable_content)
    assert prepared is not None
    database.upsert_page_embedding(
        page_id=page1.id,
        source_text_sha256=prepared.sha256,
        model=MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        config_version=EMBEDDING_CONFIG_VERSION,
        vector=(1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1),
    )

    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-regression-test")
    _stub_database(monkeypatch, database_path)
    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: fake)

    outcome = runtime.application_hybrid_search_service().search("定时器")

    assert outcome.vector_status is VectorPathStatus.OK
    assert outcome.invalid_vector_candidates == 0
    hit = next(
        item for item in outcome.results if item.result.page_id == page1.id
    )
    assert hit.lexical_rank == 1
    assert hit.vector_rank == 1
    # RRF k=60: both branches contribute 1/(60+1).
    assert hit.fused_score == pytest.approx(2.0 / 61.0)
    assert hit.result.document_title == "STM32入门"
    assert hit.result.filename == "stm32.pdf"
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == ("定时器",)
    assert fake.calls[0][1] == MODEL
    assert fake.calls[0][2] == EMBEDDING_DIMENSIONS


def test_fake_provider_hybrid_without_stored_embeddings_reports_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stored vectors: one query embedding still runs, lexical is preserved."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, pages = _library(database_path)
    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-regression-test")
    _stub_database(monkeypatch, database_path)
    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: fake)

    outcome = runtime.application_hybrid_search_service().search("定时器")

    assert outcome.vector_status is VectorPathStatus.EMPTY
    assert len(fake.calls) == 1
    page_ids = {item.result.page_id for item in outcome.results}
    assert pages[0].id in page_ids


def test_natural_language_query_fuses_both_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A free-form sentence flows through lexical AND vector in the assembly.

    This is the Phase 10D gap closed: the same natural-language query that was
    lexically empty under a raw ``Database`` injection now produces a lexical
    rank, and the vector branch still contributes its own rank via RRF.
    """
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, pages = _library(database_path)
    page1, _page2 = pages
    prepared = prepare_page_text(page1.searchable_content)
    assert prepared is not None
    database.upsert_page_embedding(
        page_id=page1.id,
        source_text_sha256=prepared.sha256,
        model=MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        config_version=EMBEDDING_CONFIG_VERSION,
        vector=(1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1),
    )

    query = "定时器预分频器和自动重装载寄存器的作用"
    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-regression-test")
    _stub_database(monkeypatch, database_path)
    fake = FakeEmbeddingProvider()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: fake)

    outcome = runtime.application_hybrid_search_service().search(query)

    assert outcome.vector_status is VectorPathStatus.OK
    hit = next(item for item in outcome.results if item.result.page_id == page1.id)
    assert hit.lexical_rank is not None  # natural-language lexical gap closed
    assert hit.vector_rank is not None
    assert hit.fused_score == pytest.approx(1.0 / 61.0 + 1.0 / 61.0)
    assert fake.calls[0][0] == (query,)
    assert fake.calls[0][2] == EMBEDDING_DIMENSIONS


def test_fake_provider_unavailable_degrades_to_lexical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider unavailability must never break the lexical path."""
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, pages = _library(database_path)
    _stub_settings(monkeypatch, database_path, ai_mode="api", ai_api_key="sk-regression-test")
    _stub_database(monkeypatch, database_path)
    monkeypatch.setattr(
        runtime, "application_ai_provider", lambda: UnavailableEmbeddingProvider()
    )

    outcome = runtime.application_hybrid_search_service().search("定时器")

    assert outcome.vector_status is VectorPathStatus.UNAVAILABLE
    page_ids = {item.result.page_id for item in outcome.results}
    assert pages[0].id in page_ids
