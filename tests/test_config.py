"""Tests for the boundary between formal settings and isolated test settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src import __version__
from src.config import (
    OFFICIAL_HOST,
    OFFICIAL_PORT,
    OfficialEndpointError,
    Settings,
    get_settings,
    require_official_endpoint,
)


def test_current_application_version_is_v050() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_title == "工程知识库 v0.5.0"
    assert settings.app_version == "0.5.0"
    assert __version__ == "0.5.0"


def test_official_configuration_accepts_only_loopback_8501() -> None:
    settings = Settings(_env_file=None)

    require_official_endpoint(settings.host, settings.port)

    assert (settings.host, settings.port) == (OFFICIAL_HOST, OFFICIAL_PORT)


def test_formal_settings_loader_rejects_environment_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EKB_PORT", "49340")
    get_settings.cache_clear()

    try:
        with pytest.raises(OfficialEndpointError, match="127.0.0.1:8501"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_official_endpoint_rejects_non_8501_and_non_loopback() -> None:
    with pytest.raises(OfficialEndpointError, match="端口 49341"):
        require_official_endpoint(OFFICIAL_HOST, 49341)
    with pytest.raises(OfficialEndpointError, match="地址 0.0.0.0"):
        require_official_endpoint("0.0.0.0", OFFICIAL_PORT)
    with pytest.raises(ValidationError):
        Settings(host="0.0.0.0", _env_file=None)  # type: ignore[arg-type]


def test_formal_settings_loader_reports_non_loopback_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EKB_HOST", "0.0.0.0")
    get_settings.cache_clear()

    try:
        with pytest.raises(OfficialEndpointError, match="127.0.0.1:8501"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_isolated_tests_can_still_inject_an_explicit_temporary_port() -> None:
    settings = Settings(port=49342, _env_file=None)

    assert settings.host == OFFICIAL_HOST
    assert settings.port == 49342
    with pytest.raises(OfficialEndpointError):
        require_official_endpoint(settings.host, settings.port)
