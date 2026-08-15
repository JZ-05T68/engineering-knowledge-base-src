"""Staging instance lifecycle tests: dual local services, zero interference.

Covers the Phase 10B service-manager extension: independent pid/log/runtime
paths, independent ports, stop operations scoped to exactly one instance,
and the in-process staging settings resolver. No network, no AI calls.
"""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.service_manager as manager
import src.runtime as runtime
from src import config
from src.config import STAGING_ENV_VAR, staging_settings


@pytest.fixture(autouse=True)
def _reset_active_settings(monkeypatch: pytest.MonkeyPatch):
    """Ensure every test starts and ends in production management mode."""

    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", None)
    yield


def _spawn_child() -> subprocess.Popen:
    """Spawn a real venv-python child process for lifecycle tests."""

    python = manager.expected_python()
    return subprocess.Popen(
        [str(python), "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_record(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "project_root": str(manager.PROJECT_ROOT),
                "python": str(manager.expected_python()),
                "port": 8502,
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------- settings wiring
def test_active_settings_defaults_to_production(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(manager, "get_settings", lambda: sentinel)
    assert manager.active_settings() is sentinel


def test_active_settings_staging_when_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = staging_settings(tmp_path / "staging")
    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", staging)
    assert manager.active_settings() is staging


def test_parser_accepts_staging_flag_on_lifecycle_commands() -> None:
    parser = manager.build_parser()
    for command in ("start", "stop", "status"):
        arguments = parser.parse_args([command, "--staging"])
        assert arguments.staging is True
    assert parser.parse_args(["status"]).staging is False


def test_runtime_settings_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGING_ENV_VAR, "1")
    staging = config.runtime_settings()
    assert staging.port == 8502
    assert "staging-data" in str(staging.database_path)

    monkeypatch.delenv(STAGING_ENV_VAR)
    production = config.runtime_settings()
    assert production.port == 8501
    assert "staging-data" not in str(production.database_path)


def test_application_settings_uses_staging_when_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STAGING_ENV_VAR, "1")
    importlib.reload(runtime)  # 绕过 conftest 的 application_settings 防护桩
    try:
        runtime.application_settings.cache_clear()
        settings = runtime.application_settings()
        assert settings.port == 8502
        assert "staging-data" in str(settings.database_path)
    finally:
        runtime.application_settings.cache_clear()
        for handler in logging.getLogger().handlers[:]:
            if "staging" in getattr(handler, "baseFilename", ""):
                logging.getLogger().removeHandler(handler)
        monkeypatch.delenv(STAGING_ENV_VAR)
        importlib.reload(runtime)


# --------------------------------------------------------------- pid isolation
def test_staging_pid_record_is_separate_from_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = staging_settings(tmp_path / "staging")
    production_pid = tmp_path / "prod" / "runtime" / "engineering-kb.pid.json"
    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", staging)

    manager.write_pid_record(12345)

    assert staging.pid_path.is_file()
    record = json.loads(staging.pid_path.read_text(encoding="utf-8"))
    assert record["pid"] == 12345
    assert record["port"] == 8502
    assert not production_pid.exists()


def test_stop_staging_never_touches_production_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = _spawn_child()
    try:
        production = tmp_path / "prod"
        production_pid = production / "runtime" / "engineering-kb.pid.json"
        _write_record(production_pid, child.pid)
        staging = staging_settings(tmp_path / "staging")
        monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", staging)
        # 本机 8502 可能有真实 staging 服务；本测试只关心 stop 的进程作用域
        monkeypatch.setattr(manager, "is_port_open", lambda port: False)

        # staging 无 PID 记录：stop 不得触碰 production 记录指向的进程
        assert manager.stop_service() == 0
        assert "未运行" in capsys.readouterr().out
        assert manager.is_process_alive(child.pid)
        assert production_pid.is_file()

        # staging 记录指向该进程：stop 只终止记录中的进程，production 记录原样保留
        _write_record(staging.pid_path, child.pid)
        assert manager.stop_service() == 0
        assert not manager.is_process_alive(child.pid)
        assert production_pid.is_file()
        assert json.loads(production_pid.read_text(encoding="utf-8"))["pid"] == child.pid
    finally:
        if manager.is_process_alive(child.pid):
            child.kill()


def test_stop_production_never_touches_staging_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = _spawn_child()
    try:
        staging = staging_settings(tmp_path / "staging")
        _write_record(staging.pid_path, child.pid)
        # production 模式（默认），且 production 无 PID 记录
        production = tmp_path / "prod"
        prod_ns = SimpleNamespace(
            port=8501,
            runtime_dir=production / "runtime",
            pid_path=production / "runtime" / "engineering-kb.pid.json",
            logs_dir=production / "logs",
            ensure_directories=lambda: None,
        )
        monkeypatch.setattr(manager, "get_settings", lambda: prod_ns)
        # 本机 8501 可能有真实服务；本测试只关心 stop 的进程作用域
        monkeypatch.setattr(manager, "is_port_open", lambda port: False)

        assert manager.stop_service() == 0
        assert "未运行" in capsys.readouterr().out
        assert manager.is_process_alive(child.pid)
        assert staging.pid_path.is_file()
    finally:
        if manager.is_process_alive(child.pid):
            child.kill()


# --------------------------------------------------------------- port isolation
def test_instances_probe_independent_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[int] = []
    monkeypatch.setattr(
        manager, "is_port_open", lambda port: probed.append(port) or False
    )
    staging = staging_settings(tmp_path / "staging")
    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", staging)
    manager.detect_state()
    assert probed == [8502]

    probed.clear()
    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", None)
    production = SimpleNamespace(port=8501, pid_path=tmp_path / "p" / "pid.json")
    monkeypatch.setattr(manager, "get_settings", lambda: production)
    manager.detect_state()
    assert probed == [8501]


# ---------------------------------------------------------------- log isolation
def test_manager_log_goes_to_staging_logs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = staging_settings(tmp_path / "staging")
    monkeypatch.setattr(manager, "_ACTIVE_SETTINGS", staging)
    manager.LOGGER.handlers.clear()

    manager.configure_manager_logging()

    try:
        assert manager.LOGGER.handlers
        handler_path = Path(manager.LOGGER.handlers[-1].baseFilename)
        assert staging.logs_dir in handler_path.parents
    finally:
        manager.LOGGER.handlers.clear()
