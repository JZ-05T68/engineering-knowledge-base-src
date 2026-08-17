"""离线检索消融 runner（v0.5.1 Phase 2，受控 ablation）。

在 ``scripts/retrieval_benchmark.py`` 的冻结合同（同一语料、同一 query 集、
同一 fixture 向量、同一评分口径）之上，对 Phase 1 暴露的两个结构性缺陷做
**离线受控消融**：

- D-01：partial embedding coverage 下，无阈值向量召回 + RRF 双来源累加
  系统性压低未嵌入的 keyword 相关页（7 条 regression）；
- D-03：hybrid 结果数膨胀（无最终 top-k 上限）。

硬边界（与 benchmark 合同 §6 一致）：

- 0 HTTP、0 Qwen、0 LLM、0 rerank、0 真实 page embedding；
- 不修改 ``src/`` / ``pages/`` 任何生产代码，不修改冻结基准文件；
- fixture 向量只写入 runner 的临时目录 DB，绝不触碰 ``data/`` 或 ``staging-data/``；
- 输出一律标注为 **algorithmic / offline evidence**；
- 不设 PASS/FAIL 阈值，只报告分布。

确定性：同一冻结输入连跑两次，JSON / Markdown 输出逐字节一致；
报告正文不含任何时间戳。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.retrieval_benchmark import (  # noqa: E402
    CROSS_DOCUMENT_CATEGORY,
    DELTA_LABELS,
    NEGATIVE_CATEGORY,
    SEARCH_LIMIT,
    FixtureEmbeddingProvider,
    ModeAggregate,
    QuerySpec,
    _build_database,
    _write_fixture_embeddings,
    best_rank,
    coverage_at_5,
    cross_document_covered,
    delta_label,
    fixture_vector,
    hit_at,
    load_corpus,
    load_queries,
    reciprocal_rank,
)
from src.ai.hybrid_search import (  # noqa: E402
    HybridSearchOutcome,
    HybridSearchResult,
    HybridSearchService,
    LexicalSearch,
    PageHydrationSource,
    VectorPathStatus,
    _search_result_from_page,
    lexical_ranked_hits,
)
from src.ai.provider import AIError, AIUnavailableError  # noqa: E402
from src.ai.retrieval import DEFAULT_RRF_K, HybridHit, RankedHit, rrf_fuse  # noqa: E402
from src.ai.vector_recall import (  # noqa: E402
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
)
from src.search_service import SearchService  # noqa: E402

LOGGER = logging.getLogger(__name__)

#: Phase 1E 冻结的 7 条 regression query（顺序固定，用于稳定输出）。
PHASE1_REGRESSIONS: Final[tuple[str, ...]] = ("A4", "B4", "C3", "E1", "I2", "J1", "J2")

#: Phase 1D 确认的 3 条 hybrid 语义收益 query。
SEMANTIC_GAIN_QUERIES: Final[tuple[str, ...]] = ("C2", "H2", "D3")

#: F 类 negative query（结果数 + Top5 语义召回暴露）。
F_QUERIES: Final[tuple[str, ...]] = ("F1", "F2", "F3")

#: D 类跨文档覆盖 query。
CROSS_DOC_QUERIES: Final[tuple[str, ...]] = ("D1", "D2", "D3")


# ---------------------------------------------------------------------------
# 变体机制 1：相似度门控 + Top-K 截断的向量召回包装器
# ---------------------------------------------------------------------------


class GatedVectorRecallSource:
    """相似度门控 + Top-K 截断的 ``VectorRecallSource`` 包装器。

    包裹一个 ``PersistentVectorRecallSource``，实现生产
    ``VectorRecallSource`` 协议（``recall(query, *, limit)``）：

    - 调用 ``inner.recall_scored(query, limit=limit)`` 拿到带原始相似度的候选；
    - ``min_similarity`` 非 ``None`` 时只保留 ``similarity > min_similarity``
      （严格大于）的候选；
    - ``top_k`` 非 ``None`` 时截断为前 ``top_k`` 个；
    - 重新编号 1..N 后返回 ``RankedHit``。

    A/B 变体把它插进**未修改的**生产 ``HybridSearchService`` 即可生效。
    """

    def __init__(
        self,
        inner: PersistentVectorRecallSource,
        *,
        min_similarity: float | None = None,
        top_k: int | None = None,
    ) -> None:
        if min_similarity is not None:
            if isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float)):
                raise ValueError(f"min_similarity 必须是数值：{min_similarity!r}")
            min_similarity = float(min_similarity)
            if not math.isfinite(min_similarity):
                raise ValueError(f"min_similarity 必须是有限数值：{min_similarity!r}")
        if top_k is not None and (
            isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0
        ):
            raise ValueError(f"top_k 必须为正整数：{top_k!r}")
        self._inner = inner
        self._min_similarity = min_similarity
        self._top_k = top_k

    def recall(self, query: str, *, limit: int) -> tuple[RankedHit, ...]:
        """返回门控 + 截断后重新编号的 Top-K 候选。"""

        scored = self._inner.recall_scored(query, limit=limit)
        if self._min_similarity is not None:
            scored = tuple(
                hit for hit in scored if hit.similarity > self._min_similarity
            )
        if self._top_k is not None:
            scored = scored[: self._top_k]
        return tuple(
            RankedHit(page_id=hit.page_id, rank=rank)
            for rank, hit in enumerate(scored, start=1)
        )


# ---------------------------------------------------------------------------
# 变体机制 2：参数化 RRF 融合核
# ---------------------------------------------------------------------------


def _validate_hits(hits: Sequence[RankedHit], *, source: str) -> None:
    """与 ``src/ai/retrieval.py`` 的 ``_validate_hits`` 语义一致的 fail-closed 校验。"""

    seen: set[int] = set()
    for hit in hits:
        if not isinstance(hit, RankedHit):
            raise ValueError(f"{source} 候选必须是 RankedHit：{type(hit).__name__}")
        if isinstance(hit.page_id, bool) or not isinstance(hit.page_id, int):
            raise ValueError(f"{source} 候选 page_id 必须是整数：{hit.page_id!r}")
        if hit.page_id < 1:
            raise ValueError(f"{source} 候选 page_id 必须大于 0：{hit.page_id}")
        if isinstance(hit.rank, bool) or not isinstance(hit.rank, int):
            raise ValueError(f"{source} 候选 rank 必须是整数：{hit.rank!r}")
        if hit.rank < 1:
            raise ValueError(f"{source} 候选 rank 必须从 1 开始：{hit.rank}")
        if hit.page_id in seen:
            raise ValueError(f"{source} 候选存在重复 page_id：{hit.page_id}")
        seen.add(hit.page_id)


def _require_finite_factor(value: float, name: str) -> float:
    """校验融合系数：非负有限数值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值：{value!r}")
    factor = float(value)
    if not math.isfinite(factor) or factor < 0:
        raise ValueError(f"{name} 必须是非负有限数值：{value!r}")
    return factor


def rrf_fuse_variant(
    lexical_hits: Sequence[RankedHit],
    vector_hits: Sequence[RankedHit],
    *,
    k: int = DEFAULT_RRF_K,
    vector_weight: float = 1.0,
    vector_only_penalty: float = 1.0,
    lexical_first_tiebreak: bool = False,
) -> tuple[HybridHit, ...]:
    """生产 ``rrf_fuse`` 的参数化变体（默认参数下与生产逐分位等价）。

    完整复刻生产语义（输入校验、按页累加、``HybridHit`` 输出），只允许
    以下三处受控改动：

    - 向量侧贡献乘以 ``vector_weight``；
    - 只出现在向量列表中的页面，其融合得分再乘以 ``vector_only_penalty``；
    - ``lexical_first_tiebreak`` 为真时，最终排序键变为
      ``(-fused_score, 0 if lexical_rank is not None else 1, best_rank, page_id)``，
      否则保持生产键 ``(-fused_score, best_rank, page_id)``。
    """

    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError(f"k 必须为正整数：{k!r}")
    vector_weight = _require_finite_factor(vector_weight, "vector_weight")
    vector_only_penalty = _require_finite_factor(vector_only_penalty, "vector_only_penalty")
    _validate_hits(lexical_hits, source="lexical")
    _validate_hits(vector_hits, source="vector")

    lexical_ranks = {hit.page_id: hit.rank for hit in lexical_hits}
    vector_ranks = {hit.page_id: hit.rank for hit in vector_hits}
    fused: list[HybridHit] = []
    for page_id in lexical_ranks.keys() | vector_ranks.keys():
        score = 0.0
        lexical_rank = lexical_ranks.get(page_id)
        vector_rank = vector_ranks.get(page_id)
        if lexical_rank is not None:
            score += 1.0 / (k + lexical_rank)
        if vector_rank is not None:
            score += vector_weight * (1.0 / (k + vector_rank))
        if lexical_rank is None and vector_rank is not None:
            score *= vector_only_penalty
        fused.append(
            HybridHit(
                page_id=page_id,
                fused_score=score,
                lexical_rank=lexical_rank,
                vector_rank=vector_rank,
            )
        )
    if lexical_first_tiebreak:
        return tuple(
            sorted(
                fused,
                key=lambda hit: (
                    -hit.fused_score,
                    0 if hit.lexical_rank is not None else 1,
                    hit.best_rank,
                    hit.page_id,
                ),
            )
        )
    return tuple(
        sorted(fused, key=lambda hit: (-hit.fused_score, hit.best_rank, hit.page_id))
    )


# ---------------------------------------------------------------------------
# 变体机制 3：融合核可替换的 hybrid 服务
# ---------------------------------------------------------------------------


class VariantHybridSearchService:
    """与生产 ``HybridSearchService`` 同流程、仅融合核替换为 ``rrf_fuse_variant``。

    ``search`` 逐行对齐生产实现（src/ai/hybrid_search.py:132-186）：lexical
    优先、相同的向量降级状态（DISABLED/OK/EMPTY/UNAVAILABLE/FAILED）、相同的
    向量候选单独校验、相同的 ``PageHydrationSource`` 水合与
    ``HybridSearchOutcome`` 形状；唯一差异是融合调用 ``rrf_fuse_variant``。
    """

    def __init__(
        self,
        *,
        lexical: LexicalSearch,
        hydration: PageHydrationSource,
        vector: Any = None,
        rrf_k: int = DEFAULT_RRF_K,
        vector_weight: float = 1.0,
        vector_only_penalty: float = 1.0,
        lexical_first_tiebreak: bool = False,
    ) -> None:
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
            raise ValueError(f"rrf_k 必须为正整数：{rrf_k!r}")
        self._lexical = lexical
        self._hydration = hydration
        self._vector = vector
        self._rrf_k = rrf_k
        self._vector_weight = vector_weight
        self._vector_only_penalty = vector_only_penalty
        self._lexical_first_tiebreak = lexical_first_tiebreak

    def search(self, query: str, *, limit: int = 20) -> HybridSearchOutcome:
        """与 src/ai/hybrid_search.py:132-186 同构；仅融合核替换。"""

        lexical_results = list(self._lexical.search(query, limit))
        lexical_hits = lexical_ranked_hits(lexical_results)
        lexical_by_id = {result.page_id: result for result in lexical_results}

        vector_hits: tuple[RankedHit, ...] = ()
        vector_status = VectorPathStatus.OK
        if self._vector is None:
            vector_status = VectorPathStatus.DISABLED
        else:
            try:
                vector_hits = tuple(self._vector.recall(query, limit=limit))
                if not vector_hits:
                    vector_status = VectorPathStatus.EMPTY
            except AIUnavailableError as exc:
                LOGGER.info("向量召回不可用，退化为纯词面检索：%s", exc)
                vector_status = VectorPathStatus.UNAVAILABLE
            except (AIError, ValueError) as exc:
                LOGGER.warning("向量召回失败或输出非法，退化为纯词面检索：%s", exc)
                vector_status = VectorPathStatus.FAILED
            except Exception:
                LOGGER.exception("向量召回发生未预期错误，退化为纯词面检索")
                vector_status = VectorPathStatus.FAILED

        if vector_hits:
            try:
                rrf_fuse((), vector_hits, k=self._rrf_k)
            except ValueError as exc:
                LOGGER.warning("向量候选未通过融合校验，退化为纯词面检索：%s", exc)
                vector_hits = ()
                vector_status = VectorPathStatus.FAILED

        fused = rrf_fuse_variant(
            lexical_hits,
            vector_hits,
            k=self._rrf_k,
            vector_weight=self._vector_weight,
            vector_only_penalty=self._vector_only_penalty,
            lexical_first_tiebreak=self._lexical_first_tiebreak,
        )

        results: list[HybridSearchResult] = []
        invalid_candidates = 0
        for hit in fused:
            hydrated = self._hydrate(hit, lexical_by_id)
            if hydrated is None:
                invalid_candidates += 1
                continue
            results.append(hydrated)
        return HybridSearchOutcome(
            results=tuple(results),
            vector_status=vector_status,
            invalid_vector_candidates=invalid_candidates,
        )

    def _hydrate(
        self, hit: HybridHit, lexical_by_id: dict[int, Any]
    ) -> HybridSearchResult | None:
        """与生产 ``HybridSearchService._hydrate`` 一致的水合逻辑。"""

        provenance = {
            "fused_score": hit.fused_score,
            "lexical_rank": hit.lexical_rank,
            "vector_rank": hit.vector_rank,
        }
        if hit.lexical_rank is not None:
            original = lexical_by_id.get(hit.page_id)
            if original is not None:
                return HybridSearchResult(result=original, **provenance)
        page = self._hydration.get_page(hit.page_id)
        if page is None:
            LOGGER.warning("跳过无法还原的向量候选：page_id=%s", hit.page_id)
            return None
        document = self._hydration.get_document(page.document_id)
        if document is None:
            LOGGER.warning(
                "跳过文档缺失的向量候选：page_id=%s document_id=%s",
                hit.page_id,
                page.document_id,
            )
            return None
        return HybridSearchResult(result=_search_result_from_page(page, document), **provenance)


# ---------------------------------------------------------------------------
# 变体矩阵
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """一个消融变体的参数组合（全部为可选改动的叠加）。"""

    name: str
    description: str
    min_similarity: float | None = None
    top_k: int | None = None
    vector_weight: float = 1.0
    vector_only_penalty: float = 1.0
    lexical_first_tiebreak: bool = False


VARIANTS: Final[tuple[VariantSpec, ...]] = (
    VariantSpec("V0", "基线：纯生产路径（无门控、无截断、生产 RRF）"),
    VariantSpec("A1", "相似度门控：min_similarity=0.0（剔除 sim≤0 候选）", min_similarity=0.0),
    VariantSpec("B3", "向量 Top-K 截断：top_k=3", top_k=3),
    VariantSpec("B5", "向量 Top-K 截断：top_k=5", top_k=5),
    VariantSpec("B10", "向量 Top-K 截断：top_k=10", top_k=10),
    VariantSpec("C1-w0.5", "RRF 向量权重 0.5（仅融合变体，召回不变）", vector_weight=0.5),
    VariantSpec("C1-w0.25", "RRF 向量权重 0.25（仅融合变体，召回不变）", vector_weight=0.25),
    VariantSpec(
        "C2", "RRF 词面优先 tie-break（平分时有词面 rank 者在前）", lexical_first_tiebreak=True
    ),
    VariantSpec("C3", "vector_only_penalty=0.5（纯向量候选得分减半）", vector_only_penalty=0.5),
    VariantSpec(
        "D1",
        "组合：A1 + top_k=5 + 词面优先 tie-break",
        min_similarity=0.0,
        top_k=5,
        lexical_first_tiebreak=True,
    ),
    VariantSpec(
        "D2",
        "组合：A1 + top_k=5 + vector_weight=0.5",
        min_similarity=0.0,
        top_k=5,
        vector_weight=0.5,
    ),
)


# ---------------------------------------------------------------------------
# 逐 query 记录与聚合
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantQueryRecord:
    """一个变体在一条 query 上的固定记录字段。"""

    query_id: str
    category: str
    keyword_best: int | None
    hybrid_best: int | None
    delta: str | None
    hit_at_1: bool | None
    hit_at_3: bool | None
    reciprocal_rank: float | None
    coverage_at_5: float | None
    result_count: int
    semantic_recall_in_top5: bool | None
    cross_document_top5: bool | None


@dataclass(frozen=True, slots=True)
class VariantSummary:
    """一个变体的聚合证据（n=26 非 F 类；结果数分布覆盖全部 29 条）。"""

    spec: VariantSpec
    records: tuple[VariantQueryRecord, ...]
    query_count: int
    hit_at_1: float
    hit_at_3: float
    mrr: float
    delta_counts: Mapping[str, int]
    cross_document_top5: Mapping[str, bool]
    semantic_gain: Mapping[str, str]
    regression_dispositions: Mapping[str, str]
    f_semantic_top5: Mapping[str, bool]
    result_count_min: int
    result_count_median: float
    result_count_max: int


@dataclass(frozen=True, slots=True)
class AblationReport:
    """一次完整消融运行的全部证据（确定性、无时间戳）。"""

    corpus_id: str
    query_set_id: str
    total_pages: int
    embedded_pages: int
    keyword: ModeAggregate
    keyword_best: Mapping[str, int | None]
    variants: tuple[VariantSummary, ...]

    def variant(self, name: str) -> VariantSummary:
        """按名字取变体聚合。"""

        for summary in self.variants:
            if summary.spec.name == name:
                return summary
        raise KeyError(f"未知变体：{name!r}")


def _regression_disposition(
    keyword_best: int | None, v0_hybrid_best: int | None, hybrid_best: int | None
) -> str:
    """单条 Phase 1 regression 在变体下的处置标签。

    - ``fixed``：hybrid best_rank ≤ keyword best_rank；
    - ``improved-not-fixed``：优于 V0 hybrid 但仍差于 keyword；
    - ``unchanged``：与 V0 hybrid 相同；``worse``：比 V0 hybrid 更差。
    """

    if hybrid_best is not None and keyword_best is not None and hybrid_best <= keyword_best:
        return "fixed"
    absent = math.inf
    current = hybrid_best if hybrid_best is not None else absent
    baseline = v0_hybrid_best if v0_hybrid_best is not None else absent
    if current < baseline:
        return "improved-not-fixed"
    if current == baseline:
        return "unchanged"
    return "worse"


def _summarize_variant(
    spec: VariantSpec,
    records: tuple[VariantQueryRecord, ...],
    v0_records: tuple[VariantQueryRecord, ...],
) -> VariantSummary:
    """聚合一个变体的全部指标与处置标签。"""

    scored = [record for record in records if record.delta is not None]
    count = len(scored)
    by_id = {record.query_id: record for record in records}
    v0_best = {record.query_id: record.hybrid_best for record in v0_records}

    delta_counts = {label: 0 for label in DELTA_LABELS}
    for record in scored:
        assert record.delta is not None
        delta_counts[record.delta] += 1

    cross_document = {
        query_id: bool(by_id[query_id].cross_document_top5) for query_id in CROSS_DOC_QUERIES
    }
    semantic_gain: dict[str, str] = {}
    for query_id in SEMANTIC_GAIN_QUERIES:
        record = by_id[query_id]
        preserved = (
            record.hybrid_best is not None
            and record.keyword_best is not None
            and record.hybrid_best <= record.keyword_best
            and record.delta in ("improved", "unchanged")
        )
        semantic_gain[query_id] = "preserved" if preserved else "lost"
    dispositions = {
        query_id: _regression_disposition(
            by_id[query_id].keyword_best, v0_best[query_id], by_id[query_id].hybrid_best
        )
        for query_id in PHASE1_REGRESSIONS
    }
    f_flags = {
        query_id: bool(by_id[query_id].semantic_recall_in_top5) for query_id in F_QUERIES
    }
    counts = [record.result_count for record in records]
    return VariantSummary(
        spec=spec,
        records=records,
        query_count=count,
        hit_at_1=sum(1.0 for record in scored if record.hit_at_1) / count,
        hit_at_3=sum(1.0 for record in scored if record.hit_at_3) / count,
        mrr=sum(record.reciprocal_rank or 0.0 for record in scored) / count,
        delta_counts=delta_counts,
        cross_document_top5=cross_document,
        semantic_gain=semantic_gain,
        regression_dispositions=dispositions,
        f_semantic_top5=f_flags,
        result_count_min=min(counts),
        result_count_median=float(statistics.median(counts)),
        result_count_max=max(counts),
    )


def _build_variant_service(
    spec: VariantSpec,
    *,
    lexical: SearchService,
    hydration: Any,
    recall_source: PersistentVectorRecallSource,
) -> HybridSearchService | VariantHybridSearchService:
    """按变体参数组装服务：A/B 走生产服务 + 门控召回；C/D 换融合变体服务。"""

    vector: Any = recall_source
    if spec.min_similarity is not None or spec.top_k is not None:
        vector = GatedVectorRecallSource(
            recall_source, min_similarity=spec.min_similarity, top_k=spec.top_k
        )
    fuse_changed = (
        spec.vector_weight != 1.0
        or spec.vector_only_penalty != 1.0
        or spec.lexical_first_tiebreak
    )
    if fuse_changed:
        return VariantHybridSearchService(
            lexical=lexical,
            hydration=hydration,
            vector=vector,
            vector_weight=spec.vector_weight,
            vector_only_penalty=spec.vector_only_penalty,
            lexical_first_tiebreak=spec.lexical_first_tiebreak,
        )
    return HybridSearchService(lexical=lexical, hydration=hydration, vector=vector)


def _run_variant_query(
    query: QuerySpec,
    outcome: HybridSearchOutcome,
    keyword_best: int | None,
    page_keys_by_id: Mapping[int, str],
    document_of: Mapping[str, str],
) -> VariantQueryRecord:
    """对一条冻结 query 记录一个变体的指标（口径与 Phase 1C 合同一致）。"""

    ranking = tuple(page_keys_by_id[hit.result.page_id] for hit in outcome.results)
    has_relevant = bool(query.relevant_pages)
    hybrid_best = best_rank(ranking, query.relevant_pages) if has_relevant else None
    return VariantQueryRecord(
        query_id=query.query_id,
        category=query.category,
        keyword_best=keyword_best,
        hybrid_best=hybrid_best,
        delta=delta_label(keyword_best, hybrid_best) if has_relevant else None,
        hit_at_1=hit_at(hybrid_best, 1) if has_relevant else None,
        hit_at_3=hit_at(hybrid_best, 3) if has_relevant else None,
        reciprocal_rank=reciprocal_rank(hybrid_best) if has_relevant else None,
        coverage_at_5=(
            coverage_at_5(ranking, query.relevant_pages) if has_relevant else None
        ),
        result_count=len(outcome.results),
        semantic_recall_in_top5=(
            any(
                hit.lexical_rank is None and hit.vector_rank is not None
                for hit in outcome.results[:5]
            )
            if query.category == NEGATIVE_CATEGORY
            else None
        ),
        cross_document_top5=(
            cross_document_covered(ranking, query.relevant_pages, document_of)
            if query.category == CROSS_DOCUMENT_CATEGORY and has_relevant
            else None
        ),
    )


def run_ablation(root: Path) -> AblationReport:
    """执行完整离线消融：建库 → keyword 基线 → 11 个变体 × 29 条 query。

    只读 ``root/benchmarks`` 下的两个冻结 JSON；数据库建在临时目录，
    随函数返回自动清理。同一输入多次调用返回完全一致的报告。
    """

    corpus = load_corpus(root / "benchmarks" / "corpus_synthetic_v1.json")
    query_set = load_queries(root / "benchmarks" / "queries_v1.json", corpus)
    with tempfile.TemporaryDirectory(prefix="ekb-retrieval-ablation-") as tmp:
        work_dir = Path(tmp)
        database, page_ids, document_of = _build_database(corpus, work_dir)
        embedded_pages = _write_fixture_embeddings(corpus, database, page_ids)
        page_keys_by_id = {page_id: key for key, page_id in page_ids.items()}

        provider = FixtureEmbeddingProvider(
            {
                query.text: fixture_vector(
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

        # keyword 是变体不变式，只跑一次。
        keyword_rankings: dict[str, tuple[str, ...]] = {}
        keyword_best: dict[str, int | None] = {}
        for query in query_set.queries:
            results = search_service.search(query.text, limit=SEARCH_LIMIT)
            ranking = tuple(page_keys_by_id[result.page_id] for result in results)
            keyword_rankings[query.query_id] = ranking
            keyword_best[query.query_id] = (
                best_rank(ranking, query.relevant_pages) if query.relevant_pages else None
            )

        scored_queries = [query for query in query_set.queries if query.relevant_pages]
        keyword_aggregate = ModeAggregate(
            query_count=len(scored_queries),
            hit_at_1=(
                sum(
                    1.0
                    for query in scored_queries
                    if hit_at(keyword_best[query.query_id], 1)
                )
                / len(scored_queries)
            ),
            hit_at_3=(
                sum(
                    1.0
                    for query in scored_queries
                    if hit_at(keyword_best[query.query_id], 3)
                )
                / len(scored_queries)
            ),
            mrr=(
                sum(
                    reciprocal_rank(keyword_best[query.query_id])
                    for query in scored_queries
                )
                / len(scored_queries)
            ),
        )

        all_records: dict[str, tuple[VariantQueryRecord, ...]] = {}
        for spec in VARIANTS:
            service = _build_variant_service(
                spec,
                lexical=search_service,
                hydration=database,
                recall_source=recall_source,
            )
            all_records[spec.name] = tuple(
                _run_variant_query(
                    query,
                    service.search(query.text, limit=SEARCH_LIMIT),
                    keyword_best[query.query_id],
                    page_keys_by_id,
                    document_of,
                )
                for query in query_set.queries
            )

    v0_records = all_records["V0"]
    summaries = tuple(
        _summarize_variant(spec, all_records[spec.name], v0_records) for spec in VARIANTS
    )
    return AblationReport(
        corpus_id=corpus.corpus_id,
        query_set_id=query_set.query_set_id,
        total_pages=len(corpus.page_keys),
        embedded_pages=embedded_pages,
        keyword=keyword_aggregate,
        keyword_best=keyword_best,
        variants=summaries,
    )


# ---------------------------------------------------------------------------
# 报告输出（确定性：无时间戳、排序固定）
# ---------------------------------------------------------------------------


def _format_float(value: float) -> str:
    """格式化浮点指标（4 位小数）。"""

    return f"{value:.4f}"


def _format_best(best: int | None, has_relevant: bool) -> str:
    """格式化 best_rank：无 relevant 记 n/a，缺席记 ABSENT。"""

    if not has_relevant:
        return "n/a"
    return "ABSENT" if best is None else str(best)


def _format_median(value: float) -> str:
    """中位数：整数值去掉小数点。"""

    return str(int(value)) if value == int(value) else f"{value:.1f}"


def report_to_dict(report: AblationReport) -> dict[str, Any]:
    """报告的 JSON 可序列化形式（``json.dumps(sort_keys=True)`` 下确定）。"""

    return {
        "evidence_level": "algorithmic/offline evidence（非真实模型语义质量证据）",
        "corpus_id": report.corpus_id,
        "query_set_id": report.query_set_id,
        "embedding": {
            "embedded_pages": report.embedded_pages,
            "total_pages": report.total_pages,
        },
        "keyword": {
            "query_count": report.keyword.query_count,
            "hit_at_1": report.keyword.hit_at_1,
            "hit_at_3": report.keyword.hit_at_3,
            "mrr": report.keyword.mrr,
        },
        "variants": {
            summary.spec.name: {
                "description": summary.spec.description,
                "parameters": {
                    "min_similarity": summary.spec.min_similarity,
                    "top_k": summary.spec.top_k,
                    "vector_weight": summary.spec.vector_weight,
                    "vector_only_penalty": summary.spec.vector_only_penalty,
                    "lexical_first_tiebreak": summary.spec.lexical_first_tiebreak,
                },
                "aggregate": {
                    "query_count": summary.query_count,
                    "hit_at_1": summary.hit_at_1,
                    "hit_at_3": summary.hit_at_3,
                    "mrr": summary.mrr,
                    "delta_counts": dict(summary.delta_counts),
                    "cross_document_top5": dict(summary.cross_document_top5),
                    "semantic_gain": dict(summary.semantic_gain),
                    "regression_dispositions": dict(summary.regression_dispositions),
                    "f_semantic_top5": dict(summary.f_semantic_top5),
                    "result_count_min": summary.result_count_min,
                    "result_count_median": summary.result_count_median,
                    "result_count_max": summary.result_count_max,
                },
                "queries": [
                    {
                        "id": record.query_id,
                        "category": record.category,
                        "keyword_best": record.keyword_best,
                        "hybrid_best": record.hybrid_best,
                        "delta": record.delta,
                        "hit_at_1": record.hit_at_1,
                        "hit_at_3": record.hit_at_3,
                        "reciprocal_rank": record.reciprocal_rank,
                        "coverage_at_5": record.coverage_at_5,
                        "result_count": record.result_count,
                        "semantic_recall_in_top5": record.semantic_recall_in_top5,
                        "cross_document_top5": record.cross_document_top5,
                    }
                    for record in summary.records
                ],
            }
            for summary in report.variants
        },
    }


def report_to_markdown(report: AblationReport) -> str:
    """渲染 Markdown 报告（正文无时间戳，输出确定）。"""

    keyword = report.keyword
    lines: list[str] = [
        "# v0.5.1 检索机制消融报告（离线 ablation）",
        "",
        "> **algorithmic / offline evidence**：本报告由 `scripts/retrieval_ablation.py`",
        "> 基于冻结合成语料与确定性 latent-topic fixture 向量生成，只验证",
        "> 向量门控 / Top-K 截断 / RRF 参数化变体对 Phase 1 缺陷（D-01/D-03）的",
        "> 机制性影响，**不构成真实模型（Qwen）语义质量证据**。",
        "",
        f"- 语料：`{report.corpus_id}`（{report.total_pages} 页）",
        f"- Query 集：`{report.query_set_id}`（29 条，A–J；指标 n=26 非 F 类）",
        f"- Embedding 覆盖：{report.embedded_pages}/{report.total_pages} 页",
        f"- keyword（变体不变式）：Hit@1={_format_float(keyword.hit_at_1 or 0.0)} "
        f"Hit@3={_format_float(keyword.hit_at_3 or 0.0)} "
        f"MRR={_format_float(keyword.mrr or 0.0)}（n={keyword.query_count}）",
    ]
    for summary in report.variants:
        lines.append("")
        lines.append(f"## 变体 {summary.spec.name} — {summary.spec.description}")
        lines.append("")
        lines.append(
            "| Hit@1 | Hit@3 | MRR | improved | unchanged | regression | "
            "hybrid-only recall | both-miss | 结果数 min/median/max |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        counts = summary.delta_counts
        lines.append(
            f"| {_format_float(summary.hit_at_1)} | {_format_float(summary.hit_at_3)} | "
            f"{_format_float(summary.mrr)} | {counts['improved']} | {counts['unchanged']} | "
            f"{counts['regression']} | {counts['hybrid-only recall']} | "
            f"{counts['both-miss']} | {summary.result_count_min}/"
            f"{_format_median(summary.result_count_median)}/{summary.result_count_max} |"
        )
        lines.append("")
        dispositions = "、".join(
            f"{query_id}={label}"
            for query_id, label in summary.regression_dispositions.items()
        )
        gains = "、".join(
            f"{query_id}={label}" for query_id, label in summary.semantic_gain.items()
        )
        cross = "、".join(
            f"{query_id}={covered}"
            for query_id, covered in summary.cross_document_top5.items()
        )
        f_flags = "、".join(
            f"{query_id}={flag}" for query_id, flag in summary.f_semantic_top5.items()
        )
        lines.append(f"- Phase 1 七条 regression 处置：{dispositions}")
        lines.append(f"- 语义收益（C2/H2/D3）：{gains}")
        lines.append(f"- 跨文档 Top5 覆盖（D1/D2/D3 ≥2 文档）：{cross}")
        lines.append(f"- F 类 Top5 出现语义召回：{f_flags}")
        lines.append("")
        lines.append("| query | K best | H best | delta | 结果数 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for record in summary.records:
            has_relevant = record.delta is not None
            lines.append(
                f"| {record.query_id} | {_format_best(record.keyword_best, has_relevant)} | "
                f"{_format_best(record.hybrid_best, has_relevant)} | "
                f"{record.delta or '—'} | {record.result_count} |"
            )
    lines.append("")
    lines.append("## 变体对比总表")
    lines.append("")
    lines.append(
        "| 变体 | Hit@1 | Hit@3 | MRR | improved | unchanged | regression | "
        "结果数 min/median/max | F1 语义Top5 | F2 语义Top5 | F3 语义Top5 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for summary in report.variants:
        counts = summary.delta_counts
        lines.append(
            f"| {summary.spec.name} | {_format_float(summary.hit_at_1)} | "
            f"{_format_float(summary.hit_at_3)} | {_format_float(summary.mrr)} | "
            f"{counts['improved']} | {counts['unchanged']} | {counts['regression']} | "
            f"{summary.result_count_min}/{_format_median(summary.result_count_median)}/"
            f"{summary.result_count_max} | {summary.f_semantic_top5['F1']} | "
            f"{summary.f_semantic_top5['F2']} | {summary.f_semantic_top5['F3']} |"
        )
    lines.append("")
    return "\n".join(lines)


def summary_lines(report: AblationReport) -> list[str]:
    """stdout 紧凑摘要：keyword 不变式一行 + 每个变体一行。"""

    keyword = report.keyword
    lines = [
        f"语料={report.corpus_id} query集={report.query_set_id} "
        f"embedding覆盖={report.embedded_pages}/{report.total_pages} 变体数={len(report.variants)}",
        f"keyword（不变式）: Hit@1={_format_float(keyword.hit_at_1 or 0.0)} "
        f"Hit@3={_format_float(keyword.hit_at_3 or 0.0)} "
        f"MRR={_format_float(keyword.mrr or 0.0)} (n={keyword.query_count})",
    ]
    for summary in report.variants:
        counts = summary.delta_counts
        f_flags = " ".join(
            f"{query_id}={'T' if flag else 'F'}"
            for query_id, flag in summary.f_semantic_top5.items()
        )
        lines.append(
            f"{summary.spec.name:<9} Hit@1={_format_float(summary.hit_at_1)} "
            f"Hit@3={_format_float(summary.hit_at_3)} MRR={_format_float(summary.mrr)} | "
            f"imp={counts['improved']} same={counts['unchanged']} "
            f"reg={counts['regression']} | "
            f"cnt {summary.result_count_min}/{_format_median(summary.result_count_median)}/"
            f"{summary.result_count_max} | F语义Top5 {f_flags}"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：运行消融，按参数输出 JSON / Markdown，并打印 stdout 摘要。"""

    parser = argparse.ArgumentParser(
        description="v0.5.1 离线检索机制消融（algorithmic/offline evidence）"
    )
    parser.add_argument("--json-out", type=Path, default=None, help="JSON 报告输出路径")
    parser.add_argument("--md-out", type=Path, default=None, help="Markdown 报告输出路径")
    args = parser.parse_args(argv)

    report = run_ablation(PROJECT_ROOT)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, sort_keys=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
        print(f"JSON 报告已写入：{args.json_out}")
    if args.md_out is not None:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(report_to_markdown(report), encoding="utf-8")
        print(f"Markdown 报告已写入：{args.md_out}")
    for line in summary_lines(report):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
