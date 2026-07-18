"""Tests for safe local lifecycle detection without killing real processes."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import scripts.service_manager as manager


def make_settings(tmp_path: Path, port: int) -> SimpleNamespace:
    """Return the small settings surface used by lifecycle detection."""

    runtime_dir = tmp_path / "runtime"
    logs_dir = tmp_path / "logs"

    def ensure_directories() -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(
        port=port,
        runtime_dir=runtime_dir,
        pid_path=runtime_dir / "engineering-kb.pid.json",
        logs_dir=logs_dir,
        ensure_directories=ensure_directories,
    )


def test_stale_pid_is_detected_and_cleaned(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path, 49321)
    settings.ensure_directories()
    settings.pid_path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    monkeypatch.setattr(manager, "get_settings", lambda: settings)
    monkeypatch.setattr(manager, "is_process_alive", lambda pid: False)
    monkeypatch.setattr(manager, "is_port_open", lambda port: False)

    state = manager.detect_state()

    assert state.code == "abnormal"
    assert "过期 PID" in state.detail
    assert not settings.pid_path.exists()


def test_foreign_port_listener_is_not_treated_as_our_service(
    tmp_path: Path, monkeypatch
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        settings = make_settings(tmp_path, port)
        monkeypatch.setattr(manager, "get_settings", lambda: settings)

        state = manager.detect_state()

    assert state.code == "port_occupied"
    assert "其他程序" in state.detail


def test_missing_virtual_environment_returns_clear_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings = make_settings(tmp_path, 49322)
    monkeypatch.setattr(manager, "get_settings", lambda: settings)
    monkeypatch.setattr(
        manager,
        "detect_state",
        lambda: manager.ServiceState("stopped", "服务未运行"),
    )
    monkeypatch.setattr(manager, "expected_python", lambda: tmp_path / "missing.exe")

    assert manager.start_service(open_browser=False) == 3
    assert "虚拟环境不存在" in capsys.readouterr().out


def test_pid_reuse_refuses_to_stop_unrelated_process(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings = make_settings(tmp_path, 49323)
    settings.ensure_directories()
    settings.pid_path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
    monkeypatch.setattr(manager, "get_settings", lambda: settings)
    monkeypatch.setattr(manager, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(manager, "record_matches_process", lambda record: False)

    assert manager.stop_service() == 3
    assert "拒绝停止" in capsys.readouterr().out
    assert not settings.pid_path.exists()


def test_duplicate_start_reuses_running_instance(monkeypatch, capsys) -> None:
    settings = SimpleNamespace(port=8501)
    monkeypatch.setattr(manager, "get_settings", lambda: settings)
    monkeypatch.setattr(
        manager,
        "detect_state",
        lambda: manager.ServiceState("running", "服务正常运行", 24680),
    )

    assert manager.start_service(open_browser=False) == 0
    assert "已经运行" in capsys.readouterr().out
