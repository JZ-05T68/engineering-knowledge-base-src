"""Locks the suite-wide developer-environment isolation (audit R-02 / TD-01).

The checkout's real ``.env`` carries AI credentials and the developer session
may carry sensitive environment variables. The autouse fixture in
``tests/conftest.py`` must keep ordinary tests deterministic and secret-free
without disabling explicit environment testing. These tests prove the contract
with synthetic sentinels only; the real key is never read or printed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import OFFICIAL_PORT, Settings, get_settings

_SENSITIVE_ENV_PREFIXES = ("EKB_", "DASHSCOPE_")
_SENTINEL_KEY = "sentinel-not-a-real-key"


def test_ordinary_settings_never_consume_the_real_env_file() -> None:
    """Settings() must show defaults even though the checkout .env has secrets.

    This is also the proof that test isolation cannot accidentally enable real
    AI: the real ``.env`` sets ``EKB_AI_MODE=api`` plus a key, and an ordinary
    test construction must still resolve to manual mode with an empty key.
    """

    settings = Settings()

    assert settings.ai_mode == "manual"
    assert settings.ai_api_key.get_secret_value() == ""
    assert settings.host == "127.0.0.1"
    assert settings.port == OFFICIAL_PORT


def test_ambient_sensitive_environment_variables_are_sanitized() -> None:
    """No developer EKB_*/DASHSCOPE_* variable may survive into a test body."""

    leaked = [
        name for name in os.environ if name.startswith(_SENSITIVE_ENV_PREFIXES)
    ]
    assert leaked == []


def test_explicit_opt_in_dotenv_loading_still_works(tmp_path: Path) -> None:
    """Tests that deliberately verify dotenv loading opt in via ``_env_file``."""

    env_file = tmp_path / "test-only.env"
    env_file.write_text(
        f"EKB_AI_MODE=api\nEKB_AI_API_KEY={_SENTINEL_KEY}\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.ai_mode == "api"
    assert settings.ai_api_key.get_secret_value() == _SENTINEL_KEY


def test_monkeypatched_environment_values_still_reach_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that set their own variables after isolation keep working."""

    monkeypatch.setenv("EKB_AI_MODE", "api")
    monkeypatch.setenv("EKB_AI_API_KEY", _SENTINEL_KEY)

    settings = Settings(_env_file=None)

    assert settings.ai_mode == "api"
    assert settings.ai_api_key.get_secret_value() == _SENTINEL_KEY


def test_formal_settings_loader_still_enforces_endpoint_under_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() keeps reading os.environ; only the dotenv file is off."""

    from src.config import OfficialEndpointError

    monkeypatch.setenv("EKB_PORT", "49344")
    get_settings.cache_clear()
    try:
        with pytest.raises(OfficialEndpointError, match="127.0.0.1:8501"):
            get_settings()
    finally:
        get_settings.cache_clear()
