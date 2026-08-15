"""Windows-friendly lifecycle manager for the local Streamlit service."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import (  # noqa: E402
    OfficialEndpointError,
    Settings,
    get_settings,
    staging_settings,
)

TASK_NAME: Final[str] = "EngineeringKnowledgeBase"
HEALTH_PATH: Final[str] = "/_stcore/health"
START_TIMEOUT_SECONDS: Final[int] = 30
STOP_TIMEOUT_SECONDS: Final[int] = 10
LOGGER = logging.getLogger("service_manager")

#: Settings of the instance being managed; ``None`` means production.
_ACTIVE_SETTINGS: Settings | None = None


def active_settings() -> Settings:
    """Return the settings of the managed instance (production by default).

    Production callers see ``get_settings()`` exactly as before; with
    ``--staging`` the manager operates on the isolated staging instance
    (port 8502, staging pid/log/runtime paths) instead.
    """

    return _ACTIVE_SETTINGS if _ACTIVE_SETTINGS is not None else get_settings()


@dataclass(frozen=True, slots=True)
class ServiceState:
    """Detected service state and concise user-facing detail."""

    code: str
    detail: str
    pid: int | None = None


def configure_manager_logging() -> None:
    """Write manager events to a bounded local log."""

    settings = active_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.logs_dir / "service-manager.log",
        maxBytes=1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{HEALTH_PATH}"


def is_healthy(port: int, timeout: float = 1.0) -> bool:
    """Check Streamlit's privacy-safe loopback health endpoint."""

    try:
        with urllib.request.urlopen(health_url(port), timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def is_port_open(port: int) -> bool:
    """Return whether a loopback TCP listener currently owns ``port``."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def is_process_alive(pid: int) -> bool:
    """Check one PID without signaling or enumerating unrelated Python processes."""

    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    process_query = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def process_executable(pid: int) -> Path | None:
    """Return the executable of one Windows PID for stale/PID-reuse protection."""

    if os.name != "nt":
        return None
    process_query = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        success = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        )
        return Path(buffer.value) if success else None
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def read_pid_record() -> dict[str, object] | None:
    """Read a validated project PID record; malformed records are treated as stale."""

    path = active_settings().pid_path
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("pid"), int):
            raise ValueError("PID 记录格式不正确")
        return value
    except (OSError, ValueError, json.JSONDecodeError):
        LOGGER.warning("发现无效 PID 文件，已清理：%s", path, exc_info=True)
        path.unlink(missing_ok=True)
        return None


def write_pid_record(pid: int) -> None:
    """Atomically persist process identity for precise future stop operations."""

    settings = active_settings()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "pid": pid,
        "project_root": str(PROJECT_ROOT),
        "python": str(expected_python()),
        "port": settings.port,
        "started_at": time.time(),
    }
    temporary = settings.pid_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(settings.pid_path)


def expected_python(*, windowed: bool = False) -> Path:
    """Return the project-local virtual environment interpreter path."""

    name = "pythonw.exe" if windowed else "python.exe"
    return PROJECT_ROOT / ".venv" / "Scripts" / name


def record_matches_process(record: dict[str, object]) -> bool:
    """Ensure a live PID still belongs to this project's recorded interpreter."""

    pid = int(record["pid"])
    executable = process_executable(pid)
    if executable is None:
        return os.name != "nt"
    expected = Path(str(record.get("python", expected_python())))
    return executable.resolve() == expected.resolve()


def detect_state(*, clean_stale: bool = True) -> ServiceState:
    """Combine PID, process identity, health, and port state."""

    settings = active_settings()
    record = read_pid_record()
    if record is not None:
        pid = int(record["pid"])
        if not is_process_alive(pid):
            if clean_stale:
                settings.pid_path.unlink(missing_ok=True)
            if is_port_open(settings.port):
                return ServiceState("port_occupied", "PID 已失效，端口被其他程序占用")
            return ServiceState("abnormal", "服务异常退出，已识别并清理过期 PID")
        if not record_matches_process(record):
            if clean_stale:
                settings.pid_path.unlink(missing_ok=True)
            return ServiceState("abnormal", "PID 已被其他程序复用，未终止该进程")
        if is_healthy(settings.port):
            return ServiceState("running", "服务正常运行", pid)
        started_at = float(record.get("started_at", 0))
        if time.time() - started_at <= START_TIMEOUT_SECONDS:
            return ServiceState("starting", "服务正在启动", pid)
        return ServiceState("abnormal", "进程存在但健康检查失败", pid)
    if is_port_open(settings.port):
        detail = (
            "端口响应健康检查，但没有本项目 PID 记录，无法安全接管"
            if is_healthy(settings.port)
            else "端口被其他程序占用"
        )
        return ServiceState("port_occupied", detail)
    return ServiceState("stopped", "服务未运行")


def start_service(*, open_browser: bool = True) -> int:
    """Start one detached local service and wait for a verified health response."""

    settings = active_settings()
    state = detect_state()
    if state.code == "running":
        print(f"工程知识库已经运行（PID {state.pid}）。")
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{settings.port}")
        return 0
    if state.code == "starting":
        print(f"工程知识库正在启动（PID {state.pid}），请稍候。")
        return 0
    if state.code == "port_occupied":
        print(f"启动失败：{state.detail}（127.0.0.1:{settings.port}）。")
        return 2

    python_path = expected_python()
    if not python_path.is_file():
        print(f"启动失败：虚拟环境不存在：{python_path}")
        print("请先按照 README 的首次安装步骤创建 .venv 并安装依赖。")
        return 3
    app_path = PROJECT_ROOT / "app.py"
    if not app_path.is_file():
        print(f"启动失败：找不到应用入口：{app_path}")
        return 4

    settings.ensure_directories()
    console_log = settings.logs_dir / "server-console.log"
    _rotate_console_log(console_log)
    command = [
        str(python_path),
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(settings.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = 0x00000008 | 0x00000200 | 0x08000000
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    if _ACTIVE_SETTINGS is not None:
        # staging 子进程必须整体运行在 staging_settings 之上，而不是
        # 临时改写 production 配置。
        environment["EKB_STAGING_INSTANCE"] = "1"
    else:
        environment.pop("EKB_STAGING_INSTANCE", None)
    with console_log.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )
    write_pid_record(process.pid)
    LOGGER.info("启动服务：pid=%s port=%s", process.pid, settings.port)

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            settings.pid_path.unlink(missing_ok=True)
            print(f"启动失败：服务异常退出（退出码 {process.returncode}）。")
            _print_log_tail(console_log)
            return 5
        if is_healthy(settings.port):
            print(f"工程知识库启动成功：http://127.0.0.1:{settings.port}")
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{settings.port}")
            return 0
        time.sleep(0.5)
    print("启动失败：等待健康检查超时，服务可能仍在初始化。")
    _print_log_tail(console_log)
    return 6


def stop_service() -> int:
    """Stop only the exact process recorded for this project."""

    settings = active_settings()
    record = read_pid_record()
    if record is None:
        state = detect_state()
        if state.code == "port_occupied":
            print(f"未停止任何进程：{state.detail}。")
            return 2
        print("工程知识库未运行。")
        return 0
    pid = int(record["pid"])
    if not is_process_alive(pid):
        settings.pid_path.unlink(missing_ok=True)
        print("服务已经停止，过期 PID 文件已清理。")
        return 0
    if not record_matches_process(record):
        settings.pid_path.unlink(missing_ok=True)
        print("拒绝停止：PID 已属于其他程序；已清理本项目过期记录。")
        return 3

    LOGGER.info("停止服务：pid=%s", pid)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline and is_process_alive(pid):
        time.sleep(0.25)
    if is_process_alive(pid):
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
            if handle:
                try:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, signal.SIGKILL)
    settings.pid_path.unlink(missing_ok=True)
    print("工程知识库已停止；未影响其他 Python 进程。")
    return 0


def show_status() -> int:
    """Print the five-state local lifecycle result."""

    state = detect_state()
    suffix = f"（PID {state.pid}）" if state.pid else ""
    print(f"{state.detail}{suffix}")
    return 0 if state.code in {"running", "starting", "stopped"} else 1


def enable_autostart() -> int:
    """Create an ONLOGON task, falling back to the current-user Startup folder."""

    if os.name != "nt":
        print("开机自启仅支持 Windows。")
        return 2
    pythonw = expected_python(windowed=True)
    if not pythonw.is_file():
        print(f"启用失败：找不到虚拟环境解释器：{pythonw}")
        return 3
    script = Path(__file__).resolve()
    task_command = f'"{pythonw}" "{script}" start --no-browser'
    result = subprocess.run(
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            task_command,
            "/F",
            "/RL",
            "LIMITED",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        try:
            startup_entry = _startup_entry_path()
            startup_entry.parent.mkdir(parents=True, exist_ok=True)
            startup_entry.write_text(
                "@echo off\n"
                f'"{pythonw}" "{script}" start --no-browser\n',
                encoding="utf-8",
            )
        except OSError as exc:
            print("启用开机自启失败：任务计划程序被拒绝，启动文件夹也无法写入。")
            print(str(exc))
            return result.returncode or 1
        print("任务计划程序被当前策略拒绝，已改用当前用户启动文件夹。")
        print(f"已启用登录后自动启动：{startup_entry}")
        return 0
    print(f"已启用当前用户登录后自动启动：计划任务 {TASK_NAME}")
    return 0


def disable_autostart() -> int:
    """Remove the optional current-user scheduled task."""

    if os.name != "nt":
        print("开机自启仅支持 Windows。")
        return 2
    startup_entry = _startup_entry_path()
    startup_existed = startup_entry.exists()
    try:
        startup_entry.unlink(missing_ok=True)
    except OSError as exc:
        print(f"无法移除启动文件夹入口：{exc}")
        return 1
    result = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout).strip()
        not_found = "不存在" in combined or "cannot find" in combined.casefold()
        if not_found or startup_existed:
            if startup_existed:
                print("已关闭并移除启动文件夹中的开机自启入口。")
            else:
                print("开机自启任务原本就不存在。")
            return 0
        print(f"关闭开机自启失败：{combined}")
        return result.returncode or 1
    print(f"已关闭并移除开机自启任务：{TASK_NAME}")
    return 0


def _startup_entry_path() -> Path:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise OSError("找不到当前用户 APPDATA 目录")
    return (
        Path(app_data)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "EngineeringKnowledgeBase.cmd"
    )


def _rotate_console_log(path: Path, max_bytes: int = 2 * 1024 * 1024) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_suffix(path.suffix + ".3")
    oldest.unlink(missing_ok=True)
    for index in range(2, 0, -1):
        source = path.with_suffix(path.suffix + f".{index}")
        if source.exists():
            source.replace(path.with_suffix(path.suffix + f".{index + 1}"))
    path.replace(path.with_suffix(path.suffix + ".1"))


def _print_log_tail(path: Path, line_count: int = 12) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if lines:
        print("最近的启动日志：")
        print("\n".join(lines[-line_count:]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="工程知识库本地服务管理器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start", help="启动后台服务")
    start_parser.add_argument("--no-browser", action="store_true")
    start_parser.add_argument(
        "--staging", action="store_true", help="管理隔离的 staging 实例（8502）"
    )
    stop_parser = subparsers.add_parser("stop", help="停止本项目服务")
    stop_parser.add_argument(
        "--staging", action="store_true", help="停止 staging 实例（不影响 production）"
    )
    status_parser = subparsers.add_parser("status", help="查看运行状态")
    status_parser.add_argument(
        "--staging", action="store_true", help="查看 staging 实例状态"
    )
    subparsers.add_parser("enable-autostart", help="启用当前用户登录后自启")
    subparsers.add_parser("disable-autostart", help="关闭当前用户登录后自启")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if getattr(arguments, "staging", False):
        global _ACTIVE_SETTINGS
        _ACTIVE_SETTINGS = staging_settings()
    configure_manager_logging()
    try:
        if arguments.command == "start":
            return start_service(open_browser=not arguments.no_browser)
        if arguments.command == "stop":
            return stop_service()
        if arguments.command == "status":
            return show_status()
        if arguments.command == "enable-autostart":
            return enable_autostart()
        if arguments.command == "disable-autostart":
            return disable_autostart()
    except OfficialEndpointError as exc:
        print(f"正式服务配置错误：{exc}")
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
