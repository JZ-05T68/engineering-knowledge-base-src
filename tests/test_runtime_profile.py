"""Strict profile parsing and preservation of the existing Local entrypoints."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import src.config as config
from src.runtime_profile import (
    RuntimeConfigurationError,
    RuntimeConfigurationErrorCode,
    RuntimeProfile,
    get_runtime_profile,
    parse_runtime_profile,
)


@pytest.fixture(autouse=True)
def _isolated_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in tuple(os.environ):
        if name.startswith("EKB_") or name == "DASHSCOPE_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setitem(config.Settings.model_config, "env_file", tmp_path / ".env")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, RuntimeProfile.LOCAL), ("local", RuntimeProfile.LOCAL),
     ("hosted", RuntimeProfile.HOSTED)],
)
def test_exact_profile_contract(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: RuntimeProfile
) -> None:
    if value is not None:
        monkeypatch.setenv("EKB_RUNTIME_PROFILE", value)
    assert parse_runtime_profile(value) is expected
    assert get_runtime_profile() is expected
    assert json.loads(json.dumps(expected)) == expected.value


@pytest.mark.parametrize(
    "value",
    ["", " ", "\t", "\n", "local ", " hosted", "LOCAL", "Local", "HOSTED", "Hosted",
     "prod", "production", "server", "cloud", "test", "staging", "invalid"],
)
def test_present_invalid_profile_never_defaults_to_local(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", value)
    with pytest.raises(RuntimeConfigurationError) as caught:
        get_runtime_profile()
    assert caught.value.code is RuntimeConfigurationErrorCode.INVALID_RUNTIME_PROFILE


def test_missing_profile_preserves_local_dotenv_and_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "EKB_RUNTIME_PROFILE=hosted\n"
        "EKB_AI_MODE=api\nEKB_AI_API_KEY=TEST_ONLY_FAKE_KEY\n"
        "EKB_AI_LLM_MODEL=TEST_ONLY_FAKE_MODEL\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLOUD_ENVIRONMENT", "true")
    settings = config.runtime_settings()
    assert settings.runtime_profile is RuntimeProfile.LOCAL
    assert settings.ai_api_key.get_secret_value() == "TEST_ONLY_FAKE_KEY"
    assert settings.ai_mode == "api"
    assert settings.ai_llm_model == "TEST_ONLY_FAKE_MODEL"
    assert settings.database_path == config.PROJECT_ROOT / "data/database/knowledge.db"
    assert settings is config.get_settings()
    assert "TEST_ONLY_FAKE_KEY" not in repr(settings)


def test_explicit_local_keeps_environment_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("EKB_AI_MODE=api\n", encoding="utf-8")
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", "local")
    monkeypatch.setenv("EKB_AI_MODE", "manual")
    assert config.runtime_settings().ai_mode == "manual"


@pytest.mark.parametrize("value", ["hosted", "", "HOSTED", "invalid"])
def test_local_entrypoints_reject_wrong_profile_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch, value: str, tmp_path: Path
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Local settings must not be loaded for this profile")

    monkeypatch.setenv("EKB_RUNTIME_PROFILE", value)
    monkeypatch.setenv(config.STAGING_ENV_VAR, "1")
    monkeypatch.setattr(config, "Settings", forbidden)
    for loader in (config.get_settings, config.runtime_settings,
                   lambda: config.staging_settings(tmp_path)):
        with pytest.raises(RuntimeConfigurationError):
            loader()


@pytest.mark.parametrize("value", ["hosted", "", "invalid"])
def test_local_cache_cannot_bypass_profile_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    config.get_settings()
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", value)
    with pytest.raises(RuntimeConfigurationError):
        config.get_settings()


def test_existing_staging_is_local_not_a_third_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", "local")
    settings = config.staging_settings(tmp_path / "staging")
    assert settings.runtime_profile is RuntimeProfile.LOCAL
    assert settings.port == config.STAGING_PORT
    assert settings.database_path.is_relative_to(tmp_path)


def test_configuration_dependency_direction() -> None:
    root = Path(__file__).resolve().parents[1]
    modules = ("config.py", "runtime_profile.py", "hosted_config.py")
    for filename in modules:
        tree = ast.parse((root / "src" / filename).read_text(encoding="utf-8"))
        imports = [
            name
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
        ]
        assert not any(name.split(".")[0] in {"streamlit", "fastapi", "uvicorn"}
                       or name.startswith("src.agent") or name == "src.runtime"
                       for name in imports)
    for path in (root / "src/agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"src.hosted_config", "src.runtime_profile"}
            elif isinstance(node, ast.Import):
                assert not any(alias.name in {"src.hosted_config", "src.runtime_profile"}
                               for alias in node.names)
