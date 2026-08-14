"""Tests for the optional AI configuration fields on Settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings


def test_ai_defaults_to_manual_mode_without_any_credential() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_mode == "manual"
    assert settings.ai_provider == "qwen"
    assert settings.ai_api_key.get_secret_value() == ""


def test_ai_model_defaults_match_verified_qwen_model_ids() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_llm_model == "qwen3.7-plus"
    assert settings.ai_llm_model_hard == "qwen3.8-max"
    assert settings.ai_embedding_model == "qwen3.7-text-embedding"
    assert settings.ai_rerank_model == "qwen3-rerank"
    assert settings.ai_timeout_seconds == 30.0


def test_ai_environment_overrides_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EKB_AI_MODE", "api")
    monkeypatch.setenv("EKB_AI_API_KEY", "sk-test-0123456789")
    monkeypatch.setenv("EKB_AI_LLM_MODEL", "qwen3.8-max")
    monkeypatch.setenv("EKB_AI_TIMEOUT_SECONDS", "45")

    settings = Settings(_env_file=None)

    assert settings.ai_mode == "api"
    assert settings.ai_api_key.get_secret_value() == "sk-test-0123456789"
    assert settings.ai_llm_model == "qwen3.8-max"
    assert settings.ai_timeout_seconds == 45.0


def test_ai_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(ai_mode="auto", _env_file=None)  # type: ignore[arg-type]


def test_ai_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(ai_timeout_seconds=0, _env_file=None)


def test_api_key_is_masked_in_repr_and_str() -> None:
    secret = "sk-secret-should-never-appear"
    settings = Settings(ai_api_key=secret, _env_file=None)

    rendered_views = (
        repr(settings),
        str(settings),
        repr(settings.ai_api_key),
        str(settings.ai_api_key),
    )
    for rendered in rendered_views:
        assert secret not in rendered
    assert settings.ai_api_key.get_secret_value() == secret


def test_formal_settings_loader_refreshes_ai_fields_after_cache_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EKB_AI_MODE", "api")
    get_settings.cache_clear()

    try:
        assert get_settings().ai_mode == "api"

        monkeypatch.delenv("EKB_AI_MODE")
        get_settings.cache_clear()
        assert get_settings().ai_mode == "manual"
    finally:
        get_settings.cache_clear()
