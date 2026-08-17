"""离线检索质量 benchmark runner（v0.5.1 Phase 1B/1C 冻结合同）。

基于冻结合成语料（``benchmarks/corpus_synthetic_v1.json``）与冻结 query 集
（``benchmarks/queries_v1.json``），在临时目录 SQLite 中走**真实生产代码路径**
（``Database`` / ``SearchService`` / ``PersistentVectorRecallSource`` /
``HybridSearchService``）执行 keyword 与 hybrid 双模式检索，按
``docs/v0.5.1-phase1c-evaluation-contract.md`` 的评分口径输出报告。

硬边界（与合同 §6 一致）：

- 0 HTTP、0 Qwen、0 LLM、0 rerank、0 真实 page embedding、无 retry（无 transport）；
- query 侧 embedding 由 ``FixtureEmbeddingProvider`` fake 提供，只认识冻结 query 文本；
- fixture 向量只写入 runner 的临时目录 DB，绝不触碰 ``data/`` 或 ``staging-data/``；
- 输出一律标注为 **algorithmic / offline evidence**，不代表真实模型语义质量；
- 不设任何 PASS/FAIL 阈值，只报告分布。

确定性：同一冻结输入连跑两次，报告内容（JSON / Markdown）逐字节一致；
报告正文不含任何时间戳，运行耗时等信息只打印到 stdout。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.hybrid_search import HybridSearchService  # noqa: E402
from src.ai.provider import EmbeddingResult  # noqa: E402
from src.ai.vector_recall import (  # noqa: E402
    PersistentVectorRecallSource,
    SearchableContentFingerprintSource,
)
from src.database import Database  # noqa: E402
from src.search_mode import result_source  # noqa: E402
from src.search_service import SearchService  # noqa: E402

#: 合同 §2：limit=50 ≥ 语料 42 页，「不在结果中」只由检索逻辑决定。
SEARCH_LIMIT: Final[int] = 50

#: 合同 §3 的五种 delta 标签（固定顺序，用于稳定输出）。
DELTA_LABELS: Final[tuple[str, ...]] = (
    "improved",
    "unchanged",
    "regression",
    "hybrid-only recall",
    "both-miss",
)

#: 合同 §4：F 类（negative / no-good-answer）。
NEGATIVE_CATEGORY: Final[str] = "F"

#: 合同 §3：跨文档覆盖只看 D 类。
CROSS_DOCUMENT_CATEGORY: Final[str] = "D"


# ---------------------------------------------------------------------------
# 冻结输入的结构化模型与校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixturePage:
    """语料中的一页（含 latent-topic fixture 权重与嵌入覆盖标记）。"""

    key: str
    page_number: int
    text: str
    topics: Mapping[str, float]
    embedded: bool


@dataclass(frozen=True, slots=True)
class FixtureDocument:
    """语料中的一个文档。"""

    key: str
    title: str
    filename: str
    pages: tuple[FixturePage, ...]


@dataclass(frozen=True, slots=True)
class CorpusFixture:
    """冻结语料：文档/页面 + fixture embedding 三元组 + topic 维度表。"""

    corpus_id: str
    embedding_model: str
    embedding_dimensions: int
    embedding_config_version: int
    topics: tuple[str, ...]
    documents: tuple[FixtureDocument, ...]

    @property
    def page_keys(self) -> tuple[str, ...]:
        """按语料顺序返回全部页面 key。"""

        return tuple(page.key for document in self.documents for page in document.pages)


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """冻结 query 集中的一条 query。"""

    query_id: str
    text: str
    category: str
    topics: Mapping[str, float]
    relevant_pages: tuple[str, ...]
    distractor_pages: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class QuerySet:
    """冻结 query 集。"""

    query_set_id: str
    categories: Mapping[str, str]
    queries: tuple[QuerySpec, ...]


def _load_json(path: Path) -> Any:
    """读取 JSON 文件；结构非法时抛出带中文说明的 ``ValueError``。"""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到冻结输入文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"冻结输入不是合法 JSON：{path}（{exc}）") from exc


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    """校验 ``value`` 是 JSON 对象。"""

    if not isinstance(value, dict):
        raise ValueError(f"{where} 必须是 JSON 对象，实际为 {type(value).__name__}")
    return value


def _require_str(value: Any, where: str) -> str:
    """校验 ``value`` 是非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} 必须是非空字符串，实际为 {value!r}")
    return value


def _require_topic_weights(value: Any, where: str, known_topics: Sequence[str]) -> dict[str, float]:
    """校验 topic 权重表：topic 必须已知、权重必须是有限数值。"""

    mapping = _require_mapping(value, where)
    weights: dict[str, float] = {}
    known = set(known_topics)
    for topic, weight in mapping.items():
        if topic not in known:
            raise ValueError(f"{where} 使用了语料中不存在的 topic：{topic!r}")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(f"{where} 的 topic {topic!r} 权重必须是数值：{weight!r}")
        weights[topic] = float(weight)
    return weights


def _require_str_list(value: Any, where: str) -> tuple[str, ...]:
    """校验 ``value`` 是字符串数组。"""

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} 必须是字符串数组，实际为 {value!r}")
    return tuple(value)


def load_corpus(path: Path) -> CorpusFixture:
    """加载并校验冻结语料 JSON。"""

    raw = _require_mapping(_load_json(path), f"语料文件 {path}")
    topics = _require_str_list(raw.get("topics"), "语料 topics")
    if not topics:
        raise ValueError("语料 topics 不能为空")
    dimensions = raw.get("embedding_dimensions")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
        raise ValueError(f"语料 embedding_dimensions 必须是正整数：{dimensions!r}")
    if len(topics) != dimensions:
        raise ValueError(f"语料 topics 数量 {len(topics)} 与 dimensions {dimensions} 不一致")
    config_version = raw.get("embedding_config_version")
    if (
        isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version <= 0
    ):
        raise ValueError(f"语料 embedding_config_version 必须是正整数：{config_version!r}")
    raw_documents = raw.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("语料 documents 必须是非空数组")
    documents: list[FixtureDocument] = []
    seen_page_keys: set[str] = set()
    for index, raw_document in enumerate(raw_documents):
        document_raw = _require_mapping(raw_document, f"语料 documents[{index}]")
        raw_pages = document_raw.get("pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raise ValueError(f"语料 documents[{index}] 的 pages 必须是非空数组")
        pages: list[FixturePage] = []
        for page_index, raw_page in enumerate(raw_pages):
            where = f"语料 documents[{index}].pages[{page_index}]"
            page_raw = _require_mapping(raw_page, where)
            key = _require_str(page_raw.get("key"), f"{where}.key")
            if key in seen_page_keys:
                raise ValueError(f"语料页面 key 重复：{key!r}")
            seen_page_keys.add(key)
            page_number = page_raw.get("page_number")
            if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
                raise ValueError(f"{where}.page_number 必须是正整数：{page_number!r}")
            embedded = page_raw.get("embedded")
            if not isinstance(embedded, bool):
                raise ValueError(f"{where}.embedded 必须是布尔值：{embedded!r}")
            pages.append(
                FixturePage(
                    key=key,
                    page_number=page_number,
                    text=_require_str(page_raw.get("text"), f"{where}.text"),
                    topics=_require_topic_weights(
                        page_raw.get("topics"), f"{where}.topics", topics
                    ),
                    embedded=embedded,
                )
            )
        documents.append(
            FixtureDocument(
                key=_require_str(document_raw.get("key"), f"语料 documents[{index}].key"),
                title=_require_str(document_raw.get("title"), f"语料 documents[{index}].title"),
                filename=_require_str(
                    document_raw.get("filename"), f"语料 documents[{index}].filename"
                ),
                pages=tuple(pages),
            )
        )
    return CorpusFixture(
        corpus_id=_require_str(raw.get("corpus_id"), "语料 corpus_id"),
        embedding_model=_require_str(raw.get("embedding_model"), "语料 embedding_model"),
        embedding_dimensions=dimensions,
        embedding_config_version=config_version,
        topics=topics,
        documents=tuple(documents),
    )


def load_queries(path: Path, corpus: CorpusFixture) -> QuerySet:
    """加载并校验冻结 query 集 JSON（relevant/distractor key 必须存在于语料）。"""

    raw = _require_mapping(_load_json(path), f"query 文件 {path}")
    known_pages = set(corpus.page_keys)
    raw_queries = raw.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("query 集 queries 必须是非空数组")
    queries: list[QuerySpec] = []
    seen_ids: set[str] = set()
    for index, raw_query in enumerate(raw_queries):
        where = f"query[{index}]"
        query_raw = _require_mapping(raw_query, where)
        query_id = _require_str(query_raw.get("id"), f"{where}.id")
        if query_id in seen_ids:
            raise ValueError(f"query id 重复：{query_id!r}")
        seen_ids.add(query_id)
        relevant = _require_str_list(query_raw.get("relevant_pages"), f"{where}.relevant_pages")
        distractor = _require_str_list(
            query_raw.get("distractor_pages"), f"{where}.distractor_pages"
        )
        for key in (*relevant, *distractor):
            if key not in known_pages:
                raise ValueError(f"{where} 引用了语料中不存在的页面：{key!r}")
        queries.append(
            QuerySpec(
                query_id=query_id,
                text=_require_str(query_raw.get("text"), f"{where}.text"),
                category=_require_str(query_raw.get("category"), f"{where}.category"),
                topics=_require_topic_weights(
                    query_raw.get("topics"), f"{where}.topics", corpus.topics
                ),
                relevant_pages=relevant,
                distractor_pages=distractor,
                notes=str(query_raw.get("notes", "")),
            )
        )
    categories_raw = raw.get("categories", {})
    categories = {str(key): str(value) for key, value in _require_mapping(
        categories_raw, "query 集 categories"
    ).items()}
    return QuerySet(
        query_set_id=_require_str(raw.get("query_set_id"), "query 集 query_set_id"),
        categories=categories,
        queries=tuple(queries),
    )


def fixture_vector(
    topics: Mapping[str, float], topic_order: Sequence[str], dimensions: int
) -> tuple[float, ...]:
    """把 topic 权重表展开成 fixture 向量：第 i 维 = ``topic_order[i]`` 的权重。"""

    if len(topic_order) != dimensions:
        raise ValueError(f"topic 维度表长度 {len(topic_order)} 与 dimensions {dimensions} 不一致")
    return tuple(float(topics.get(topic, 0.0)) for topic in topic_order)


# ---------------------------------------------------------------------------
# 离线 query embedding fake
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedCallRecord:
    """一次 fake query embedding 调用的完整记录（成本维度证据）。"""

    texts: tuple[str, ...]
    model: str | None
    dimensions: int | None


class FixtureEmbeddingProvider:
    """按冻结 query 文本返回 fixture 向量的 ``EmbeddingProvider`` fake。

    只认识冻结 query 集中的精确文本；任何未知文本直接 ``AssertionError``，
    保证 benchmark 绝不会意外触发「真实语义」路径。每次调用都被完整记录。
    """

    def __init__(self, vectors: Mapping[str, tuple[float, ...]]) -> None:
        self._vectors = dict(vectors)
        self.calls: list[EmbedCallRecord] = []

    def embed(
        self,
        texts: Sequence[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        """按输入顺序返回每个文本对应的 fixture 向量。"""

        batch = tuple(texts)
        self.calls.append(EmbedCallRecord(texts=batch, model=model, dimensions=dimensions))
        embeddings: list[tuple[float, ...]] = []
        for text in batch:
            if text not in self._vectors:
                raise AssertionError(f"fake provider 收到未知 query 文本：{text!r}")
            embeddings.append(self._vectors[text])
        return EmbeddingResult(
            embeddings=tuple(embeddings),
            model=model or "synthetic-fixture-v1",
            usage=None,
        )


# ---------------------------------------------------------------------------
# 指标函数（合同 §3；纯函数，供单测直接锁定）
# ---------------------------------------------------------------------------


def best_rank(ranking: Sequence[str], relevant: Sequence[str]) -> int | None:
    """relevant 页在排名中的最小 1-based 位置；全部缺席返回 ``None``（ABSENT）。"""

    positions = [index for index, key in enumerate(ranking, start=1) if key in set(relevant)]
    return min(positions) if positions else None


def reciprocal_rank(best: int | None) -> float:
    """RR = 1 / best_rank；ABSENT 记 0。"""

    return 0.0 if best is None else 1.0 / best


def hit_at(best: int | None, k: int) -> bool:
    """Hit@k：best_rank ≤ k；ABSENT 记 False。"""

    return best is not None and best <= k


def coverage_at_5(ranking: Sequence[str], relevant: Sequence[str]) -> float:
    """Coverage@5 = Top 5 中不同 relevant 页数 / |relevant|。"""

    relevant_set = set(relevant)
    if not relevant_set:
        raise ValueError("relevant 为空时 Coverage@5 无定义")
    return len(set(ranking[:5]) & relevant_set) / len(relevant_set)


def delta_label(keyword_best: int | None, hybrid_best: int | None) -> str:
    """合同 §3 的 delta 标签。

    - 双方 ABSENT → ``both-miss``；
    - keyword ABSENT 且 hybrid 命中 → ``hybrid-only recall``（单列）；
    - keyword 命中且 hybrid ABSENT → ``regression``（hybrid 把结果拉低的极端情形）；
    - 其余按 ``keyword_best − hybrid_best`` 的符号：正 improved、零 unchanged、负 regression。
    """

    if keyword_best is None and hybrid_best is None:
        return "both-miss"
    if keyword_best is None:
        return "hybrid-only recall"
    if hybrid_best is None:
        return "regression"
    if keyword_best > hybrid_best:
        return "improved"
    if keyword_best < hybrid_best:
        return "regression"
    return "unchanged"


def cross_document_covered(
    ranking: Sequence[str],
    relevant: Sequence[str],
    document_of: Mapping[str, str],
) -> bool:
    """D 类口径：Top 5 中的 relevant 页是否覆盖 ≥ 2 个不同文档。"""

    relevant_set = set(relevant)
    documents = {document_of[key] for key in ranking[:5] if key in relevant_set}
    return len(documents) >= 2


# ---------------------------------------------------------------------------
# 逐 query 记录与聚合
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankedEntry:
    """hybrid 排名中的一条结果（含 UI 同款 provenance 标签）。"""

    page_key: str
    provenance: str
    lexical_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True, slots=True)
class ModeMetrics:
    """单一模式在一条 query 上的指标；F 类（无 relevant）全部为 ``None``。"""

    best_rank: int | None
    hit_at_1: bool | None
    hit_at_3: bool | None
    reciprocal_rank: float | None
    coverage_at_5: float | None


@dataclass(frozen=True, slots=True)
class QueryBenchmarkRecord:
    """合同 §5 的逐 query 固定记录字段。"""

    query_id: str
    text: str
    category: str
    notes: str
    relevant_pages: tuple[str, ...]
    keyword_ranking: tuple[str, ...]
    hybrid_ranking: tuple[RankedEntry, ...]
    vector_status: str
    invalid_vector_candidates: int
    embed_call_count: int
    keyword_metrics: ModeMetrics
    hybrid_metrics: ModeMetrics
    delta: str | None
    keyword_cross_document_top5: bool | None
    hybrid_cross_document_top5: bool | None
    provenance_counts_top10: Mapping[str, int]
    semantic_recall_in_top5: bool | None


@dataclass(frozen=True, slots=True)
class ModeAggregate:
    """一组 query 在单一模式下的聚合指标；无有效 query 时指标为 ``None``。"""

    query_count: int
    hit_at_1: float | None
    hit_at_3: float | None
    mrr: float | None


@dataclass(frozen=True, slots=True)
class CategoryAggregate:
    """一个类别的双模式聚合 + delta 标签分布。"""

    category: str
    description: str
    keyword: ModeAggregate
    hybrid: ModeAggregate
    delta_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """一次完整 benchmark 运行的全部证据（确定性、无时间戳）。"""

    corpus_id: str
    query_set_id: str
    embedding_model: str
    embedding_dimensions: int
    embedding_config_version: int
    total_pages: int
    embedded_pages: int
    queries: tuple[QueryBenchmarkRecord, ...]
    per_category: tuple[CategoryAggregate, ...]
    overall_keyword: ModeAggregate
    overall_hybrid: ModeAggregate
    overall_delta_counts: Mapping[str, int]
    embed_calls: tuple[EmbedCallRecord, ...] = field(default=())


def _aggregate_mode(records: Sequence[QueryBenchmarkRecord], mode: str) -> ModeAggregate:
    """聚合一组 query 的 Hit@1 / Hit@3 / MRR（只统计有 relevant 的 query）。"""

    scored = [record for record in records if record.relevant_pages]
    if not scored:
        return ModeAggregate(query_count=0, hit_at_1=None, hit_at_3=None, mrr=None)
    metrics = [
        record.keyword_metrics if mode == "keyword" else record.hybrid_metrics
        for record in scored
    ]
    count = len(metrics)
    return ModeAggregate(
        query_count=count,
        hit_at_1=sum(1.0 for item in metrics if item.hit_at_1) / count,
        hit_at_3=sum(1.0 for item in metrics if item.hit_at_3) / count,
        mrr=sum(item.reciprocal_rank or 0.0 for item in metrics) / count,
    )


def _delta_counts(records: Sequence[QueryBenchmarkRecord]) -> dict[str, int]:
    """统计一组 query 的 delta 标签分布（固定包含全部五种标签）。"""

    counts = {label: 0 for label in DELTA_LABELS}
    for record in records:
        if record.delta is not None:
            counts[record.delta] += 1
    return counts


# ---------------------------------------------------------------------------
# 核心运行
# ---------------------------------------------------------------------------


def _build_database(
    corpus: CorpusFixture, work_dir: Path
) -> tuple[Database, dict[str, int], dict[str, str]]:
    """在临时目录建库：写入文档/页面，返回 ``(db, page_key→page_id, page_key→doc_key)``。"""

    database = Database(work_dir / "benchmark.db")
    page_ids: dict[str, int] = {}
    document_of: dict[str, str] = {}
    for document in corpus.documents:
        digest = hashlib.sha256((document.title + document.filename).encode("utf-8")).hexdigest()
        created = database.create_document(
            title=document.title,
            filename=document.filename,
            source_path=work_dir / document.filename,
            sha256=digest,
        )
        for page in document.pages:
            safe_name = page.key.replace("/", "_")
            created_page = database.create_page(
                document_id=created.id,
                page_number=page.page_number,
                image_path=work_dir / f"{safe_name}.png",
                extracted_text=page.text,
            )
            page_ids[page.key] = created_page.id
            document_of[page.key] = document.key
    return database, page_ids, document_of


def _write_fixture_embeddings(
    corpus: CorpusFixture, database: Database, page_ids: Mapping[str, int]
) -> int:
    """对 ``embedded: true`` 页面写入 fixture 向量，返回写入数量。

    指纹走真实的 ``SearchableContentFingerprintSource``，保证 recall 侧
    freshness 校验与生产路径完全一致。
    """

    fingerprints = SearchableContentFingerprintSource(database)
    written = 0
    for document in corpus.documents:
        for page in document.pages:
            if not page.embedded:
                continue
            page_id = page_ids[page.key]
            fingerprint = fingerprints.current_source_sha256(page_id)
            assert fingerprint is not None, f"页面 {page.key} 的当前指纹不应为 None"
            database.upsert_page_embedding(
                page_id=page_id,
                source_text_sha256=fingerprint,
                model=corpus.embedding_model,
                dimensions=corpus.embedding_dimensions,
                config_version=corpus.embedding_config_version,
                vector=fixture_vector(
                    page.topics, corpus.topics, corpus.embedding_dimensions
                ),
            )
            written += 1
    return written


def _mode_metrics(ranking: Sequence[str], relevant: Sequence[str]) -> ModeMetrics:
    """计算单一模式的逐 query 指标；relevant 为空（F 类）时全部 ``None``。"""

    if not relevant:
        return ModeMetrics(
            best_rank=None,
            hit_at_1=None,
            hit_at_3=None,
            reciprocal_rank=None,
            coverage_at_5=None,
        )
    best = best_rank(ranking, relevant)
    return ModeMetrics(
        best_rank=best,
        hit_at_1=hit_at(best, 1),
        hit_at_3=hit_at(best, 3),
        reciprocal_rank=reciprocal_rank(best),
        coverage_at_5=coverage_at_5(ranking, relevant),
    )


def _run_single_query(
    query: QuerySpec,
    search_service: SearchService,
    hybrid_service: HybridSearchService,
    provider: FixtureEmbeddingProvider,
    page_keys_by_id: Mapping[int, str],
    document_of: Mapping[str, str],
) -> QueryBenchmarkRecord:
    """对一条冻结 query 执行 keyword + hybrid 双模式检索并按合同记录。"""

    keyword_results = search_service.search(query.text, limit=SEARCH_LIMIT)
    keyword_ranking = tuple(page_keys_by_id[result.page_id] for result in keyword_results)

    calls_before = len(provider.calls)
    outcome = hybrid_service.search(query.text, limit=SEARCH_LIMIT)
    embed_calls = len(provider.calls) - calls_before
    assert embed_calls == 1, (
        f"query {query.query_id} 的 query embedding 调用必须恰好 1 次，实际 {embed_calls} 次"
    )

    hybrid_ranking = tuple(
        RankedEntry(
            page_key=page_keys_by_id[hit.result.page_id],
            # 与 UI 完全一致的 provenance 标签规则，见 src/search_mode.py:52-63。
            provenance=result_source(
                lexical_rank=hit.lexical_rank, vector_rank=hit.vector_rank
            ),
            lexical_rank=hit.lexical_rank,
            vector_rank=hit.vector_rank,
        )
        for hit in outcome.results
    )

    keyword_metrics = _mode_metrics(keyword_ranking, query.relevant_pages)
    hybrid_metrics = _mode_metrics(
        tuple(entry.page_key for entry in hybrid_ranking), query.relevant_pages
    )
    delta = (
        delta_label(keyword_metrics.best_rank, hybrid_metrics.best_rank)
        if query.relevant_pages
        else None
    )

    is_cross_document = query.category == CROSS_DOCUMENT_CATEGORY and bool(query.relevant_pages)
    provenance_counts: dict[str, int] = {}
    for entry in hybrid_ranking[:10]:
        provenance_counts[entry.provenance] = provenance_counts.get(entry.provenance, 0) + 1
    semantic_in_top5 = (
        any(entry.provenance == "语义召回" for entry in hybrid_ranking[:5])
        if query.category == NEGATIVE_CATEGORY
        else None
    )
    return QueryBenchmarkRecord(
        query_id=query.query_id,
        text=query.text,
        category=query.category,
        notes=query.notes,
        relevant_pages=query.relevant_pages,
        keyword_ranking=keyword_ranking,
        hybrid_ranking=hybrid_ranking,
        vector_status=str(outcome.vector_status),
        invalid_vector_candidates=outcome.invalid_vector_candidates,
        embed_call_count=embed_calls,
        keyword_metrics=keyword_metrics,
        hybrid_metrics=hybrid_metrics,
        delta=delta,
        keyword_cross_document_top5=(
            cross_document_covered(keyword_ranking, query.relevant_pages, document_of)
            if is_cross_document
            else None
        ),
        hybrid_cross_document_top5=(
            cross_document_covered(
                tuple(entry.page_key for entry in hybrid_ranking),
                query.relevant_pages,
                document_of,
            )
            if is_cross_document
            else None
        ),
        provenance_counts_top10=provenance_counts,
        semantic_recall_in_top5=semantic_in_top5,
    )


def run_benchmark(root: Path) -> BenchmarkReport:
    """执行完整离线 benchmark：建库 → 写 fixture embedding → 双模式跑全部 query。

    只读 ``root/benchmarks`` 下的两个冻结 JSON；数据库建在临时目录，
    随函数返回自动清理。同一输入多次调用返回完全一致的报告。
    """

    corpus = load_corpus(root / "benchmarks" / "corpus_synthetic_v1.json")
    query_set = load_queries(root / "benchmarks" / "queries_v1.json", corpus)
    with tempfile.TemporaryDirectory(prefix="ekb-retrieval-benchmark-") as tmp:
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
        hybrid_service = HybridSearchService(
            lexical=search_service, hydration=database, vector=recall_source
        )

        records = tuple(
            _run_single_query(
                query,
                search_service,
                hybrid_service,
                provider,
                page_keys_by_id,
                document_of,
            )
            for query in query_set.queries
        )

    categories: list[CategoryAggregate] = []
    ordered_categories = sorted({query.category for query in query_set.queries})
    for category in ordered_categories:
        group = [record for record in records if record.category == category]
        categories.append(
            CategoryAggregate(
                category=category,
                description=query_set.categories.get(category, ""),
                keyword=_aggregate_mode(group, "keyword"),
                hybrid=_aggregate_mode(group, "hybrid"),
                delta_counts=_delta_counts(group),
            )
        )
    return BenchmarkReport(
        corpus_id=corpus.corpus_id,
        query_set_id=query_set.query_set_id,
        embedding_model=corpus.embedding_model,
        embedding_dimensions=corpus.embedding_dimensions,
        embedding_config_version=corpus.embedding_config_version,
        total_pages=len(corpus.page_keys),
        embedded_pages=embedded_pages,
        queries=records,
        per_category=tuple(categories),
        overall_keyword=_aggregate_mode(records, "keyword"),
        overall_hybrid=_aggregate_mode(records, "hybrid"),
        overall_delta_counts=_delta_counts(records),
        embed_calls=tuple(provider.calls),
    )


# ---------------------------------------------------------------------------
# 报告输出（确定性：无时间戳、排序固定）
# ---------------------------------------------------------------------------


def _format_optional_float(value: float | None) -> str:
    """格式化可空浮点指标。"""

    return "—" if value is None else f"{value:.4f}"


def _format_best_rank(best: int | None, has_relevant: bool) -> str:
    """格式化 best_rank：无 relevant 记 n/a，缺席记 ABSENT。"""

    if not has_relevant:
        return "n/a"
    return "ABSENT" if best is None else str(best)


def _mode_metrics_dict(metrics: ModeMetrics) -> dict[str, Any]:
    """``ModeMetrics`` 的 JSON 形式。"""

    return {
        "best_rank": metrics.best_rank,
        "hit_at_1": metrics.hit_at_1,
        "hit_at_3": metrics.hit_at_3,
        "reciprocal_rank": metrics.reciprocal_rank,
        "coverage_at_5": metrics.coverage_at_5,
    }


def _mode_aggregate_dict(aggregate: ModeAggregate) -> dict[str, Any]:
    """``ModeAggregate`` 的 JSON 形式。"""

    return {
        "query_count": aggregate.query_count,
        "hit_at_1": aggregate.hit_at_1,
        "hit_at_3": aggregate.hit_at_3,
        "mrr": aggregate.mrr,
    }


def report_to_dict(report: BenchmarkReport) -> dict[str, Any]:
    """报告的 JSON 可序列化形式（``json.dumps(sort_keys=True)`` 下确定）。"""

    return {
        "evidence_level": "algorithmic/offline evidence（非真实模型语义质量证据）",
        "corpus_id": report.corpus_id,
        "query_set_id": report.query_set_id,
        "embedding": {
            "model": report.embedding_model,
            "dimensions": report.embedding_dimensions,
            "config_version": report.embedding_config_version,
            "embedded_pages": report.embedded_pages,
            "total_pages": report.total_pages,
        },
        "aggregates": {
            "overall": {
                "keyword": _mode_aggregate_dict(report.overall_keyword),
                "hybrid": _mode_aggregate_dict(report.overall_hybrid),
                "delta_counts": dict(report.overall_delta_counts),
            },
            "per_category": {
                item.category: {
                    "description": item.description,
                    "keyword": _mode_aggregate_dict(item.keyword),
                    "hybrid": _mode_aggregate_dict(item.hybrid),
                    "delta_counts": dict(item.delta_counts),
                }
                for item in report.per_category
            },
        },
        "embed_calls": [
            {"texts": list(call.texts), "model": call.model, "dimensions": call.dimensions}
            for call in report.embed_calls
        ],
        "queries": [
            {
                "id": record.query_id,
                "text": record.text,
                "category": record.category,
                "notes": record.notes,
                "relevant_pages": list(record.relevant_pages),
                "keyword_ranking": list(record.keyword_ranking),
                "keyword_result_count": len(record.keyword_ranking),
                "hybrid_ranking": [
                    {
                        "page_key": entry.page_key,
                        "provenance": entry.provenance,
                        "lexical_rank": entry.lexical_rank,
                        "vector_rank": entry.vector_rank,
                    }
                    for entry in record.hybrid_ranking
                ],
                "hybrid_result_count": len(record.hybrid_ranking),
                "vector_status": record.vector_status,
                "invalid_vector_candidates": record.invalid_vector_candidates,
                "embed_call_count": record.embed_call_count,
                "keyword_metrics": _mode_metrics_dict(record.keyword_metrics),
                "hybrid_metrics": _mode_metrics_dict(record.hybrid_metrics),
                "delta": record.delta,
                "keyword_cross_document_top5": record.keyword_cross_document_top5,
                "hybrid_cross_document_top5": record.hybrid_cross_document_top5,
                "provenance_counts_top10": dict(record.provenance_counts_top10),
                "semantic_recall_in_top5": record.semantic_recall_in_top5,
            }
            for record in report.queries
        ],
    }


def _render_ranking_lines(
    record: QueryBenchmarkRecord, mode: str
) -> list[str]:
    """渲染一个模式的 Top 10 排名列表（★ 标出 relevant 页）。"""

    relevant = set(record.relevant_pages)
    lines: list[str] = []
    if mode == "keyword":
        entries: Sequence[tuple[str, str]] = tuple(
            (key, "") for key in record.keyword_ranking[:10]
        )
    else:
        entries = tuple(
            (entry.page_key, f" — {entry.provenance}")
            for entry in record.hybrid_ranking[:10]
        )
    if not entries:
        return ["  （无结果）"]
    for rank, (key, suffix) in enumerate(entries, start=1):
        marker = "★ " if key in relevant else ""
        lines.append(f"  {rank}. {marker}`{key}`{suffix}")
    return lines


def report_to_markdown(report: BenchmarkReport) -> str:
    """渲染 Markdown 报告（正文无时间戳，输出确定）。"""

    lines: list[str] = [
        "# v0.5.1 检索质量离线 Baseline 报告",
        "",
        "> **algorithmic / offline evidence**：本报告由 `scripts/retrieval_benchmark.py`",
        "> 基于冻结合成语料与确定性 latent-topic fixture 向量生成，只验证",
        "> lexical / vector recall / RRF / provenance / partial coverage 机制，",
        "> **不构成真实模型（Qwen）语义质量证据**。",
        "",
        f"- 语料：`{report.corpus_id}`（{report.total_pages} 页）",
        f"- Query 集：`{report.query_set_id}`（{len(report.queries)} 条，A–J）",
        f"- Embedding 覆盖：{report.embedded_pages}/{report.total_pages} 页",
        f"- 隔离三元组：model=`{report.embedding_model}`, "
        f"dimensions={report.embedding_dimensions}, "
        f"config_version={report.embedding_config_version}",
        "",
        "## 逐 query 记录",
    ]
    for record in report.queries:
        lines.append("")
        lines.append(f"### {record.query_id}（{record.category} 类）{record.text}")
        lines.append("")
        if record.relevant_pages:
            relevant = "、".join(f"`{key}`" for key in record.relevant_pages)
            lines.append(f"- relevant：{relevant}")
        else:
            lines.append("- relevant：（空，F 类 negative query，无 Top1 正确性可言）")
        kw_best = _format_best_rank(
            record.keyword_metrics.best_rank, bool(record.relevant_pages)
        )
        hy_best = _format_best_rank(
            record.hybrid_metrics.best_rank, bool(record.relevant_pages)
        )
        lines.append(
            f"- Keyword Top 10（共 {len(record.keyword_ranking)} 条；best_rank={kw_best}）："
        )
        lines.extend(_render_ranking_lines(record, "keyword"))
        lines.append(
            f"- Hybrid Top 10（共 {len(record.hybrid_ranking)} 条；best_rank={hy_best}；"
            f"vector_status={record.vector_status}；"
            f"invalid_vector_candidates={record.invalid_vector_candidates}；"
            f"embedding 调用次数={record.embed_call_count}）："
        )
        lines.extend(_render_ranking_lines(record, "hybrid"))
        if record.relevant_pages:
            kw = record.keyword_metrics
            hy = record.hybrid_metrics
            lines.append(
                "- 指标：keyword "
                f"Hit@1={kw.hit_at_1} Hit@3={kw.hit_at_3} "
                f"RR={_format_optional_float(kw.reciprocal_rank)} "
                f"Coverage@5={_format_optional_float(kw.coverage_at_5)}；"
                "hybrid "
                f"Hit@1={hy.hit_at_1} Hit@3={hy.hit_at_3} "
                f"RR={_format_optional_float(hy.reciprocal_rank)} "
                f"Coverage@5={_format_optional_float(hy.coverage_at_5)}"
            )
            lines.append(f"- delta：**{record.delta}**")
            if record.category == CROSS_DOCUMENT_CATEGORY:
                lines.append(
                    f"- 跨文档覆盖（Top 5 ≥2 文档）：keyword={record.keyword_cross_document_top5}，"
                    f"hybrid={record.hybrid_cross_document_top5}"
                )
        else:
            lines.append(
                f"- F 类记录：keyword 结果数={len(record.keyword_ranking)}；"
                f"hybrid 结果数={len(record.hybrid_ranking)}；"
                f"Top 5 是否出现「语义召回」={record.semantic_recall_in_top5}"
            )
            lines.append(
                "- no-answer exposure 判定为定性（high / medium / low），"
                "由人工阅读本记录后填写；不得用 Top1 存在作为成功。"
            )
        if record.provenance_counts_top10:
            counts = "、".join(
                f"{label}×{count}"
                for label, count in sorted(record.provenance_counts_top10.items())
            )
            lines.append(f"- Hybrid Top 10 provenance 分布：{counts}")
        lines.append(f"- 观察（冻结 notes）：{record.notes}")

    lines.append("")
    lines.append("## 聚合指标（按类别 × 模式）")
    lines.append("")
    lines.append("| 类别 | 模式 | 有效 query 数 | Hit@1 | Hit@3 | MRR |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in report.per_category:
        for mode, aggregate in (("keyword", item.keyword), ("hybrid", item.hybrid)):
            lines.append(
                f"| {item.category} | {mode} | {aggregate.query_count} | "
                f"{_format_optional_float(aggregate.hit_at_1)} | "
                f"{_format_optional_float(aggregate.hit_at_3)} | "
                f"{_format_optional_float(aggregate.mrr)} |"
            )
    for mode, aggregate in (("keyword", report.overall_keyword), ("hybrid", report.overall_hybrid)):
        lines.append(
            f"| **整体** | **{mode}** | {aggregate.query_count} | "
            f"**{_format_optional_float(aggregate.hit_at_1)}** | "
            f"**{_format_optional_float(aggregate.hit_at_3)}** | "
            f"**{_format_optional_float(aggregate.mrr)}** |"
        )
    lines.append("")
    lines.append("## delta 标签分布（非 F 类）")
    lines.append("")
    lines.append("| 类别 | " + " | ".join(DELTA_LABELS) + " |")
    lines.append("| --- |" + " --- |" * len(DELTA_LABELS))
    for item in report.per_category:
        counts = " | ".join(str(item.delta_counts[label]) for label in DELTA_LABELS)
        lines.append(f"| {item.category} | {counts} |")
    overall = " | ".join(str(report.overall_delta_counts[label]) for label in DELTA_LABELS)
    lines.append(f"| **整体** | {overall} |")
    lines.append("")
    return "\n".join(lines)


def summary_lines(report: BenchmarkReport) -> list[str]:
    """stdout 紧凑摘要：整体指标、delta 分布、F 类 exposure 提示。"""

    lines = [
        f"语料={report.corpus_id} query集={report.query_set_id} "
        f"embedding覆盖={report.embedded_pages}/{report.total_pages}",
    ]
    for mode, aggregate in (("keyword", report.overall_keyword), ("hybrid", report.overall_hybrid)):
        lines.append(
            f"{mode}: Hit@1={_format_optional_float(aggregate.hit_at_1)} "
            f"Hit@3={_format_optional_float(aggregate.hit_at_3)} "
            f"MRR={_format_optional_float(aggregate.mrr)} "
            f"(n={aggregate.query_count})"
        )
    delta = "、".join(
        f"{label}={report.overall_delta_counts[label]}" for label in DELTA_LABELS
    )
    lines.append(f"delta 分布：{delta}")
    for record in report.queries:
        if record.category == NEGATIVE_CATEGORY:
            lines.append(
                f"F 类 {record.query_id}：keyword 结果数={len(record.keyword_ranking)}，"
                f"hybrid 结果数={len(record.hybrid_ranking)}，"
                f"Top 5 出现语义召回={record.semantic_recall_in_top5}"
                "（no-answer exposure 需人工定性判定）"
            )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：运行 benchmark，按参数输出 JSON / Markdown，并打印 stdout 摘要。"""

    parser = argparse.ArgumentParser(
        description="v0.5.1 离线检索质量 benchmark（algorithmic/offline evidence）"
    )
    parser.add_argument("--json-out", type=Path, default=None, help="JSON 报告输出路径")
    parser.add_argument("--md-out", type=Path, default=None, help="Markdown 报告输出路径")
    args = parser.parse_args(argv)

    report = run_benchmark(PROJECT_ROOT)
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
