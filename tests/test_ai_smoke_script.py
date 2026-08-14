"""Guard tests for the paid smoke-call entry point. No network ever."""

from __future__ import annotations

import pytest

import scripts.ai_smoke_test as smoke
from src.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _ready_settings() -> Settings:
    return _settings(ai_mode="api", ai_api_key="sk-smoke-guard-test")


def test_without_confirm_flag_refuses_and_never_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        smoke, "run_smoke", lambda settings: pytest.fail("不应触达真实调用")
    )
    monkeypatch.setattr(
        smoke, "get_settings", lambda: pytest.fail("未确认时不应读取配置")
    )

    exit_code = smoke.main([])

    assert exit_code == 2
    assert "SKIPPED" in capsys.readouterr().out


def test_confirm_flag_with_manual_mode_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        smoke, "run_smoke", lambda settings: pytest.fail("manual 模式不应调用")
    )

    exit_code = smoke.main(["--confirm-paid-call"])

    assert exit_code == 3
    assert "GUARD FAIL" in capsys.readouterr().out


def test_confirm_flag_without_key_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "get_settings", lambda: _settings(ai_mode="api"))
    monkeypatch.setattr(
        smoke, "run_smoke", lambda settings: pytest.fail("无 Key 不应调用")
    )

    exit_code = smoke.main(["--confirm-paid-call"])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "EKB_AI_API_KEY is not configured" in out
    assert "API Key present: NO" in out


def test_confirm_flag_with_wrong_model_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        ai_mode="api", ai_api_key="sk-x", ai_llm_model="qwen3.8-max"
    )
    monkeypatch.setattr(smoke, "get_settings", lambda: settings)
    monkeypatch.setattr(
        smoke, "run_smoke", lambda s: pytest.fail("模型不符不应调用")
    )

    assert smoke.main(["--confirm-paid-call"]) == 3


def test_all_guards_pass_delegates_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _ready_settings()
    monkeypatch.setattr(smoke, "get_settings", lambda: settings)
    calls: list[Settings] = []
    monkeypatch.setattr(
        smoke, "run_smoke", lambda s: calls.append(s) or 0
    )

    exit_code = smoke.main(["--confirm-paid-call"])

    assert exit_code == 0
    assert calls == [settings]
    out = capsys.readouterr().out
    assert "API Key present: YES" in out
    assert "sk-smoke-guard-test" not in out


def test_smoke_provider_is_forced_to_zero_retry_non_thinking() -> None:
    """The paid-call construction must pin the cost guardrails."""

    provider = smoke.QwenProvider(
        api_key="sk-x",
        llm_model=smoke.SMOKE_MODEL,
        llm_model_hard="qwen3.8-max",
        embedding_model="qwen3.7-text-embedding",
        rerank_model="qwen3-rerank",
        max_extra_attempts=smoke.SMOKE_EXTRA_ATTEMPTS,
        enable_thinking=False,
    )
    assert provider._max_extra_attempts == 0
    assert provider._enable_thinking is False
