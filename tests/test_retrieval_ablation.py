"""检索消融 harness 的机制回归测试（v0.5.1 Phase 2）。

只锁机制：冻结文件完整性、门控召回包装器语义、RRF 变体与生产的等价性、
变体 hybrid 服务与生产的等价性、V0 对 Phase 1D baseline 的复现、运行确定性。
**不断言任何变体的质量阈值**（消融结论由人工阅读报告后决定）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest

import scripts.retrieval_ablation as abl
import scripts.retrieval_benchmark as bench
from src.ai.hybrid_search import HybridSearchService
from src.ai.retrieval import RankedHit, VectorRecallSource, rrf_fuse
from src.ai.vector_recall import (
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
    VectorScoredHit,
)
from src.search_service import SearchService

PROJECT_ROOT = bench.PROJECT_ROOT


class _StubScoredRecall:
    """``recall_scored`` 的最小 stub：固定返回一组带相似度的候选。"""

    def __init__(self, scored: Sequence[VectorScoredHit]) -> None:
        self._scored = tuple(scored)

    def recall_scored(self, query: str, *, limit: int) -> tuple[VectorScoredHit, ...]:
        return self._scored[:limit]


@pytest.fixture(scope="module")
def report() -> abl.AblationReport:
    """执行一次完整消融（模块级共享，避免重复建库）。"""

    return abl.run_ablation(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 冻结文件完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_sha256"),
    [
        (
            "corpus_synthetic_v1.json",
            "8b73a7de5cc1b6e11f517af049bbac7bfdcce94669086c2e970a925265e85465",
        ),
        (
            "queries_v1.json",
            "6e153584d07b03ba1ce8d61f169dff47d1f26cc15652627611c3a485757ef125",
        ),
    ],
)
def test_frozen_files_sha256(filename: str, expected_sha256: str) -> None:
    """冻结基准文件的 sha256（归一化 CRLF → LF 后）必须与合同一致。"""

    data = (PROJECT_ROOT / "benchmarks" / filename).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == expected_sha256


# ---------------------------------------------------------------------------
# GatedVectorRecallSource
# ---------------------------------------------------------------------------


def test_gated_recall_implements_protocol() -> None:
    """包装器实现生产 VectorRecallSource 协议。"""

    gated = abl.GatedVectorRecallSource(_StubScoredRecall(()), min_similarity=0.0)
    assert isinstance(gated, VectorRecallSource)


def test_gated_recall_excludes_zero_similarity() -> None:
    """min_similarity=0.0：相似度恰为 0 与负值被剔除，0.0001 保留（严格 >）。"""

    stub = _StubScoredRecall(
        (
            VectorScoredHit(page_id=1, similarity=0.5),
            VectorScoredHit(page_id=2, similarity=0.0001),
            VectorScoredHit(page_id=3, similarity=0.0),
            VectorScoredHit(page_id=4, similarity=-0.2),
        )
    )
    gated = abl.GatedVectorRecallSource(stub, min_similarity=0.0)  # type: ignore[arg-type]
    hits = gated.recall("任意", limit=50)
    assert [(hit.page_id, hit.rank) for hit in hits] == [(1, 1), (2, 2)]


def test_gated_recall_renumbers_ranks() -> None:
    """门控剔除中间候选后 rank 重新编号为 1..N。"""

    stub = _StubScoredRecall(
        (
            VectorScoredHit(page_id=7, similarity=0.9),
            VectorScoredHit(page_id=8, similarity=0.0),
            VectorScoredHit(page_id=9, similarity=0.3),
        )
    )
    gated = abl.GatedVectorRecallSource(stub, min_similarity=0.0)  # type: ignore[arg-type]
    hits = gated.recall("任意", limit=50)
    assert [(hit.page_id, hit.rank) for hit in hits] == [(7, 1), (9, 2)]


def test_gated_recall_top_k_truncates() -> None:
    """top_k 截断为前 top_k 个候选并重新编号。"""

    stub = _StubScoredRecall(
        tuple(VectorScoredHit(page_id=index, similarity=1.0 - index * 0.1) for index in range(1, 6))
    )
    gated = abl.GatedVectorRecallSource(stub, top_k=2)  # type: ignore[arg-type]
    hits = gated.recall("任意", limit=50)
    assert [(hit.page_id, hit.rank) for hit in hits] == [(1, 1), (2, 2)]


def test_gated_recall_no_gating_passes_through() -> None:
    """无门控参数时输出与 inner 一致（仅重新编号，inner 本就有序）。"""

    stub = _StubScoredRecall(
        (VectorScoredHit(page_id=3, similarity=0.8), VectorScoredHit(page_id=1, similarity=0.1))
    )
    gated = abl.GatedVectorRecallSource(stub)  # type: ignore[arg-type]
    hits = gated.recall("任意", limit=50)
    assert [(hit.page_id, hit.rank) for hit in hits] == [(3, 1), (1, 2)]


@pytest.mark.parametrize("top_k", [0, -1, 1.5, True])
def test_gated_recall_rejects_invalid_top_k(top_k: object) -> None:
    """top_k 必须是正整数。"""

    with pytest.raises(ValueError, match="top_k"):
        abl.GatedVectorRecallSource(_StubScoredRecall(()), top_k=top_k)  # type: ignore[arg-type]


@pytest.mark.parametrize("min_similarity", [float("nan"), float("inf"), True, "0.5"])
def test_gated_recall_rejects_invalid_min_similarity(min_similarity: object) -> None:
    """min_similarity 必须是有限数值。"""

    with pytest.raises(ValueError, match="min_similarity"):
        abl.GatedVectorRecallSource(
            _StubScoredRecall(()), min_similarity=min_similarity  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# rrf_fuse_variant 与生产 rrf_fuse 的等价性（默认参数）
# ---------------------------------------------------------------------------


FUSION_CASES: tuple[tuple[tuple[RankedHit, ...], tuple[RankedHit, ...]], ...] = (
    ((), ()),
    # 双来源累加
    ((RankedHit(1, 1), RankedHit(2, 2)), (RankedHit(2, 1), RankedHit(3, 2))),
    # 纯词面
    ((RankedHit(5, 1), RankedHit(6, 2)), ()),
    # 纯向量
    ((), (RankedHit(1, 1), RankedHit(2, 2))),
    # 精确平分：单来源 rank-1 对单来源 rank-1，生产按 page_id 升序决胜
    ((RankedHit(9, 1),), (RankedHit(1, 1),)),
    # 多页混合 + 部分重叠
    (
        (RankedHit(4, 1), RankedHit(2, 2), RankedHit(7, 3)),
        (RankedHit(2, 1), RankedHit(5, 2), RankedHit(4, 3)),
    ),
)


@pytest.mark.parametrize(("lexical", "vector"), FUSION_CASES)
def test_rrf_fuse_variant_defaults_equal_production(
    lexical: tuple[RankedHit, ...], vector: tuple[RankedHit, ...]
) -> None:
    """默认参数下 rrf_fuse_variant 与生产 rrf_fuse 输出完全一致。"""

    assert abl.rrf_fuse_variant(lexical, vector) == rrf_fuse(lexical, vector)


def test_rrf_fuse_variant_validates_like_production() -> None:
    """变体核的输入校验与生产一致（fail-closed）。"""

    with pytest.raises(ValueError, match="k 必须为正整数"):
        abl.rrf_fuse_variant((), (), k=0)
    with pytest.raises(ValueError, match="重复 page_id"):
        abl.rrf_fuse_variant((RankedHit(1, 1), RankedHit(1, 2)), ())
    with pytest.raises(ValueError, match="rank 必须从 1 开始"):
        abl.rrf_fuse_variant((), (RankedHit(1, 0),))


def test_lexical_first_tiebreak_prefers_lexical_page() -> None:
    """精确平分时 lexical_first_tiebreak 让有词面 rank 的页面排在前面。"""

    lexical = (RankedHit(10, 1),)
    vector = (RankedHit(1, 1),)
    # 生产默认：page_id 升序，纯向量页 page_id=1 在前。
    default = abl.rrf_fuse_variant(lexical, vector)
    assert [hit.page_id for hit in default] == [1, 10]
    tiebreak = abl.rrf_fuse_variant(lexical, vector, lexical_first_tiebreak=True)
    assert [hit.page_id for hit in tiebreak] == [10, 1]


def test_vector_weight_lowers_vector_only_score() -> None:
    """vector_weight<1 只缩向量侧贡献：纯向量页得分减半，词面页不变。"""

    fused = abl.rrf_fuse_variant((RankedHit(2, 1),), (RankedHit(1, 1),), vector_weight=0.5)
    by_id = {hit.page_id: hit for hit in fused}
    assert by_id[1].fused_score == pytest.approx(0.5 / 61)
    assert by_id[2].fused_score == pytest.approx(1.0 / 61)


def test_vector_only_penalty_lowers_only_vector_only_score() -> None:
    """vector_only_penalty<1 只影响纯向量页；双来源页不受影响。"""

    fused = abl.rrf_fuse_variant(
        (RankedHit(1, 1),), (RankedHit(1, 2), RankedHit(2, 1)), vector_only_penalty=0.5
    )
    by_id = {hit.page_id: hit for hit in fused}
    assert by_id[2].fused_score == pytest.approx(0.5 / 61)
    assert by_id[1].fused_score == pytest.approx(1.0 / 61 + 1.0 / 62)


# ---------------------------------------------------------------------------
# VariantHybridSearchService 与生产 HybridSearchService 的等价性（默认参数）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_services(tmp_path_factory: pytest.TempPathFactory) -> tuple:
    """在临时目录建真实 fixture 库，返回生产/变体两个 hybrid 服务与 query 集。"""

    corpus = bench.load_corpus(PROJECT_ROOT / "benchmarks" / "corpus_synthetic_v1.json")
    query_set = bench.load_queries(PROJECT_ROOT / "benchmarks" / "queries_v1.json", corpus)
    work_dir = tmp_path_factory.mktemp("ablation-equivalence")
    database, page_ids, _ = bench._build_database(corpus, work_dir)
    bench._write_fixture_embeddings(corpus, database, page_ids)
    provider = bench.FixtureEmbeddingProvider(
        {
            query.text: bench.fixture_vector(
                query.topics, corpus.topics, corpus.embedding_dimensions
            )
            for query in query_set.queries
        }
    )
    search_service = SearchService(database)
    recall_source = PersistentVectorRecallSource(
        query_embedding=provider,
        embeddings=database,
        fingerprints=SearchableContentFingerprintSource(database),
        model=corpus.embedding_model,
        dimensions=corpus.embedding_dimensions,
        config_version=corpus.embedding_config_version,
    )
    production = HybridSearchService(
        lexical=search_service, hydration=database, vector=recall_source
    )
    variant = abl.VariantHybridSearchService(
        lexical=search_service, hydration=database, vector=recall_source
    )
    return query_set, production, variant


def test_variant_service_defaults_equal_production(fixture_services: tuple) -> None:
    """默认参数下变体服务与生产服务在全部 29 条 query 上排名/状态完全一致。"""

    query_set, production, variant = fixture_services
    assert len(query_set.queries) == 29
    for query in query_set.queries:
        expected = production.search(query.text, limit=bench.SEARCH_LIMIT)
        actual = variant.search(query.text, limit=bench.SEARCH_LIMIT)
        assert actual.vector_status == expected.vector_status, query.query_id
        assert actual.invalid_vector_candidates == expected.invalid_vector_candidates
        expected_shape = [
            (hit.result.page_id, hit.fused_score, hit.lexical_rank, hit.vector_rank)
            for hit in expected.results
        ]
        actual_shape = [
            (hit.result.page_id, hit.fused_score, hit.lexical_rank, hit.vector_rank)
            for hit in actual.results
        ]
        assert actual_shape == expected_shape, query.query_id


# ---------------------------------------------------------------------------
# V0 控制组：复现 Phase 1D 已提交的 baseline 聚合（harness 回归检查）
# ---------------------------------------------------------------------------


def test_v0_reproduces_phase1d_baseline(report: abl.AblationReport) -> None:
    """V0 必须逐位复现 Phase 1D 提交的 keyword/hybrid 聚合与 delta 分布。"""

    keyword = report.keyword
    assert keyword.query_count == 26
    assert keyword.hit_at_1 == pytest.approx(0.8846, abs=1e-4)
    assert keyword.hit_at_3 == pytest.approx(0.9231, abs=1e-4)
    assert keyword.mrr == pytest.approx(0.9183, abs=1e-4)

    v0 = report.variant("V0")
    assert v0.query_count == 26
    assert v0.hit_at_1 == pytest.approx(0.7308, abs=1e-4)
    assert v0.hit_at_3 == pytest.approx(0.8462, abs=1e-4)
    assert v0.mrr == pytest.approx(0.8135, abs=1e-4)
    assert dict(v0.delta_counts) == {
        "improved": 2,
        "unchanged": 17,
        "regression": 7,
        "hybrid-only recall": 0,
        "both-miss": 0,
    }


def test_variant_matrix_complete(report: abl.AblationReport) -> None:
    """11 个变体全部运行，每个变体覆盖全部 29 条 query。"""

    assert [summary.spec.name for summary in report.variants] == [
        "V0",
        "A1",
        "B3",
        "B5",
        "B10",
        "C1-w0.5",
        "C1-w0.25",
        "C2",
        "C3",
        "D1",
        "D2",
    ]
    for summary in report.variants:
        assert len(summary.records) == 29
        assert summary.query_count == 26


# ---------------------------------------------------------------------------
# 运行确定性
# ---------------------------------------------------------------------------


def test_ablation_is_deterministic(report: abl.AblationReport) -> None:
    """同一冻结输入连跑两次：每个变体的逐 query 排名证据逐字段一致。"""

    second = abl.run_ablation(PROJECT_ROOT)
    for first, followup in zip(report.variants, second.variants, strict=True):
        assert first.records == followup.records, f"{first.spec.name} 逐 query 记录不确定"
    assert abl.report_to_dict(report) == abl.report_to_dict(second)
