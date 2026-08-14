"""One-shot, human-confirmed, paid Qwen smoke call (v0.5.0 Phase 2).

This script is the only entry point that may emit a real AI API request in
this phase. It is never imported or triggered by pytest, release_check,
application startup, or the service manager.

Hard rules:

- Without ``--confirm-paid-call`` it refuses to touch the network.
- With the flag it still refuses unless every guard holds: api mode, Qwen
  provider, configured API key, model ``qwen3.7-plus``, thinking disabled,
  exactly 0 extra attempts, ``max_completion_tokens`` 64.
- At most ONE real HTTP inference request is ever sent. There is no retry,
  no fallback model, and no second call on failure or on an unexpected
  answer.
- The API key is never printed, logged, or included in any output.

Usage::

    python scripts/ai_smoke_test.py                     # dry audit, no call
    python scripts/ai_smoke_test.py --confirm-paid-call # one paid request
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.provider import AIError  # noqa: E402
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.config import OfficialEndpointError, Settings, get_settings  # noqa: E402

SMOKE_MODEL: Final[str] = "qwen3.7-plus"
SMOKE_PROMPT: Final[str] = "只回复：EKB_QWEN_SMOKE_OK"
SMOKE_MAX_COMPLETION_TOKENS: Final[int] = 64
SMOKE_EXTRA_ATTEMPTS: Final[int] = 0


def check_smoke_guards(settings: Settings) -> list[str]:
    """Return every violated paid-call guard; empty means the call is allowed."""

    problems: list[str] = []
    if settings.ai_mode != "api":
        problems.append(f'ai_mode 必须为 "api"（当前 {settings.ai_mode}）')
    if settings.ai_provider != "qwen":
        problems.append(f'ai_provider 必须为 "qwen"（当前 {settings.ai_provider}）')
    if not settings.ai_api_key.get_secret_value():
        problems.append("EKB_AI_API_KEY is not configured")
    if settings.ai_llm_model != SMOKE_MODEL:
        problems.append(
            f"ai_llm_model 必须为 {SMOKE_MODEL}（当前 {settings.ai_llm_model}）"
        )
    return problems


def print_pre_call_audit(settings: Settings, *, key_present: bool) -> None:
    """Print the pre-call audit. The API key itself is never shown."""

    import subprocess

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
    print(f"model: {SMOKE_MODEL}")
    print("thinking: OFF (enable_thinking=false)")
    print(f"max_completion_tokens: {SMOKE_MAX_COMPLETION_TOKENS}")
    print(f"max_extra_attempts: {SMOKE_EXTRA_ATTEMPTS}")
    print(f"API Key present: {'YES' if key_present else 'NO'}")
    print("endpoint host: dashscope.aliyuncs.com (compatible-mode)")
    print("estimated maximum real HTTP requests: 1")
    print("-----------------------------")


def run_smoke(settings: Settings) -> int:
    """Execute exactly one paid smoke call and report observability data."""

    provider = QwenProvider(
        api_key=settings.ai_api_key.get_secret_value(),
        llm_model=settings.ai_llm_model,
        llm_model_hard=settings.ai_llm_model_hard,
        embedding_model=settings.ai_embedding_model,
        rerank_model=settings.ai_rerank_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_extra_attempts=SMOKE_EXTRA_ATTEMPTS,
        enable_thinking=False,
        transport=urllib_transport,
    )
    started = time.perf_counter()
    try:
        result = provider.complete(
            SMOKE_PROMPT,
            model=SMOKE_MODEL,
            max_completion_tokens=SMOKE_MAX_COMPLETION_TOKENS,
        )
    except AIError as exc:
        latency = time.perf_counter() - started
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print(f"latency: {latency:.3f}s")
        print("按成本护栏约定：不重试、不发起第二次请求。")
        return 1
    latency = time.perf_counter() - started
    print("PASS")
    print(f"model: {result.model}")
    print(f"response text: {result.text!r}")
    if result.usage is None:
        print("usage: unavailable")
    else:
        print(
            f"usage: prompt={result.usage.prompt_tokens} "
            f"completion={result.usage.completion_tokens} "
            f"total={result.usage.total_tokens}"
        )
    print(f"latency: {latency:.3f}s")
    print(f"finish reason: {result.finish_reason or 'unavailable'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EKB v0.5.0 Qwen 单次付费 smoke 调用")
    parser.add_argument(
        "--confirm-paid-call",
        action="store_true",
        help="确认执行恰好一次按量付费的真实 API 请求",
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
    problems = check_smoke_guards(settings)
    if problems:
        for problem in problems:
            print(f"GUARD FAIL: {problem}")
        if not key_present:
            print("EKB_AI_API_KEY is not configured; real smoke call not executed.")
        print("未发起任何网络请求。")
        return 3

    return run_smoke(settings)


if __name__ == "__main__":
    raise SystemExit(main())
