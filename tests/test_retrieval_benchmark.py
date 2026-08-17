"""检索质量 benchmark harness 的机制回归测试（v0.5.1 Phase 1B）。

只锁机制：冻结输入完整性、fixture 隔离、运行确定性、指标函数口径、
fake provider 调用计数、lexical 路径真实执行。**不断言任何
Hit@1 / MRR 质量阈值**（阈值等 baseline 证据出来后再由人工决定）。
"""

from __future__ import annotations

import pytest

import scripts.retrieval_benchmark as bench
from src.ai.page_indexer import EMBEDDING_DIMENSIONS

PROJECT_ROOT = bench.PROJECT_ROOT
FOREIGN_TOPICS = ("foreign_cloud", "foreign_battery", "foreign_ros", "reserve")


@pytest.fixture(scope="module")
def corpus() -> bench.CorpusFixture:
    """加载冻结语料。"""

    return bench.load_corpus(PROJECT_ROOT / "benchmarks" / "corpus_synthetic_v1.json")


@pytest.fixture(scope="module")
def query_set(corpus: bench.CorpusFixture) -> bench.QuerySet:
    """加载冻结 query 集。"""

    return bench.load_queries(PROJECT_ROOT / "benchmarks" / "queries_v1.json", corpus)


@pytest.fixture(scope="module")
def report() -> bench.BenchmarkReport:
    """执行一次完整 benchmark（模块级共享，避免重复建库）。"""

    return bench.run_benchmark(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 冻结输入完整性
# ---------------------------------------------------------------------------


def test_corpus_structure(corpus: bench.CorpusFixture) -> None:
    """语料基本结构：5 文档 / 42 页 / 恰好 21 页嵌入。"""

    assert len(corpus.documents) == 5
    assert len(corpus.page_keys) == 42
    embedded = sum(
        1 for document in corpus.documents for page in document.pages if page.embedded
    )
    assert embedded == 21
    assert len(corpus.topics) == corpus.embedding_dimensions


def test_query_set_structure(query_set: bench.QuerySet) -> None:
    """query 集基本结构：29 条、类别覆盖 A–J、F 类 relevant 为空。"""

    assert len(query_set.queries) == 29
    categories = {query.category for query in query_set.queries}
    assert categories == set("ABCDEFGHIJ")
    for query in query_set.queries:
        if query.category == "F":
            assert query.relevant_pages == ()


def test_page_references_exist(
    corpus: bench.CorpusFixture, query_set: bench.QuerySet
) -> None:
    """每条 query 的 relevant/distractor key 都存在于语料。"""

    known = set(corpus.page_keys)
    for query in query_set.queries:
        for key in (*query.relevant_pages, *query.distractor_pages):
            assert key in known, f"{query.query_id} 引用了不存在的页面 {key}"


def test_topics_are_known_and_nonzero(
    corpus: bench.CorpusFixture, query_set: bench.QuerySet
) -> None:
    """页面与 query 的 topic 都在语料 topics 中；每条 query 向量为非零向量。"""

    known_topics = set(corpus.topics)
    for document in corpus.documents:
        for page in document.pages:
            assert set(page.topics) <= known_topics
    for query in query_set.queries:
        assert set(query.topics) <= known_topics
        assert query.topics, f"{query.query_id} 的 topics 为空"
        vector = bench.fixture_vector(
            query.topics, corpus.topics, corpus.embedding_dimensions
        )
        assert any(component != 0.0 for component in vector), (
            f"{query.query_id} 的 fixture 向量是零向量"
        )


def test_foreign_topics_unused_by_pages(corpus: bench.CorpusFixture) -> None:
    """foreign/reserve topic 不被任何页面使用（专供 F 类 negative query）。"""

    for document in corpus.documents:
        for page in document.pages:
            assert not (set(page.topics) & set(FOREIGN_TOPICS)), (
                f"页面 {page.key} 不应使用 foreign/reserve topic"
            )


# ---------------------------------------------------------------------------
# fixture 隔离三元组
# ---------------------------------------------------------------------------


def test_fixture_isolation_from_production(corpus: bench.CorpusFixture) -> None:
    """fixture embedding 三元组必须不同于生产常量，保证 SQL 层天然隔离。"""

    # 生产维度常量直接从其唯一来源 src/ai/page_indexer.py 导入；
    # 生产模型名是 pydantic 字段默认值（src/config.py 的 ai_embedding_model），
    # 通过 model_fields 读取，避免实例化 Settings 时受环境变量干扰。
    from src.config import Settings

    production_model = Settings.model_fields["ai_embedding_model"].default
    assert production_model == "qwen3.7-text-embedding"
    assert corpus.embedding_model != production_model
    assert corpus.embedding_dimensions != EMBEDDING_DIMENSIONS


# ---------------------------------------------------------------------------
# 运行确定性
# ---------------------------------------------------------------------------


def test_run_is_deterministic(report: bench.BenchmarkReport) -> None:
    """同一冻结输入连跑两次：keyword/hybrid 排序与整份报告逐字段一致。"""

    second = bench.run_benchmark(PROJECT_ROOT)
    assert len(report.queries) == 29
    for first_record, second_record in zip(report.queries, second.queries, strict=True):
        assert first_record.keyword_ranking == second_record.keyword_ranking, (
            f"{first_record.query_id} keyword 排序不确定"
        )
        assert first_record.hybrid_ranking == second_record.hybrid_ranking, (
            f"{first_record.query_id} hybrid 排序不确定"
        )
    assert bench.report_to_dict(report) == bench.report_to_dict(second)


# ---------------------------------------------------------------------------
# 指标函数口径（合同 §3）
# ---------------------------------------------------------------------------


def test_best_rank_and_absent() -> None:
    """best_rank：最小 1-based 位置；全部缺席为 None。"""

    assert bench.best_rank(["a", "b", "c"], ("c",)) == 3
    assert bench.best_rank(["a", "b", "c"], ("c", "b")) == 2
    assert bench.best_rank(["a", "b"], ("x",)) is None
    assert bench.best_rank([], ("x",)) is None


def test_reciprocal_rank_and_hits() -> None:
    """RR 与 Hit@k，含 ABSENT 处理。"""

    assert bench.reciprocal_rank(1) == 1.0
    assert bench.reciprocal_rank(3) == pytest.approx(1 / 3)
    assert bench.reciprocal_rank(None) == 0.0
    assert bench.hit_at(1, 1) is True
    assert bench.hit_at(2, 1) is False
    assert bench.hit_at(3, 3) is True
    assert bench.hit_at(4, 3) is False
    assert bench.hit_at(None, 3) is False


def test_delta_label_all_cases() -> None:
    """delta 标签的五种情形，含 both-miss 与 hybrid-only recall。"""

    assert bench.delta_label(5, 2) == "improved"
    assert bench.delta_label(3, 3) == "unchanged"
    assert bench.delta_label(1, 4) == "regression"
    assert bench.delta_label(None, 2) == "hybrid-only recall"
    assert bench.delta_label(None, None) == "both-miss"
    # keyword 命中而 hybrid ABSENT 是 regression 的极端情形
    assert bench.delta_label(2, None) == "regression"


def test_coverage_at_5() -> None:
    """Coverage@5：Top 5 中不同 relevant 页数 / |relevant|。"""

    assert bench.coverage_at_5(["a", "b", "c"], ("a", "b", "c")) == 1.0
    assert bench.coverage_at_5(["a", "x", "y", "z", "b", "c"], ("a", "b", "c")) == pytest.approx(
        2 / 3
    )
    assert bench.coverage_at_5(["x"], ("a",)) == 0.0
    with pytest.raises(ValueError, match="Coverage@5"):
        bench.coverage_at_5(["a"], ())


def test_cross_document_covered() -> None:
    """D 类跨文档覆盖：Top 5 中 relevant 页覆盖 ≥ 2 个文档。"""

    document_of = {"a/p1": "a", "a/p2": "a", "b/p1": "b"}
    assert bench.cross_document_covered(["a/p1", "b/p1"], ("a/p1", "b/p1"), document_of)
    assert not bench.cross_document_covered(["a/p1", "a/p2"], ("a/p1", "a/p2"), document_of)
    assert not bench.cross_document_covered(["x", "b/p1"], ("a/p1", "b/p1"), document_of)


# ---------------------------------------------------------------------------
# fake provider 调用计数（成本维度证据）
# ---------------------------------------------------------------------------


def test_fake_provider_call_count(
    report: bench.BenchmarkReport, query_set: bench.QuerySet
) -> None:
    """每条 hybrid query 恰好 1 次 query embedding；调用文本全部来自冻结 query。"""

    frozen_texts = {query.text for query in query_set.queries}
    assert len(report.embed_calls) == len(query_set.queries)
    for call in report.embed_calls:
        assert len(call.texts) == 1
        assert call.texts[0] in frozen_texts
    for record in report.queries:
        assert record.embed_call_count == 1, f"{record.query_id} embedding 调用次数异常"


def test_fake_provider_rejects_unknown_text() -> None:
    """fake provider 对未知文本 fail-closed（AssertionError）。"""

    provider = bench.FixtureEmbeddingProvider({"已知问题": (1.0, 0.0)})
    with pytest.raises(AssertionError, match="未知 query 文本"):
        provider.embed(("没注册过的问题",), model="synthetic-fixture-v1", dimensions=2)


# ---------------------------------------------------------------------------
# lexical 路径真实执行（机制检查，非质量门槛）
# ---------------------------------------------------------------------------


def test_keyword_path_is_real(report: bench.BenchmarkReport) -> None:
    """A1 的 keyword Top 10 必须包含 stm32/p4：证明 FTS+LIKE 真的跑了。"""

    record = next(item for item in report.queries if item.query_id == "A1")
    assert "stm32/p4" in record.keyword_ranking[:10]


def test_vector_path_ran(report: bench.BenchmarkReport) -> None:
    """hybrid 的向量侧真实执行：vector_status 全部为 ok、无非法候选。"""

    for record in report.queries:
        assert record.vector_status == "ok", f"{record.query_id} vector_status 异常"
        assert record.invalid_vector_candidates == 0
