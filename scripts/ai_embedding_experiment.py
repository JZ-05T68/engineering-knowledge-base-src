"""One-shot, human-confirmed, paid Qwen embedding experiment (v0.5.0 Phase 3).

Goal: verify that ``qwen3.7-text-embedding`` produces vectors through the
EKB provider boundary and that a tiny, fully in-memory, synthetic candidate
set yields a sensible semantic ranking. This is a baseline experiment, not
production retrieval: no vector store, no persistence, no real user data.

Hard rules:

- Without ``--confirm-paid-call`` it refuses to touch the network.
- With the flag it still refuses unless every guard holds: api mode, Qwen
  provider, configured API key, model ``qwen3.7-text-embedding``,
  dimensions 1024, batch size 8, 0 extra attempts.
- At most ONE real HTTP request: all 8 fixed synthetic texts (Q0, Q1,
  D0..D5) go in a single batch embedding call. No retry, no second call,
  no per-text calls, regardless of outcome.
- Only the 8 fixed non-private synthetic strings below are ever sent.
  Nothing is read from the database, data directories, PDFs, notes, or
  evidence. The API key is never printed or logged.
- All similarity computation happens locally after the single call.

Usage::

    python scripts/ai_embedding_experiment.py                     # dry audit
    python scripts/ai_embedding_experiment.py --confirm-paid-call # one paid call
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.provider import AIError  # noqa: E402
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.config import OfficialEndpointError, Settings, get_settings  # noqa: E402

EXPERIMENT_MODEL: Final[str] = "qwen3.7-text-embedding"
EXPERIMENT_DIMENSIONS: Final[int] = 1024
EXPERIMENT_EXTRA_ATTEMPTS: Final[int] = 0

QUERIES: Final[tuple[str, ...]] = (
    "误删工程文档后应该怎样恢复？",
    "怎样避免证据包继续引用已经失效的来源？",
)

DOCUMENTS: Final[tuple[str, ...]] = (
    "删除文档前应创建备份点，恢复流程从可验证备份中还原，并检查数据库完整性。",
    "搜索服务使用关键词匹配和 BM25 排序返回命中页面，并保留来源页码和文档定位信息。",
    "笔记可以设置重点、次重点和一般的重要性等级，并在列表中按重要性筛选。",
    "PDF 导入时先计算 SHA-256 用于重复检测，再将原始文件安全保存并逐页提取文本。",
    "AI 默认采用 manual 模式，未配置 API Key 时仍可使用全部既有离线核心功能。",
    "证据篮导出前会重新校验证据来源和确认状态，"
    "来源失效或输入变化时通过 freshness 机制阻止过期证据包继续使用。",
)

# Expected Top-1 document index per query, fixed by human judgment.
EXPECTED_TOP1: Final[dict[int, int]] = {0: 0, 1: 5}

BATCH_TEXTS: Final[tuple[str, ...]] = QUERIES + DOCUMENTS


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two equal-length, non-zero vectors."""

    if len(a) != len(b):
        raise ValueError(f"向量维度不一致：{len(a)} vs {len(b)}")
    if not a:
        raise ValueError("向量不能为空")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(math.fsum(x * x for x in a))
    norm_b = math.sqrt(math.fsum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("零向量无法计算余弦相似度")
    return dot / (norm_a * norm_b)


def rank_documents(
    query_vector: Sequence[float],
    document_vectors: Sequence[Sequence[float]],
) -> tuple[tuple[int, float], ...]:
    """Rank documents by descending similarity; exact ties keep input order."""

    scored = tuple(
        (index, cosine_similarity(query_vector, vector))
        for index, vector in enumerate(document_vectors)
    )
    return tuple(sorted(scored, key=lambda item: (-item[1], item[0])))


def check_experiment_guards(settings: Settings) -> list[str]:
    """Return every violated paid-call guard; empty means the call is allowed."""

    problems: list[str] = []
    if settings.ai_mode != "api":
        problems.append(f'ai_mode 必须为 "api"（当前 {settings.ai_mode}）')
    if settings.ai_provider != "qwen":
        problems.append(f'ai_provider 必须为 "qwen"（当前 {settings.ai_provider}）')
    if not settings.ai_api_key.get_secret_value():
        problems.append("EKB_AI_API_KEY is not configured")
    if settings.ai_embedding_model != EXPERIMENT_MODEL:
        problems.append(
            f"ai_embedding_model 必须为 {EXPERIMENT_MODEL}"
            f"（当前 {settings.ai_embedding_model}）"
        )
    return problems


def print_pre_call_audit(settings: Settings, *, key_present: bool) -> None:
    """Print the pre-call audit. The API key itself is never shown."""

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    print("--- Pre-call safety audit ---")
    print(f"branch: {branch}")
    print(f"HEAD: {head}")
    print(f"ai_mode: {settings.ai_mode}")
    print(f"provider: {settings.ai_provider}")
    print(f"API Key present: {'YES' if key_present else 'NO'}")
    print(f"embedding model: {EXPERIMENT_MODEL}")
    print(f"dimensions: {EXPERIMENT_DIMENSIONS}")
    print(f"input count: {len(BATCH_TEXTS)} (single batch)")
    print(f"max_extra_attempts: {EXPERIMENT_EXTRA_ATTEMPTS}")
    print("endpoint host: dashscope.aliyuncs.com (compatible-mode)")
    print("estimated maximum real HTTP requests: 1")
    print("-----------------------------")


def run_experiment(settings: Settings) -> int:
    """Execute exactly one paid batch embedding call, then rank locally."""

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
    started = time.perf_counter()
    try:
        result = provider.embed(
            BATCH_TEXTS, model=EXPERIMENT_MODEL, dimensions=EXPERIMENT_DIMENSIONS
        )
    except AIError as exc:
        latency = time.perf_counter() - started
        print(f"CALL FAIL: {type(exc).__name__}: {exc}")
        print(f"latency: {latency:.3f}s")
        print("按成本护栏约定：不重试、不发起第二次请求。")
        return 1
    latency = time.perf_counter() - started

    print("CALL PASS")
    print(f"returned model: {result.model}")
    print(f"vector count: {len(result.embeddings)}")
    print(f"vector dimension: {len(result.embeddings[0])}")
    if result.usage is None:
        print("usage: unavailable")
    else:
        print(
            f"usage: prompt_tokens={result.usage.prompt_tokens} "
            f"total_tokens={result.usage.total_tokens}"
        )
    print(f"latency: {latency:.3f}s")
    print("网络阶段结束；以下相似度与排序全部在本地计算。")

    query_vectors = result.embeddings[: len(QUERIES)]
    document_vectors = result.embeddings[len(QUERIES) :]
    all_top1_correct = True
    for query_index, query in enumerate(QUERIES):
        ranking = rank_documents(query_vectors[query_index], document_vectors)
        print(f"\n=== Q{query_index}: {query} ===")
        for rank, (doc_index, similarity) in enumerate(ranking, start=1):
            print(f"Rank {rank}  D{doc_index}  {similarity:.4f}")
        top1_index, top1_score = ranking[0]
        top2_score = ranking[1][1]
        expected = EXPECTED_TOP1[query_index]
        verdict = "OK" if top1_index == expected else "UNEXPECTED"
        print(
            f"Top-1: D{top1_index} ({top1_score:.4f}) | "
            f"Top-2: D{ranking[1][0]} ({top2_score:.4f}) | "
            f"margin: {top1_score - top2_score:.4f} | "
            f"expected D{expected}: {verdict}"
        )
        if top1_index != expected:
            all_top1_correct = False

    if all_top1_correct:
        print("\nRETRIEVAL VERDICT: PASS（Q0→D0、Q1→D5 均为 Top-1）")
        return 0
    print("\nRETRIEVAL VERDICT: FAIL（存在非预期 Top-1；按约定不再发起请求）")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EKB v0.5.0 Qwen embedding 单次付费内存召回实验"
    )
    parser.add_argument(
        "--confirm-paid-call",
        action="store_true",
        help="确认执行恰好一次按量付费的真实 batch embedding 请求",
    )
    arguments = parser.parse_args(argv)

    if not arguments.confirm_paid_call:
        print("SKIPPED: 这是一次按量付费的真实 API 调用入口。")
        print("未提供 --confirm-paid-call，不发起任何网络请求。")
        return 2

    try:
        settings = get_settings()
    except OfficialEndpointError as exc:
        print(f"配置错误：{exc}")
        return 3

    key_present = bool(settings.ai_api_key.get_secret_value())
    print_pre_call_audit(settings, key_present=key_present)
    problems = check_experiment_guards(settings)
    if problems:
        for problem in problems:
            print(f"GUARD FAIL: {problem}")
        if not key_present:
            print("EKB_AI_API_KEY is not configured; real experiment call not executed.")
        print("未发起任何网络请求。")
        return 3

    return run_experiment(settings)


if __name__ == "__main__":
    raise SystemExit(main())
