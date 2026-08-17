"""Phase 3 受控真实 embedding 校准（v0.5.1，付费，最小样本）。

严格复刻生产 ``QwenProvider.embed`` 请求语义，回答唯一问题：真实
``qwen3.7-text-embedding`` 的 similarity distribution 能否支持某种
vector eligibility strategy。

硬边界（与 ``docs/v0.5.1-phase3-paid-call-proposal.md`` 一致）：

- model=qwen3.7-text-embedding, thinking=N/A, dimensions=1024;
- retry=0（``max_extra_attempts=0``）;
- 每批 <= 20 输入，HTTP 硬上限 5 次;
- realized query <= 23、去重候选页 <= 60；
- 请求语义与生产 ``QwenProvider.embed`` 完全一致，不引入 text_type /
  instruct / sparse / rerank 等非生产字段;
- production DB write = 0, staging DB write = 0；
- raw evidence 只写 ``logs/``（git-ignored），不写 data/、staging-data/。

所有 query/page 输入均从 **frozen** ``benchmarks/queries_v1.json`` 与
``benchmarks/corpus_synthetic_v1.json`` 派生，零人为新增判定、零 production
数据读取。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.provider import AIError  # noqa: E402
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.ai.vector_recall import cosine_similarity  # noqa: E402
from src.config import OfficialEndpointError, Settings, get_settings  # noqa: E402

EXPERIMENT_MODEL: Final[str] = "qwen3.7-text-embedding"
EXPERIMENT_DIMENSIONS: Final[int] = 1024
EXPERIMENT_EXTRA_ATTEMPTS: Final[int] = 0
BATCH_LIMIT: Final[int] = 20
HTTP_BUDGET: Final[int] = 5
MAX_QUERY: Final[int] = 23
MAX_PAGE: Final[int] = 60

# relationship categories for (query, page) pairs, matching the frozen proposal.
CAT_TRUE = "true-semantic-relevant"
CAT_PARAPHRASE = "paraphrase-relevant"
CAT_LEX_OVERLAP = "lexical-overlap-distractor"
CAT_UNRELATED = "unrelated-distractor"
CAT_PARTIAL = "partial-coverage-regression"


def load_frozen():
    queries = json.loads(
        (PROJECT_ROOT / "benchmarks" / "queries_v1.json").read_text(encoding="utf-8")
    )
    corpus = json.loads(
        (PROJECT_ROOT / "benchmarks" / "corpus_synthetic_v1.json").read_text(encoding="utf-8")
    )
    page_text: dict[str, str] = {}
    page_embedded: dict[str, bool] = {}
    for doc in corpus["documents"]:
        for page in doc["pages"]:
            page_text[page["key"]] = page["text"]
            page_embedded[page["key"]] = bool(page.get("embedded", False))
    return queries, page_text, page_embedded


def build_pairs(queries, page_text, page_embedded):
    """Derive (query_text, page_key, category) pairs from frozen ground truth.

    Selection is deterministic and driven by the frozen relevance sets. The
    intent is distribution shape across five relationship classes, not
    exhaustiveness.
    """
    by_id = {q["id"]: q for q in queries["queries"]}
    query_text: dict[str, str] = {}
    pairs: list[tuple[str, str, str]] = []  # (query_id, page_key, category)

    def add(query_id, page_key, category):
        pairs.append((query_id, page_key, category))
        query_text[query_id] = by_id[query_id]["text"]

    # --- true semantic relevant: embedded relevant pages of clear queries ---
    for qid in ("A1", "A2", "C2", "D1", "G1"):
        for pk in by_id[qid]["relevant_pages"]:
            if page_embedded.get(pk):
                add(qid, pk, CAT_TRUE)

    # --- paraphrase relevant: category B rewrites + their relevant pages ---
    for qid in ("B1", "B2", "B3", "B4", "H2"):
        for pk in by_id[qid]["relevant_pages"]:
            add(qid, pk, CAT_PARAPHRASE)

    # --- lexical-overlap distractor: all frozen distractor_pages ---
    for q in queries["queries"]:
        for pk in q.get("distractor_pages", []):
            if pk in page_text:
                add(q["id"], pk, CAT_LEX_OVERLAP)

    # --- unrelated distractor: F-class negative queries vs unrelated pages ---
    unrelated_map = {"F1": ("stm32/p1", "pid/p2", "power/p3"),
                     "F2": ("stm32/p4", "comm/p1", "robot/p5"),
                     "F3": ("pid/p2", "power/p1", "stm32/p8")}
    for qid, pages in unrelated_map.items():
        for pk in pages:
            if pk in page_text:
                add(qid, pk, CAT_UNRELATED)

    # --- partial-coverage regression: non-embedded relevant pages ---
    # Cover the D-01-critical queries (C3/J1/J2 are the ablation-identified
    # partial-coverage regression cases) plus a spread of the remaining
    # non-embedded relevant pages. A3/C4 are excluded to stay within the
    # frozen proposal's 23-query scope; their pages (power/p2, robot/p7) are
    # single-contributor partial-coverage samples already represented by
    # C3/J1/J2/B3/B4/H2/E1/I2.
    _partial_queries = ("C3", "J1", "J2", "B3", "B4", "H2", "E1", "I2", "A4")
    for qid in _partial_queries:
        for pk in by_id[qid].get("relevant_pages", []):
            if pk in page_text and not page_embedded.get(pk):
                add(qid, pk, CAT_PARTIAL)

    return query_text, pairs, page_text, page_embedded


def check_guards(settings: Settings, n_query: int, n_page: int, n_http: int) -> list[str]:
    problems: list[str] = []
    if settings.ai_mode != "api":
        problems.append(f'ai_mode 必须为 "api"（当前 {settings.ai_mode}）')
    if settings.ai_provider != "qwen":
        problems.append(f'ai_provider 必须为 "qwen"（当前 {settings.ai_provider}）')
    if not settings.ai_api_key.get_secret_value():
        problems.append("EKB_AI_API_KEY is not configured")
    if settings.ai_embedding_model != EXPERIMENT_MODEL:
        problems.append(f"ai_embedding_model 必须为 {EXPERIMENT_MODEL}")
    if n_query > MAX_QUERY:
        problems.append(f"query 数 {n_query} 超过上限 {MAX_QUERY}")
    if n_page > MAX_PAGE:
        problems.append(f"候选页数 {n_page} 超过上限 {MAX_PAGE}")
    if n_http > HTTP_BUDGET:
        problems.append(f"HTTP 请求数 {n_http} 超过预算 {HTTP_BUDGET}")
    return problems


def chunk(texts: list[str]) -> list[list[str]]:
    return [texts[i : i + BATCH_LIMIT] for i in range(0, len(texts), BATCH_LIMIT)]


def analyze(vectors: dict[str, list[float]], pairs, query_text, page_text) -> dict:
    rows = []
    for qid, pk, cat in pairs:
        if qid not in vectors or pk not in vectors:
            rows.append({"query": qid, "page": pk, "category": cat,
                         "similarity": None, "error": "missing_vector"})
            continue
        sim = cosine_similarity(vectors[qid], vectors[pk])
        rows.append({"query": qid, "page": pk, "category": cat, "similarity": sim})

    # per-category distribution
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["similarity"] is not None:
            by_cat[r["category"]].append(r["similarity"])

    summary = {}
    for cat, sims in sorted(by_cat.items()):
        summary[cat] = {
            "n": len(sims),
            "min": min(sims),
            "median": statistics.median(sims),
            "max": max(sims),
            "mean": statistics.fmean(sims),
        }
    return {"pairs": rows, "category_summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 真实 embedding 校准（付费）")
    parser.add_argument("--confirm-paid-call", action="store_true",
                        help="确认执行受控真实 embedding 请求")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="raw evidence JSON 输出路径（默认 logs/）")
    args = parser.parse_args(argv)

    if not args.confirm_paid_call:
        print("SKIPPED: 这是付费真实 API 调用入口。未提供 --confirm-paid-call，")
        print("不发起任何网络请求。")
        return 2

    queries, page_text, page_embedded = load_frozen()
    query_text, pairs, page_text, page_embedded = build_pairs(
        queries, page_text, page_embedded
    )
    query_ids = sorted(query_text.keys())
    page_keys = sorted({pk for _, pk, _ in pairs})
    # batch plan: all queries first, then all pages (each <= 20)
    all_units = []  # (kind, key, text)
    for qid in query_ids:
        all_units.append(("query", qid, query_text[qid]))
    for pk in page_keys:
        all_units.append(("page", pk, page_text[pk]))
    batches = chunk([u[2] for u in all_units])
    n_http = len(batches)

    settings: Settings
    try:
        settings = get_settings()
    except OfficialEndpointError as exc:
        print(f"配置错误：{exc}")
        return 3

    # ---- PREFLIGHT ----
    problems = check_guards(settings, len(query_ids), len(page_keys), n_http)
    print("--- Phase 3 preflight ---")
    print(f"model: {EXPERIMENT_MODEL}")
    print("thinking: N/A")
    print(f"dimensions: {EXPERIMENT_DIMENSIONS}")
    print(f"retry (max_extra_attempts): {EXPERIMENT_EXTRA_ATTEMPTS}")
    print(f"api_mode: {settings.ai_mode}")
    print(f"provider: {settings.ai_provider}")
    print(f"api_key present: {'YES' if settings.ai_api_key.get_secret_value() else 'NO'}")
    print(f"query count: {len(query_ids)} (limit {MAX_QUERY})")
    print(f"unique page count: {len(page_keys)} (limit {MAX_PAGE})")
    print(f"pair count: {len(pairs)}")
    print(f"batch size limit: {BATCH_LIMIT}")
    print(f"planned HTTP requests: {n_http} (budget {HTTP_BUDGET})")
    print(
        "request semantics: 与生产 QwenProvider.embed 一致"
        "（model/input/encoding_format/dimensions）"
    )
    print("production DB write: 0  staging DB write: 0")
    if problems:
        for p in problems:
            print(f"GUARD FAIL: {p}")
        print("未发起任何网络请求。")
        return 3

    # ---- EXECUTE ----
    provider = QwenProvider(
        api_key=settings.ai_api_key.get_secret_value(),
        llm_model=settings.ai_llm_model,
        llm_model_hard=settings.ai_llm_model_hard,
        embedding_model=settings.ai_embedding_model,
        rerank_model=settings.ai_rerank_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_extra_attempts=EXPERIMENT_EXTRA_ATTEMPTS,
        transport=urllib_transport,
    )
    vectors: dict[str, list[float]] = {}
    usage_total = 0
    http_made = 0
    t0 = time.perf_counter()
    for bi, batch in enumerate(batches, start=1):
        if http_made >= HTTP_BUDGET:
            print("STOP: 已达 HTTP 预算上限，停止。")
            return 4
        t_batch = time.perf_counter()
        try:
            result = provider.embed(batch, model=EXPERIMENT_MODEL,
                                    dimensions=EXPERIMENT_DIMENSIONS)
        except AIError as exc:
            print(f"STOP: 第 {bi} 批 CALL FAIL: {type(exc).__name__}: {exc}")
            print("按护栏：不重试、不再发起请求。")
            return 5
        http_made += 1
        latency_batch = time.perf_counter() - t_batch
        if len(result.embeddings) != len(batch):
            print(f"STOP: 第 {bi} 批返回向量数 {len(result.embeddings)} != 输入 {len(batch)}")
            return 6
        for idx, vec in enumerate(result.embeddings):
            if len(vec) != EXPERIMENT_DIMENSIONS:
                print(f"STOP: 第 {bi} 批第 {idx} 向量维度 {len(vec)} != {EXPERIMENT_DIMENSIONS}")
                return 6
        for unit, vec in zip(all_units[(bi - 1) * BATCH_LIMIT : bi * BATCH_LIMIT],
                            result.embeddings, strict=True):
            vectors[unit[1]] = list(vec)
        used = result.usage.prompt_tokens if result.usage is not None else None
        usage_total += used or 0
        print(f"batch {bi}/{n_http}: inputs={len(batch)} dim={len(result.embeddings[0])} "
              f"tokens={used} latency={latency_batch:.3f}s")

    total_latency = time.perf_counter() - t0
    print(f"TOTAL: http={http_made} latency={total_latency:.3f}s tokens={usage_total}")

    analysis = analyze(vectors, pairs, query_text, page_text)

    # write raw evidence (logs/, git-ignored)
    out_path = args.json_out or (
        PROJECT_ROOT / "logs" / "v0.5.1-phase3-calibration.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "evidence_level": "real qwen3.7-text-embedding calibration (paid, minimal)",
        "model": EXPERIMENT_MODEL,
        "dimensions": EXPERIMENT_DIMENSIONS,
        "http_requests": http_made,
        "query_count": len(query_ids),
        "page_count": len(page_keys),
        "pair_count": len(pairs),
        "token_usage_total": usage_total,
        "latency_seconds": total_latency,
        "category_summary": analysis["category_summary"],
        "pairs": analysis["pairs"],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nraw evidence -> {out_path}")

    print("\n=== category similarity summary ===")
    for cat, s in analysis["category_summary"].items():
        print(f"  {cat}: n={s['n']} min={s['min']:.4f} median={s['median']:.4f} "
              f"mean={s['mean']:.4f} max={s['max']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
