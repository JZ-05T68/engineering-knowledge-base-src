"""Tests for the optional AI provider factory in the application runtime."""

from __future__ import annotations

import pytest

import src.runtime as runtime
from src.ai.provider import AIExecutionError
from src.ai.qwen_client import QwenProvider, QwenTransportError
from src.config import Settings


@pytest.fixture(autouse=True)
def _clear_ai_provider_cache():
    runtime.application_ai_provider.cache_clear()
    yield
    runtime.application_ai_provider.cache_clear()


def _stub_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[arg-type]
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    return settings


def test_manual_mode_disables_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_settings(monkeypatch, ai_api_key="sk-present-but-manual")

    assert runtime.application_ai_provider() is None


def test_api_mode_without_key_disables_ai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api")

    assert runtime.application_ai_provider() is None


def test_api_mode_with_key_builds_qwen_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")

    provider = runtime.application_ai_provider()

    assert isinstance(provider, QwenProvider)
    assert provider.is_configured


def test_application_provider_forces_zero_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EKB runtime policy pins max_extra_attempts to 0 for the app.

    The Qwen library default stays 2; only the runtime factory's own provider
    is pinned. Verified through observable behaviour: a provider with zero
    retry calls the transport exactly once and raises on the first transient
    failure instead of retrying.
    """
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")

    provider = runtime.application_ai_provider()
    assert isinstance(provider, QwenProvider)

    calls: list[int] = []

    def _failing_transport(url, headers, payload, timeout_seconds):
        calls.append(1)
        raise QwenTransportError("transient", status_code=503)

    provider._transport = _failing_transport
    with pytest.raises(AIExecutionError):
        provider.embed(("query",), model="qwen3.7-text-embedding", dimensions=1024)

    assert len(calls) == 1


def test_provider_factory_is_cached_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")

    assert runtime.application_ai_provider() is runtime.application_ai_provider()


def test_ai_factory_never_touches_existing_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI is optional: building it must not initialize the database or OCR."""

    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")
    monkeypatch.setattr(
        runtime,
        "application_database",
        lambda: pytest.fail("AI 工厂不应触碰数据库"),
    )
    monkeypatch.setattr(
        runtime,
        "application_ocr_engine",
        lambda: pytest.fail("AI 工厂不应触碰 OCR 引擎"),
    )

    assert isinstance(runtime.application_ai_provider(), QwenProvider)
