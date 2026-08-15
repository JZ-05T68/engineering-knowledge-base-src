"""Phase 8 persistent vector recall tests. Fully offline, fake vectors only.

The chain under test: fake query vector → ``PersistentVectorRecallSource`` →
real temp SQLite ``page_embeddings`` (schema v8) → freshness check against
current fingerprints → local cosine → Top-K ``RankedHit`` → the real
``HybridSearchService`` → RRF → hydrated citation-complete ``SearchResult``.
No network, no embedding API, no API key anywhere.
"""

from __future__ import annotations

import math
import socket
import sqlite3
from pathlib import Path

import pytest

from src.ai.embedding_store import EMBEDDING_VECTOR_FORMAT_VERSION
from src.ai.hybrid_search import HybridSearchService, VectorPathStatus
from src.ai.provider import (
    AIUnavailableError,
    EmbeddingResult,
)
from src.ai.retrieval import RankedHit, VectorRecallSource
from src.ai.vector_recall import (
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
    VectorScoredHit,
    cosine_similarity,
)
from src.database import Database, DatabaseError

MODEL = "fake-embedding-model"
DIMS = 3
CONFIG = 1


# ------------------------------------------------------------------------ fakes
class FakeQueryEmbedding:
    """Vendor-neutral ``EmbeddingProvider`` fake returning a fixed vector."""

    def __init__(self, vector: tuple[float, ...] | Exception) -> None:
        self._vector = vector
        self.calls: list[tuple[tuple[str, ...], str | None, int | None]] = []

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        self.calls.append((tuple(texts), model, dimensions))
        if isinstance(self._vector, Exception):
            raise self._vector
        return EmbeddingResult(
            embeddings=(self._vector,), model=model or "fake", usage=None
        )


class DictFingerprint:
    """``CurrentFingerprintSource`` fake backed by an explicit mapping."""

    def __init__(self, mapping: dict[int, str]) -> None:
        self._mapping = mapping

    def current_source_sha256(self, page_id: int) -> str | None:
        return self._mapping.get(page_id)


def _library(tmp_path: Path) -> tuple[Database, dict[str, int]]:
    """One document with four pages; returns the database and page ids."""

    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=tmp_path / "hyd.pdf",
        sha256="d" * 64,
    )
    ids: dict[str, int] = {}
    for number, key in enumerate(("A", "B", "C", "D"), start=1):
        page = database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=tmp_path / f"page_{number:04d}.png",
            extracted_text=f"第 {number} 页 {key} 文本",
        )
        ids[key] = page.id
    return database, ids


def _store_current(
    database: Database,
    page_id: int,
    vector: tuple[float, ...],
    *,
    model: str = MODEL,
    dimensions: int = DIMS,
    config_version: int = CONFIG,
    fingerprint: str | None = None,
) -> None:
    """Upsert one embedding whose hash matches the page's current content."""

    stored_hash = (
        fingerprint
        if fingerprint is not None
        else SearchableContentFingerprintSource(database).current_source_sha256(page_id)
    )
    assert stored_hash is not None
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=stored_hash,
        model=model,
        dimensions=dimensions,
        config_version=config_version,
        vector=vector,
    )


def _recall(
    database: Database,
    *,
    query_vector: tuple[float, ...] | Exception = (1.0, 0.0, 0.0),
    fingerprints: DictFingerprint | SearchableContentFingerprintSource | None = None,
) -> PersistentVectorRecallSource:
    return PersistentVectorRecallSource(
        query_embedding=FakeQueryEmbedding(query_vector),
        embeddings=database,
        fingerprints=fingerprints or SearchableContentFingerprintSource(database),
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
    )


# -------------------------------------------------------------- cosine (A–E)
def test_cosine_similarity_correctness() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)
    assert cosine_similarity((3.0, 4.0), (3.0, 4.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 1.0), (1.0, 0.0)) == pytest.approx(
        1.0 / math.sqrt(2.0)
    )


def test_cosine_rejects_zero_vectors() -> None:
    with pytest.raises(ValueError):
        cosine_similarity((0.0, 0.0), (1.0, 0.0))
    with pytest.raises(ValueError):
        cosine_similarity((1.0, 0.0), (0.0, 0.0))


def test_cosine_rejects_dimension_mismatch_and_empty() -> None:
    with pytest.raises(ValueError):
        cosine_similarity((1.0, 0.0), (1.0,))
    with pytest.raises(ValueError):
        cosine_similarity((), ())


def test_cosine_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        cosine_similarity((float("nan"), 0.0), (1.0, 0.0))
    with pytest.raises(ValueError):
        cosine_similarity((1.0, 0.0), (float("inf"), 0.0))


def test_cosine_is_deterministic() -> None:
    a = (0.1, 0.2, 0.3, 0.4)
    b = (0.4, 0.3, 0.2, 0.1)
    assert cosine_similarity(a, b) == cosine_similarity(a, b)


# ------------------------------------------------- DB listing (F–I)
def test_list_page_embeddings_filters_configuration(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.5, 0.5, 0.5))
    _store_current(database, ids["B"], (0.25, 0.25, 0.25), model="other-model")
    _store_current(database, ids["C"], (0.1,) * 4, dimensions=4)
    _store_current(database, ids["D"], (0.2, 0.2, 0.2), config_version=2)

    rows = database.list_page_embeddings(
        model=MODEL, dimensions=DIMS, config_version=CONFIG
    )

    assert [row.page_id for row in rows] == [ids["A"]]
    assert rows[0].vector == (0.5, 0.5, 0.5)


def test_list_page_embeddings_order_is_deterministic(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    for key in ("D", "B", "A", "C"):
        _store_current(database, ids[key], (0.5, 0.5, 0.5))

    rows = database.list_page_embeddings(
        model=MODEL, dimensions=DIMS, config_version=CONFIG
    )

    assert [row.page_id for row in rows] == sorted(ids.values())


def test_list_page_embeddings_validates_configuration(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    with pytest.raises(ValueError):
        database.list_page_embeddings(model="  ", dimensions=DIMS, config_version=CONFIG)
    with pytest.raises(ValueError):
        database.list_page_embeddings(model=MODEL, dimensions=0, config_version=CONFIG)
    with pytest.raises(ValueError):
        database.list_page_embeddings(model=MODEL, dimensions=DIMS, config_version=-1)


def test_list_page_embeddings_decode_fails_closed(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.5, 0.5, 0.5))
    corrupted = sqlite3.Binary(
        bytes([EMBEDDING_VECTOR_FORMAT_VERSION + 1]) + b"\x00" * 12
    )
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("UPDATE page_embeddings SET vector = ?", (corrupted,))

    with pytest.raises(DatabaseError):
        database.list_page_embeddings(
            model=MODEL, dimensions=DIMS, config_version=CONFIG
        )


# ------------------------------------------- fresh/stale candidates (J–M, §18)
def test_recall_includes_only_fresh_current_configuration(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    fingerprints = SearchableContentFingerprintSource(database)
    # A: fresh → 召回
    _store_current(database, ids["A"], (0.95, 0.05, 0.0))
    # B: stale（存储 hash 与当前 fingerprint 不符）→ 不得召回
    _store_current(database, ids["B"], (0.99, 0.01, 0.0), fingerprint="0" * 64)
    # C: hash 匹配但属于另一 model → 不属于当前 recall 配置
    _store_current(database, ids["C"], (1.0, 0.0, 0.0), model="other-model")
    # D: 无 embedding → 不得召回

    scored = _recall(database).recall_scored("任意", limit=10)

    assert [hit.page_id for hit in scored] == [ids["A"]]
    assert fingerprints.current_source_sha256(ids["B"]) is not None


def test_recall_excludes_wrong_dimensions_and_config(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.9, 0.1, 0.0))
    _store_current(database, ids["B"], (1.0, 0.0, 0.0, 0.0), dimensions=4)
    _store_current(database, ids["C"], (1.0, 0.0, 0.0), config_version=2)

    scored = _recall(database).recall_scored("任意", limit=10)

    assert [hit.page_id for hit in scored] == [ids["A"]]


def test_recall_excludes_candidates_without_current_page(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.9, 0.1, 0.0))
    _store_current(database, ids["B"], (0.8, 0.2, 0.0))
    # fingerprint source 已无法解析 B（例如页面刚被删除的竞态）；
    # A 使用真实 hash，保证 A 确实 fresh
    real = SearchableContentFingerprintSource(database)
    mapping = {ids["A"]: real.current_source_sha256(ids["A"]) or ""}

    scored = _recall(database, fingerprints=DictFingerprint(mapping)).recall_scored(
        "任意", limit=10
    )

    assert [hit.page_id for hit in scored] == [ids["A"]]


# ------------------------------------------------------- core ranking (§19, N–P)
def test_recall_ranking_is_human_verifiable(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.95, 0.05, 0.0))
    _store_current(database, ids["B"], (0.7, 0.3, 0.0))
    _store_current(database, ids["C"], (0.0, 1.0, 0.0))
    # stale D / wrong model E 场景已由上文覆盖；此处聚焦排序
    _store_current(database, ids["D"], (1.0, 0.0, 0.0), fingerprint="0" * 64)

    hits = _recall(database).recall("任意", limit=10)

    assert hits == (
        RankedHit(page_id=ids["A"], rank=1),
        RankedHit(page_id=ids["B"], rank=2),
        RankedHit(page_id=ids["C"], rank=3),
    )


def test_recall_tie_breaks_by_page_id(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    for key in ("C", "A", "B"):
        _store_current(database, ids[key], (1.0, 1.0, 0.0))

    hits = _recall(database).recall("任意", limit=10)

    assert [hit.page_id for hit in hits] == sorted(
        [ids["A"], ids["B"], ids["C"]]
    )
    assert [hit.rank for hit in hits] == [1, 2, 3]


def test_recall_limit_caps_and_validates(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    for key in ("A", "B", "C"):
        _store_current(database, ids[key], (1.0, 0.0, 0.0))
    recall = _recall(database)

    assert len(recall.recall("任意", limit=2)) == 2
    assert len(recall.recall("任意", limit=100)) == 3  # 候选不足则返回全部
    for bad_limit in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            recall.recall("任意", limit=bad_limit)  # type: ignore[arg-type]


def test_recall_without_candidates_returns_empty(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    assert _recall(database).recall("任意", limit=10) == ()


def test_recall_has_no_similarity_threshold(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.0, 1.0, 0.0))  # cosine = 0
    _store_current(database, ids["B"], (-1.0, 0.0, 0.0))  # cosine < 0

    scored = _recall(database).recall_scored("任意", limit=10)

    assert [hit.page_id for hit in scored] == [ids["A"], ids["B"]]
    assert scored[0].similarity == pytest.approx(0.0)
    assert scored[1].similarity < 0.0


def test_zero_vector_candidate_is_skipped_not_fatal(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (0.9, 0.1, 0.0))
    _store_current(database, ids["B"], (0.0, 0.0, 0.0))  # 零向量候选

    scored = _recall(database).recall_scored("任意", limit=10)

    assert [hit.page_id for hit in scored] == [ids["A"]]


def test_scored_hits_expose_raw_similarity(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (1.0, 0.0, 0.0))

    scored = _recall(database).recall_scored("任意", limit=10)

    assert scored == (VectorScoredHit(page_id=ids["A"], similarity=pytest.approx(1.0)),)


def test_recall_implements_vector_recall_source_protocol(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    assert isinstance(_recall(database), VectorRecallSource)


# ------------------------------------------------- query vector boundary (Q, R)
def test_query_vector_comes_from_injected_provider(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (1.0, 0.0, 0.0))
    provider = FakeQueryEmbedding((0.5, 0.5, 0.0))
    recall = PersistentVectorRecallSource(
        query_embedding=provider,
        embeddings=database,
        fingerprints=SearchableContentFingerprintSource(database),
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
    )

    hits = recall.recall("测试查询", limit=5)

    assert provider.calls == [(("测试查询",), MODEL, DIMS)]
    assert hits == (RankedHit(page_id=ids["A"], rank=1),)


def test_query_vector_unavailable_raises_ai_unavailable(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    recall = _recall(database, query_vector=AIUnavailableError("manual mode"))

    with pytest.raises(AIUnavailableError):
        recall.recall("任意", limit=10)


@pytest.mark.parametrize(
    "bad_vector",
    [
        (1.0, 0.0),  # 维度不符
        (float("nan"), 0.0, 0.0),  # 非有限
        (0.0, 0.0, 0.0),  # 零向量
    ],
)
def test_malformed_query_vector_fails_closed(
    tmp_path: Path, bad_vector: tuple[float, ...]
) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (1.0, 0.0, 0.0))

    with pytest.raises(ValueError):
        _recall(database, query_vector=bad_vector).recall("任意", limit=10)


# --------------------------------------------- hybrid degradation (§21, §22, R)
def _hybrid(
    database: Database, recall: PersistentVectorRecallSource
) -> HybridSearchService:
    return HybridSearchService(lexical=database, hydration=database, vector=recall)


def test_hybrid_degrades_to_unavailable_when_query_vector_missing(
    tmp_path: Path,
) -> None:
    database, ids = _library(tmp_path)
    recall = _recall(database, query_vector=AIUnavailableError("未配置 API Key"))

    outcome = _hybrid(database, recall).search('"第 1 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.UNAVAILABLE
    assert [item.result.page_id for item in outcome.results] == [ids["A"]]


def test_hybrid_degrades_to_failed_on_malformed_query_vector(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (1.0, 0.0, 0.0))
    recall = _recall(database, query_vector=(1.0, 0.0))  # 维度不符

    outcome = _hybrid(database, recall).search('"第 1 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result.page_id for item in outcome.results] == [ids["A"]]


def test_hybrid_empty_when_no_fresh_candidates(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)
    # 只有 stale embedding：不得参与召回
    _store_current(database, ids["B"], (1.0, 0.0, 0.0), fingerprint="0" * 64)
    lexical_only = HybridSearchService(lexical=database, hydration=database)
    expected = [
        item.result.page_id for item in lexical_only.search('"第 2 页"', limit=10).results
    ]

    outcome = _hybrid(database, _recall(database)).search('"第 2 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.EMPTY
    assert [item.result.page_id for item in outcome.results] == expected


def test_hybrid_degrades_to_failed_on_persistence_error(tmp_path: Path) -> None:
    database, ids = _library(tmp_path)

    class BrokenStore:
        def list_page_embeddings(self, **kwargs: object) -> tuple:
            raise DatabaseError("数据库读取失败")

    recall = PersistentVectorRecallSource(
        query_embedding=FakeQueryEmbedding((1.0, 0.0, 0.0)),
        embeddings=BrokenStore(),
        fingerprints=SearchableContentFingerprintSource(database),
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
    )

    outcome = _hybrid(database, recall).search('"第 1 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.FAILED
    assert [item.result.page_id for item in outcome.results] == [ids["A"]]


# ----------------------------------------- hybrid E2E offline scenario (§20, S–U)
def test_persistent_vector_only_candidate_enters_hybrid_with_citation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="控制手册",
        filename="ctrl.pdf",
        source_path=tmp_path / "ctrl.pdf",
        sha256="e" * 64,
    )
    # A / B：含 lexical 关键词；X：无关键词，只能被向量路径召回
    page_a = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page_0001.png",
        extracted_text="闭环控制 参数整定",
    )
    page_b = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=tmp_path / "page_0002.png",
        extracted_text="闭环控制 稳定性分析",
    )
    page_x = database.create_page(
        document_id=document.id,
        page_number=3,
        image_path=tmp_path / "page_0003.png",
        extracted_text="反馈回路的动态响应特性",
    )
    fingerprints = SearchableContentFingerprintSource(database)
    query_vector = (1.0, 0.0, 0.0)
    # vector recall：X rank1，A rank2；B 无 embedding
    _store_current(database, page_x.id, (0.98, 0.02, 0.0))
    _store_current(database, page_a.id, (0.8, 0.2, 0.0))
    # stale 干扰项：即使向量最接近也不得进入
    stale = database.create_page(
        document_id=document.id,
        page_number=4,
        image_path=tmp_path / "page_0004.png",
        extracted_text="已经改写过的页面",
    )
    _store_current(database, stale.id, (1.0, 0.0, 0.0), fingerprint="0" * 64)

    recall = PersistentVectorRecallSource(
        query_embedding=FakeQueryEmbedding(query_vector),
        embeddings=database,
        fingerprints=fingerprints,
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
    )
    outcome = HybridSearchService(
        lexical=database, hydration=database, vector=recall
    ).search('"闭环控制"', limit=10)

    assert outcome.vector_status is VectorPathStatus.OK
    by_page = {item.result.page_id: item for item in outcome.results}
    assert set(by_page) == {page_a.id, page_b.id, page_x.id}
    # X：无 LIKE 命中，纯向量召回，经 RRF 进入 hybrid union
    x_hit = by_page[page_x.id]
    assert x_hit.lexical_rank is None
    assert x_hit.vector_rank == 1
    assert by_page[page_a.id].lexical_rank == 1
    assert by_page[page_a.id].vector_rank == 2
    assert by_page[page_b.id].vector_rank is None
    # X 经现有 hydration 路径恢复完整 citation
    x = x_hit.result
    assert x.document_title == "控制手册"
    assert x.filename == "ctrl.pdf"
    assert x.page_number == 3
    assert x.document_sha256 == "e" * 64
    assert x.image_path == page_x.image_path
    assert x.content == "反馈回路的动态响应特性"
    assert x.match_type == "语义召回"
    # 排序人工可验证：A（lex1+vec2）> X（vec1）> B（lex2）
    assert [item.result.page_id for item in outcome.results] == [
        page_a.id,
        page_x.id,
        page_b.id,
    ]


def test_lexical_behavior_unchanged_without_fresh_vectors(tmp_path: Path) -> None:
    database, _ = _library(tmp_path)
    lexical_only = HybridSearchService(lexical=database, hydration=database)
    baseline = lexical_only.search('"第 1 页"', limit=10)

    outcome = _hybrid(database, _recall(database)).search('"第 1 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.EMPTY
    assert [item.result.page_id for item in outcome.results] == [
        item.result.page_id for item in baseline.results
    ]


# --------------------------------------------------------------- offline (V, W)
def test_persistent_recall_runs_fully_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("vector recall 禁止任何网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    database, ids = _library(tmp_path)
    _store_current(database, ids["A"], (1.0, 0.0, 0.0))
    outcome = _hybrid(database, _recall(database)).search('"第 1 页"', limit=10)

    assert outcome.vector_status is VectorPathStatus.OK
    assert outcome.results[0].result.page_id == ids["A"]
