"""Tests for the optional AI provider factory in the application runtime."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import src.runtime as runtime
from src.ai.provider import (
    AIExecutionError,
    AIProductionCompositionError,
    AuditedAIProvider,
)
from src.ai.qwen_client import QwenProvider, QwenTransportError
from src.config import Settings


@pytest.fixture(autouse=True)
def _clear_ai_provider_cache():
    def clear() -> None:
        for target in (
            runtime.application_ai_provider,
            runtime.application_experience_model_service,
        ):
            if hasattr(target, "cache_clear"):
                target.cache_clear()

    clear()
    yield
    clear()


def _stub_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[arg-type]
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    return settings


def test_manual_mode_disables_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_settings(monkeypatch, ai_api_key="sk-present-but-manual")
    constructor = Mock(side_effect=AssertionError("manual mode must not construct Qwen"))
    monkeypatch.setattr(runtime, "QwenProvider", constructor)

    assert runtime.application_ai_provider() is None
    constructor.assert_not_called()


def test_api_mode_without_key_disables_ai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api")
    constructor = Mock(side_effect=AssertionError("missing key must not construct Qwen"))
    monkeypatch.setattr(runtime, "QwenProvider", constructor)

    assert runtime.application_ai_provider() is None
    constructor.assert_not_called()


def test_api_mode_with_key_builds_audited_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")
    constructor = Mock(wraps=QwenProvider)
    monkeypatch.setattr(runtime, "QwenProvider", constructor)

    provider = runtime.application_ai_provider()

    assert isinstance(provider, AuditedAIProvider)
    assert provider.is_configured
    assert not hasattr(provider, "wrapped")
    constructor.assert_called_once()
    assert isinstance(provider._ledger, runtime._LazyDatabaseAiCallLedger)
    assert isinstance(provider._budget_guard, runtime._LazyTokenBudgetGuard)


def test_application_provider_uses_configured_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry policy comes from Settings.ai_max_extra_attempts, never a hard-coded 0."""

    _stub_settings(
        monkeypatch,
        ai_mode="api",
        ai_api_key="sk-runtime-test",
        ai_max_extra_attempts=0,
    )

    calls: list[int] = []

    def _failing_transport(url, headers, payload, timeout_seconds):
        calls.append(1)
        raise QwenTransportError("transient", status_code=503)

    database = Mock()
    monkeypatch.setattr(runtime, "urllib_transport", _failing_transport)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    provider = runtime.application_ai_provider()
    assert isinstance(provider, AuditedAIProvider)
    with pytest.raises(AIExecutionError):
        provider.embed(("query",), model="qwen3.7-text-embedding", dimensions=1024)

    assert len(calls) == 1
    database.insert_ai_call.assert_called_once()


def test_application_provider_default_retry_policy_is_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")
    constructor = Mock(wraps=QwenProvider)
    monkeypatch.setattr(runtime, "QwenProvider", constructor)

    provider = runtime.application_ai_provider()
    assert isinstance(provider, AuditedAIProvider)
    assert constructor.call_args.kwargs["max_extra_attempts"] == 2


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

    assert isinstance(runtime.application_ai_provider(), AuditedAIProvider)


def test_local_factory_fails_closed_if_production_builder_returns_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_settings(monkeypatch, ai_mode="api", ai_api_key="sk-runtime-test")
    raw = Mock()
    monkeypatch.setattr(
        runtime, "build_production_audited_provider", lambda *args, **kwargs: raw
    )

    with pytest.raises(AIProductionCompositionError):
        runtime.application_ai_provider()

    raw.complete.assert_not_called()


def test_experience_production_composition_rejects_raw_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = Mock()
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: raw)

    with pytest.raises(AIProductionCompositionError):
        runtime.application_experience_model_service()

    raw.complete.assert_not_called()
