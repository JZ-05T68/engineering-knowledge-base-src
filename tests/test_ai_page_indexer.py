"""Phase 9 page indexing orchestration tests. Fully offline, fake provider only.

Proves the cost guard end to end against a real temp SQLite schema v8
database: dry-run statistics, freshness reuse (zero provider calls),
bounded batching, failure semantics, idempotency, stale-only reindexing,
and readability of the freshly indexed rows by ``PersistentVectorRecallSource``.
"""

from __future__ import annotations

import hashlib
import socket
import sqlite3
from pathlib import Path

import pytest

from src.ai.page_indexer import (
    DEFAULT_BATCH_SIZE,
    EMBEDDING_CONFIG_VERSION,
    MAX_SOURCE_TEXT_CHARS,
    PageEmbeddingIndexer,
    PageIndexStatus,
    prepare_page_text,
)
from src.ai.provider import (
    AIExecutionError,
    AIUnavailableError,
    EmbeddingResult,
)
from src.ai.vector_recall import (
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
)
from src.database import Database
from src.models import PageStatus

MODEL = "fake-embedding-model"
DIMS = 3
CONFIG = EMBEDDING_CONFIG_VERSION


# ------------------------------------------------------------------------ fakes
def _default_vector(text: str) -> tuple[float, ...]:
    """Deterministic non-zero fake vector derived from the text."""

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return (digest[0] / 255 + 0.5, digest[1] / 255 + 0.5, digest[2] / 255 + 0.5)


class FakeEmbeddingProvider:
    """Countable, no-retry fake of the vendor-neutral ``EmbeddingProvider``."""

    def __init__(
        self,
        *,
        vectors: dict[str, tuple[float, ...]] | None = None,
        error: Exception | None = None,
        fail_on_text: str | None = None,
    ) -> None:
        self._vectors = vectors or {}
        self._error = error
        self._fail_on_text = fail_on_text
        self.calls: list[tuple[tuple[str, ...], str | None, int | None]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def embedded_texts(self) -> tuple[str, ...]:
        return tuple(text for call in self.calls for text in call[0])

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        self.calls.append((tuple(texts), model, dimensions))
        if self._error is not None:
            raise self._error
        if self._fail_on_text is not None and self._fail_on_text in texts:
            raise AIExecutionError("批次含失败标记文本")
        return EmbeddingResult(
            embeddings=tuple(
                self._vectors.get(text, _default_vector(text)) for text in texts
            ),
            model=model or MODEL,
        )


def _indexer(
    database: Database,
    provider: FakeEmbeddingProvider,
    *,
    batch_size: int = 25,
) -> PageEmbeddingIndexer:
    return PageEmbeddingIndexer(
        database=database,
        embedding=provider,
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
        batch_size=batch_size,
    )


def _library(
    tmp_path: Path, texts: dict[str, str]
) -> tuple[Database, dict[str, int]]:
    """One document; ``texts`` maps a key to each page's extracted text."""

    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=tmp_path / "hyd.pdf",
        sha256="d" * 64,
    )
    ids: dict[str, int] = {}
    for number, (key, text) in enumerate(texts.items(), start=1):
        page = database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=tmp_path / f"page_{number:04d}.png",
            extracted_text=text,
        )
        ids[key] = page.id
    return database, ids


def _embedding_count(database: Database) -> int:
    with sqlite3.connect(database.database_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM page_embeddings").fetchone()[0]
        )


# ------------------------------------------------------- text preparation (§1)
def test_prepare_page_text_skips_empty_and_whitespace() -> None:
    assert prepare_page_text("") is None
    assert prepare_page_text("   \n\t  ") is None


def test_prepare_page_text_is_deterministic() -> None:
    first = prepare_page_text("闭环控制原文")
    second = prepare_page_text("闭环控制原文")
    assert first is not None and second is not None
    assert first == second
    assert first.sha256 == hashlib.sha256("闭环控制原文".encode()).hexdigest()
    assert first.truncated is False


def test_prepare_page_text_truncates_explicitly() -> None:
    content = "泵" * (MAX_SOURCE_TEXT_CHARS + 100)
    prepared = prepare_page_text(content)
    assert prepared is not None
    assert prepared.truncated is True
    assert len(prepared.text) == MAX_SOURCE_TEXT_CHARS
    assert prepared.sha256 == hashlib.sha256(
        content[:MAX_SOURCE_TEXT_CHARS].encode("utf-8")
    ).hexdigest()


def test_indexer_configuration_is_validated(tmp_path: Path) -> None:
    database, _ = _library(tmp_path, {"A": "文本"})
    provider = FakeEmbeddingProvider()
    for kwargs in (
        {"model": "  "},
        {"dimensions": 0},
        {"config_version": 0},
        {"batch_size": 0},
        {"batch_size": -2},
    ):
        payload = {
            "database": database,
            "embedding": provider,
            "model": MODEL,
            "dimensions": DIMS,
            "config_version": CONFIG,
            "batch_size": 25,
        }
        payload.update(kwargs)
        with pytest.raises(ValueError):
            PageEmbeddingIndexer(**payload)


# ------------------------------------------------------- dry-run (§4) and reuse
def test_dry_run_classifies_without_provider_calls(tmp_path: Path) -> None:
    database, ids = _library(
        tmp_path, {"A": "文本 A", "B": "文本 B", "C": "   ", "D": "文本 D"}
    )
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider)
    # 预先：A fresh；D stale（存储 hash 与当前文本不符）
    first_run = indexer.index_pages()
    assert first_run.indexed == 3  # A、B、D；C 空白跳过
    # 改 D 的文本使其 stale
    database.update_page_markdown(ids["D"], "# 改写后的 D", None, review_status=PageStatus.DRAFT)

    plan = indexer.plan_indexing()

    assert provider.call_count == 1  # dry-run 不产生新调用
    assert plan.total == 4
    assert plan.reused == 2  # A、B 仍 fresh
    assert plan.missing == 0
    assert plan.stale == 1  # D
    assert plan.skipped_empty == 1  # C
    assert plan.to_generate == 1


def test_fresh_page_is_reused_with_zero_provider_calls(tmp_path: Path) -> None:
    database, ids = _library(tmp_path, {"A": "文本 A"})
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider)
    indexer.index_pages()
    assert provider.call_count == 1

    report = indexer.index_pages()

    assert report.reused == 1
    assert report.indexed == 0
    assert report.provider_calls == 0
    assert provider.call_count == 1  # 全程只调过一次
    assert _embedding_count(database) == 1
    stored = database.get_page_embedding(
        page_id=ids["A"], model=MODEL, dimensions=DIMS, config_version=CONFIG
    )
    assert stored is not None
    assert stored.source_text_sha256 == prepare_page_text("文本 A").sha256


# ------------------------------------------------------------- batch / cost (§5/§6)
def test_batching_is_bounded_and_order_correspondent(tmp_path: Path) -> None:
    texts = {f"P{index}": f"第 {index} 页文本" for index in range(1, 8)}
    database, ids = _library(tmp_path, texts)
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider, batch_size=3)

    report = indexer.index_pages()

    assert report.indexed == 7
    assert report.provider_calls == 3  # 3 + 3 + 1
    assert [len(call[0]) for call in provider.calls] == [3, 3, 1]
    # page ↔ vector 顺序对应：每页存的就是该页文本的确定性向量
    # （存储为 float32，按精度容差比较）
    for key, page_id in ids.items():
        stored = database.get_page_embedding(
            page_id=page_id, model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        assert stored is not None
        assert stored.vector == pytest.approx(_default_vector(texts[key]), rel=1e-6)


def test_vector_count_mismatch_fails_whole_batch_closed(tmp_path: Path) -> None:
    database, ids = _library(tmp_path, {"A": "文本 A", "B": "文本 B"})

    class ShortProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, model=None, dimensions=None) -> EmbeddingResult:
            self.calls.append((tuple(texts), model, dimensions))
            return EmbeddingResult(embeddings=((1.0, 0.0, 0.0),), model=MODEL)

    provider = ShortProvider()
    report = _indexer(database, provider).index_pages()

    assert report.failed == 2
    assert report.indexed == 0
    assert _embedding_count(database) == 0
    assert all("vector_count_mismatch" in failure.reason for failure in report.failures)


def test_invalid_vector_fails_only_that_page(tmp_path: Path) -> None:
    database, ids = _library(tmp_path, {"A": "文本 A", "B": "文本 B"})
    provider = FakeEmbeddingProvider(vectors={"文本 B": (float("nan"), 0.0, 0.0)})
    report = _indexer(database, provider).index_pages()

    assert report.indexed == 1
    assert report.failed == 1
    assert [failure.page_id for failure in report.failures] == [ids["B"]]
    assert report.failures[0].reason == "vector_non_finite"
    # A 已写入且不受 B 失败影响
    assert (
        database.get_page_embedding(
            page_id=ids["A"], model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        is not None
    )
    assert (
        database.get_page_embedding(
            page_id=ids["B"], model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        is None
    )


# ------------------------------------------------------------- failure semantics
def test_batch_failure_does_not_roll_back_other_batches(tmp_path: Path) -> None:
    texts = {"A": "文本 A", "B": "引爆批次", "C": "文本 C"}
    database, ids = _library(tmp_path, texts)
    provider = FakeEmbeddingProvider(fail_on_text="引爆批次")
    indexer = _indexer(database, provider, batch_size=1)

    report = indexer.index_pages()

    assert report.provider_calls == 3  # 每批一次，无重试
    assert report.indexed == 2
    assert report.failed == 1
    assert [failure.page_id for failure in report.failures] == [ids["B"]]
    assert report.failures[0].reason == "provider_error"
    # 成功的批次已独立提交，未被回滚
    assert (
        database.get_page_embedding(
            page_id=ids["A"], model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        is not None
    )
    assert (
        database.get_page_embedding(
            page_id=ids["C"], model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        is not None
    )


def test_provider_unavailable_aborts_without_disguising_success(tmp_path: Path) -> None:
    database, ids = _library(tmp_path, {"A": "文本 A", "B": "文本 B", "C": "文本 C"})
    provider = FakeEmbeddingProvider(error=AIUnavailableError("未配置 API Key"))

    report = _indexer(database, provider).index_pages()

    assert report.provider_calls == 1  # 首次调用即不可用，立即中止
    assert report.indexed == 0
    assert report.failed == 3
    assert {failure.reason for failure in report.failures} == {"provider_unavailable"}
    assert _embedding_count(database) == 0


# ------------------------------------------------------------------ idempotency
def test_second_run_is_fully_reused_with_zero_calls(tmp_path: Path) -> None:
    database, _ = _library(
        tmp_path, {"A": "文本 A", "B": "文本 B", "C": "文本 C"}
    )
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider)

    first = indexer.index_pages()
    second = indexer.index_pages()

    assert first.indexed == 3 and first.provider_calls == 1
    assert second.reused == 3
    assert second.indexed == 0
    assert second.provider_calls == 0
    assert provider.call_count == 1
    assert _embedding_count(database) == 3


# ------------------------------------------------------------- source mutation
def test_only_mutated_page_is_reindexed(tmp_path: Path) -> None:
    database, ids = _library(
        tmp_path, {"A": "文本 A", "B": "文本 B", "C": "文本 C"}
    )
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider)
    indexer.index_pages()
    assert provider.call_count == 1
    original = database.get_page_embedding(
        page_id=ids["A"], model=MODEL, dimensions=DIMS, config_version=CONFIG
    )

    database.update_page_markdown(
        ids["B"], "# 新的 B 笔记", None, review_status=PageStatus.DRAFT
    )
    plan = indexer.plan_indexing()
    report = indexer.index_pages()

    assert plan.stale == 1 and plan.reused == 2
    assert report.indexed == 1
    assert report.reused == 2
    assert report.provider_calls == 1
    assert provider.calls[-1][0] == ("# 新的 B 笔记",)  # 仅重生成该页
    # A 的记录未被触碰（created_at 保持）
    after = database.get_page_embedding(
        page_id=ids["A"], model=MODEL, dimensions=DIMS, config_version=CONFIG
    )
    assert after == original
    assert _embedding_count(database) == 3  # stale 更新不积累旧行


# ------------------------------------------------------------- recall reads rows
def test_recall_reads_indexed_records(tmp_path: Path) -> None:
    database, ids = _library(
        tmp_path, {"A": "闭环控制 参数", "B": "液压泵维护"}
    )
    vectors = {
        "闭环控制 参数": (0.95, 0.05, 0.0),
        "液压泵维护": (0.0, 1.0, 0.0),
    }
    provider = FakeEmbeddingProvider(vectors=vectors)
    _indexer(database, provider).index_pages()

    class FakeQuery:
        def embed(self, texts, *, model=None, dimensions=None) -> EmbeddingResult:
            return EmbeddingResult(embeddings=((1.0, 0.0, 0.0),), model=MODEL)

    recall = PersistentVectorRecallSource(
        query_embedding=FakeQuery(),
        embeddings=database,
        fingerprints=SearchableContentFingerprintSource(database),
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
    )
    hits = recall.recall("任意查询", limit=10)

    assert [hit.page_id for hit in hits] == [ids["A"], ids["B"]]
    assert [hit.rank for hit in hits] == [1, 2]


# ----------------------------------------------------------------------- offline
def test_indexing_runs_fully_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("page indexing 禁止任何网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    database, _ = _library(tmp_path, {"A": "文本 A", "B": "文本 B"})
    provider = FakeEmbeddingProvider()
    indexer = _indexer(database, provider)
    plan = indexer.plan_indexing()
    report = indexer.index_pages()

    assert plan.missing == 2
    assert report.indexed == 2
    assert report.provider_calls == 1


def test_status_enum_values_are_stable() -> None:
    assert {status.value for status in PageIndexStatus} == {
        "indexed",
        "reused",
        "skipped_empty",
        "failed",
    }


def test_default_batch_size_respects_qwen_input_limit() -> None:
    """qwen3.7-text-embedding 单批最多 20 条输入（2026-08-15 官方文档）。

    仅钉住输入条数边界；单批 128,000 Token 上限的 payload/token budget
    guard 在真实接线阶段前另行补充（deferred）。
    """

    assert 0 < DEFAULT_BATCH_SIZE <= 20


def test_caller_can_override_smaller_batch_size(tmp_path: Path) -> None:
    database, _ = _library(tmp_path, {"A": "文本 A", "B": "文本 B", "C": "文本 C"})
    provider = FakeEmbeddingProvider()
    report = _indexer(database, provider, batch_size=2).index_pages()

    assert report.indexed == 3
    assert [len(call[0]) for call in provider.calls] == [2, 1]
