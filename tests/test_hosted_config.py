"""Env-only Hosted settings and no-DB/no-network startup foundation tests."""

from __future__ import annotations

import os
import socket
import sqlite3
import traceback
import urllib.request
from pathlib import Path

import pytest
from pydantic_settings.sources import DotEnvSettingsSource

import src.config as local_config
import src.hosted_config as hosted
from src.runtime_profile import (
    RuntimeConfigurationError,
    RuntimeConfigurationErrorCode,
    RuntimeProfile,
)


def test_security_defaults_are_bounded(tmp_path: Path) -> None:
    settings = hosted.HostedSettings(runtime_profile="hosted", data_root=tmp_path)
    assert settings.agent_rate_limit_per_minute == 10
    assert settings.source_rate_limit_per_minute == 60
    assert settings.max_active_agent_runs == 4
    assert settings.cors_allowed_origins == ()
    assert settings.trusted_proxy_cidrs == ()


@pytest.mark.parametrize("field", ["agent_rate_limit_per_minute", "source_rate_limit_per_minute"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "", "unlimited", "1.0"])
def test_invalid_security_rate_fails_closed(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(RuntimeConfigurationError):
        hosted.HostedSettings(runtime_profile="hosted", data_root=tmp_path, **{field: value})


@pytest.mark.parametrize("value", [0, 9, -1, True, "", "1.5"])
def test_invalid_active_run_config_fails_closed(tmp_path: Path, value: object) -> None:
    with pytest.raises(RuntimeConfigurationError):
        hosted.HostedSettings(
            runtime_profile="hosted", data_root=tmp_path, max_active_agent_runs=value
        )


@pytest.mark.parametrize("value", [1, 4, 8])
def test_valid_active_run_range(tmp_path: Path, value: int) -> None:
    settings = hosted.HostedSettings(
        runtime_profile="hosted", data_root=tmp_path, max_active_agent_runs=value
    )
    assert settings.max_active_agent_runs == value


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "example.com",
        "https://*.example.com",
        "https://example.com/path",
        "https://example.com/",
        "https://example.com?q=x",
        "https://example.com?",
        "https://example.com#x",
        "https://example.com#",
        "https://user:pass@example.com",
        "file:///tmp/x",
        "http://example.com",
        "https://example.com:0",
        "https://example.com:65536",
        "https://example.com:",
        "https://evil\n.example.com",
        "https://bad_host.example",
        "https://example.com,,https://demo.example.com",
    ],
)
def test_invalid_cors_origin_fails_closed(tmp_path: Path, origin: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(
            runtime_profile="hosted", data_root=tmp_path, cors_allowed_origins=origin
        )
    assert origin not in str(caught.value)


@pytest.mark.parametrize(
    "cidr",
    [
        "*",
        "garbage",
        "10.0.0.0/33",
        "2001:db8::/129",
        "10.0.0.1/24",
        "fe80::1%eth0",
        "10.0.0.0/8,,::1",
    ],
)
def test_invalid_proxy_config_fails_closed(tmp_path: Path, cidr: str) -> None:
    with pytest.raises(RuntimeConfigurationError):
        hosted.HostedSettings(
            runtime_profile="hosted", data_root=tmp_path, trusted_proxy_cidrs=cidr
        )


def test_security_env_only_allowlists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EKB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EKB_AGENT_RATE_LIMIT_PER_MINUTE", "12")
    monkeypatch.setenv("EKB_SOURCE_RATE_LIMIT_PER_MINUTE", "70")
    monkeypatch.setenv("EKB_MAX_ACTIVE_AGENT_RUNS", "8")
    origins = (
        "https://example.com, https://demo.example.com,http://localhost:5173,http://[::1]:8000"
    )
    monkeypatch.setenv("EKB_CORS_ALLOWED_ORIGINS", origins)
    monkeypatch.setenv("EKB_TRUSTED_PROXY_CIDRS", "10.0.0.0/8,2001:db8::/32,127.0.0.1")
    settings = hosted.load_hosted_settings()
    assert settings.agent_rate_limit_per_minute == 12
    assert settings.source_rate_limit_per_minute == 70
    assert settings.max_active_agent_runs == 8
    assert settings.cors_allowed_origins == tuple(item.strip() for item in origins.split(","))
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "2001:db8::/32", "127.0.0.1/32")


@pytest.fixture(autouse=True)
def _isolated_no_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in tuple(os.environ):
        if name.startswith("EKB_") or name == "DASHSCOPE_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", "hosted")
    monkeypatch.setitem(local_config.Settings.model_config, "env_file", tmp_path / ".env")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("WP1 must not open SQLite, use the network, or read dotenv")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", forbidden)


def test_valid_hosted_paths_no_persistent_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EKB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EKB_AI_API_KEY", "TEST_ONLY_FAKE_KEY")
    settings = hosted.load_hosted_settings()
    before = set(tmp_path.iterdir())
    hosted.validate_hosted_startup(settings)
    assert settings.runtime_profile is RuntimeProfile.HOSTED
    assert settings.data_root == tmp_path.resolve()
    assert settings.database_dir == tmp_path / "database"
    assert settings.database_path == tmp_path / "database/knowledge.db"
    assert settings.logs_dir == tmp_path / "logs"
    assert settings.log_path == tmp_path / "logs/engineering-kb.log"
    assert settings.ai_api_key.get_secret_value() == "TEST_ONLY_FAKE_KEY"
    assert set(tmp_path.iterdir()) == before
    assert "TEST_ONLY_FAKE_KEY" not in repr(settings)
    assert "TEST_ONLY_FAKE_KEY" not in str(settings)
    assert "TEST_ONLY_FAKE_KEY" not in settings.model_dump_json()


def test_hosted_requires_data_root_no_local_fallback() -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.load_hosted_settings()
    assert caught.value.code is RuntimeConfigurationErrorCode.MISSING_DATA_ROOT


@pytest.mark.parametrize("value", [None, "local", "", " ", "HOSTED", "Hosted", "cloud"])
def test_hosted_loader_requires_exact_explicit_process_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    else:
        monkeypatch.setenv("EKB_RUNTIME_PROFILE", value)
    monkeypatch.setenv("EKB_DATA_ROOT", str(tmp_path))
    with pytest.raises(RuntimeConfigurationError):
        hosted.load_hosted_settings()


@pytest.mark.parametrize("process_profile", [None, "hosted"])
def test_repository_and_cwd_dotenv_cannot_supply_hosted_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, process_profile: str | None
) -> None:
    repository = tmp_path / "source"
    working_dir = tmp_path / "cwd"
    repository.mkdir()
    working_dir.mkdir()
    payload = (
        f"EKB_RUNTIME_PROFILE=hosted\nEKB_DATA_ROOT={tmp_path / 'storage'}\n"
        "EKB_AI_API_KEY=TEST_ONLY_FAKE_DOTENV_KEY\n"
    )
    for directory in (repository, working_dir):
        (directory / ".env").write_text(payload, encoding="utf-8")
    monkeypatch.chdir(working_dir)
    monkeypatch.setitem(local_config.Settings.model_config, "env_file", repository / ".env")
    if process_profile is None:
        monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    with pytest.raises(RuntimeConfigurationError):
        hosted.load_hosted_settings()
    with pytest.raises(RuntimeConfigurationError):
        hosted.HostedSettings(_env_file=repository / ".env")


def test_environment_authority_and_no_dotenv_or_dashscope_secret_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "EKB_RUNTIME_PROFILE=local\nEKB_DATA_ROOT=TEST_ONLY_FAKE_PATH\n"
        "EKB_AI_API_KEY=TEST_ONLY_FAKE_DOTENV_KEY\n", encoding="utf-8"
    )
    monkeypatch.setenv("EKB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "TEST_ONLY_FAKE_DASHSCOPE_KEY")
    settings = hosted.HostedSettings(_env_file=dotenv)
    assert settings.runtime_profile is RuntimeProfile.HOSTED
    assert settings.data_root == tmp_path.resolve()
    assert settings.ai_api_key.get_secret_value() == ""
    hosted.validate_hosted_startup(settings)  # Missing key is not WP1 startup failure.


def test_explicit_server_injection_has_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    monkeypatch.setenv("EKB_DATA_ROOT", "TEST_ONLY_FAKE_ENV_PATH")
    settings = hosted.HostedSettings(runtime_profile="hosted", data_root=tmp_path)
    assert settings.data_root == tmp_path.resolve()
    hosted.validate_hosted_startup(settings)


@pytest.mark.parametrize("value", ["", " ", "\0", "bad\0path", 12, [], None])
def test_invalid_data_root_shape(value: object) -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(data_root=value)
    assert caught.value.code is RuntimeConfigurationErrorCode.INVALID_DATA_ROOT


@pytest.mark.parametrize("relative", [".", "data", "data/database"])
def test_source_tree_is_not_hosted_mutable_storage(relative: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(data_root=local_config.PROJECT_ROOT / relative)
    assert caught.value.code is RuntimeConfigurationErrorCode.DATA_ROOT_IN_SOURCE_TREE


def test_relative_root_resolved_once_without_creating_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "storage"
    root.mkdir()
    settings = hosted.HostedSettings(data_root="storage")
    monkeypatch.chdir(local_config.PROJECT_ROOT)
    hosted.validate_hosted_startup(settings)
    assert settings.data_root == root.resolve()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "file", "file_parent", "database_file", "logs_file"])
def test_unusable_data_root_is_rejected_without_creating_directories(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "root"
    if kind in {"file", "file_parent"}:
        root.write_text("TEST_ONLY_FAKE_CONTENT", encoding="utf-8")
    elif kind in {"database_file", "logs_file"}:
        root.mkdir()
        (root / kind.removesuffix("_file")).write_text("TEST_ONLY_FAKE", encoding="utf-8")
    if kind == "file_parent":
        root /= "child"
    settings = hosted.HostedSettings(data_root=root)
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.validate_hosted_startup(settings)
    assert caught.value.code is RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
    assert not any(tmp_path.rglob(".ekb-wp1-*"))


def test_existing_child_directories_probe_without_opening_db(tmp_path: Path) -> None:
    (tmp_path / "database").mkdir()
    (tmp_path / "logs").mkdir()
    database = tmp_path / "database/knowledge.db"
    database.write_bytes(b"TEST_ONLY_FAKE_NON_SQLITE_BYTES")
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    hosted.validate_hosted_startup(hosted.HostedSettings(data_root=tmp_path))
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    assert database.read_bytes() == b"TEST_ONLY_FAKE_NON_SQLITE_BYTES"


def test_unwritable_probe_error_is_stable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("TEST_ONLY_FAKE_SECRET_AND_PRIVATE_PATH")

    monkeypatch.setattr(hosted, "NamedTemporaryFile", denied)
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.validate_hosted_startup(hosted.HostedSettings(data_root=tmp_path))
    assert caught.value.code is RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
    assert "TEST_ONLY_FAKE_SECRET_AND_PRIVATE_PATH" not in "".join(
        traceback.format_exception_only(caught.type, caught.value)
    )
    assert caught.value.__suppress_context__


def test_config_error_does_not_echo_secret_input() -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(
            runtime_profile="TEST_ONLY_FAKE_SECRET", data_root="\0",
            ai_api_key="TEST_ONLY_FAKE_KEY",
        )
    assert "TEST_ONLY_FAKE" not in str(caught.value)
    assert "TEST_ONLY_FAKE" not in repr(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("value", [[], {"key": "TEST_ONLY_FAKE_KEY"}])
def test_invalid_secret_configuration_has_safe_typed_error(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(data_root=tmp_path, ai_api_key=value)
    assert caught.value.code is RuntimeConfigurationErrorCode.INVALID_HOSTED_CONFIG
    assert "TEST_ONLY_FAKE_KEY" not in repr(caught.value)


def test_path_resolution_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def denied(*args: object, **kwargs: object) -> Path:
        raise OSError("TEST_ONLY_FAKE_PRIVATE_PATH")

    monkeypatch.setattr(Path, "resolve", denied)
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.HostedSettings(data_root=tmp_path)
    assert caught.value.code is RuntimeConfigurationErrorCode.INVALID_DATA_ROOT
    assert "TEST_ONLY_FAKE_PRIVATE_PATH" not in repr(caught.value)


def test_resolved_child_escape_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simulate a directory symlink without platform-dependent symlink privileges.
    settings = hosted.HostedSettings(data_root=tmp_path)
    resolve = Path.resolve

    def redirected(path: Path, *args: object, **kwargs: object) -> Path:
        if path == settings.database_dir:
            return tmp_path.parent / "TEST_ONLY_FAKE_EXTERNAL_TARGET"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(RuntimeConfigurationError) as caught:
        hosted.validate_hosted_startup(settings)
    assert caught.value.code is RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
    assert list(tmp_path.iterdir()) == []
