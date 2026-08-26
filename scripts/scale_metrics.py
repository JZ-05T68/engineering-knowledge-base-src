"""Lightweight scale-test metric collection for v0.2.3 (stdlib only).

Records one :class:`ScaleMetric` per scale-test run: timings, PDF size, peak
working set of the collecting process (via ``GetProcessMemoryInfo`` — psutil
is intentionally not a dependency), disk headroom and directory growth, plus
the runtime versions needed to compare runs across machines.  Results are
printed as a human-readable summary and appended as JSONL to
``runtime/v023-scale/metrics.jsonl`` by default.

Typical use::

    with ScaleMetricsCollector("generate-50-mixed", watch_dir=pdf_dir) as metrics:
        result = build_scale_pdf(...)
        metrics.finish(pdf_pages=result.pages, pdf_size_bytes=result.size_bytes)
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_PATH: Final[Path] = PROJECT_ROOT / "runtime" / "v023-scale" / "metrics.jsonl"
FORMAL_PROJECT_ROOT: Final[Path] = Path(r"D:\Projects\ekb-dev")


class FormalPathError(ValueError):
    """Raised when a metrics target points into the formal data directory."""


@dataclass(slots=True)
class ScaleMetric:
    """One scale-test metric record (serialized as one JSONL line)."""

    test_name: str
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    pdf_pages: int = 0
    pdf_size_bytes: int = 0
    peak_working_set_bytes: int | None = None
    working_set_start_bytes: int | None = None
    working_set_end_bytes: int | None = None
    disk_free_start_bytes: int | None = None
    disk_free_end_bytes: int | None = None
    dir_growth_bytes: int = 0
    python_version: str = ""
    pymupdf_version: str = ""
    streamlit_version: str = ""
    os_version: str = ""
    status: str = "success"
    error_type: str = ""
    error_message: str = ""


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _memory_counters() -> _PROCESS_MEMORY_COUNTERS | None:
    """Return this process's memory counters via GetProcessMemoryInfo.

    Windows-only; returns ``None`` on other platforms or when the call fails.
    The 64-bit HANDLE restype/argtypes declarations are required — with
    ctypes defaults the current-process pseudo-handle is truncated to 32
    bits and the call fails silently.
    """

    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None
    return counters


def peak_working_set_bytes() -> int | None:
    """Return this process's PeakWorkingSetSize, or ``None`` when unavailable.

    An unavailable measurement is never disguised as a real zero peak.
    """

    counters = _memory_counters()
    return None if counters is None else int(counters.PeakWorkingSetSize)


def current_working_set_bytes() -> int | None:
    """Return this process's current WorkingSetSize, or ``None`` when unavailable."""

    counters = _memory_counters()
    return None if counters is None else int(counters.WorkingSetSize)


class ScaleMetricsCollector:
    """Collect one metric record around a block of scale-test work.

    Usable as a context manager or via explicit ``start``/``finish``.  Inside
    a ``with`` block, ``finish`` should be called with the PDF facts; if the
    block exits without an explicit ``finish`` the record is completed
    automatically (as ``failed`` when an exception propagates).
    """

    def __init__(
        self,
        test_name: str,
        *,
        metrics_path: Path | str = DEFAULT_METRICS_PATH,
        watch_dir: Path | str | None = None,
    ) -> None:
        if not test_name.strip():
            raise ValueError("测试名称不能为空")
        self.metric = ScaleMetric(test_name=test_name.strip())
        self.metrics_path = Path(metrics_path)
        _reject_formal_path(self.metrics_path)
        self.watch_dir = Path(watch_dir) if watch_dir is not None else self.metrics_path.parent
        _reject_formal_path(self.watch_dir)
        self._started = 0.0
        self._dir_size_start = 0
        self._finished = False

    def __enter__(self) -> ScaleMetricsCollector:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if not self._finished:
            if exc is not None:
                self.finish(status="failed", error=exc)
            else:
                self.finish()
        return False

    def start(self) -> None:
        """Capture start timestamp, versions, disk headroom and directory size."""

        self.metric.started_at = _utc_now_iso()
        self.metric.python_version = platform.python_version()
        self.metric.pymupdf_version = _package_version("pymupdf")
        self.metric.streamlit_version = _package_version("streamlit")
        self.metric.os_version = platform.platform()
        self.metric.disk_free_start_bytes = _disk_free_bytes(self.watch_dir)
        self.metric.working_set_start_bytes = current_working_set_bytes()
        self._dir_size_start = directory_size_bytes(self.watch_dir)
        self._started = time.perf_counter()
        self._finished = False

    def finish(
        self,
        *,
        pdf_pages: int = 0,
        pdf_size_bytes: int = 0,
        status: str = "success",
        error: BaseException | None = None,
    ) -> ScaleMetric:
        """Complete the record, append it to the JSONL log and print a summary."""

        self.metric.finished_at = _utc_now_iso()
        self.metric.duration_seconds = time.perf_counter() - self._started
        self.metric.pdf_pages = int(pdf_pages)
        self.metric.pdf_size_bytes = int(pdf_size_bytes)
        self.metric.peak_working_set_bytes = peak_working_set_bytes()
        self.metric.working_set_end_bytes = current_working_set_bytes()
        self.metric.disk_free_end_bytes = _disk_free_bytes(self.watch_dir)
        self.metric.dir_growth_bytes = directory_size_bytes(self.watch_dir) - self._dir_size_start
        self.metric.status = "failed" if error is not None else status
        if error is not None:
            self.metric.error_type = type(error).__name__
            self.metric.error_message = str(error)[:500]
        self._finished = True
        self._append_jsonl()
        self._print_summary()
        return self.metric

    def _append_jsonl(self) -> None:
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(self.metric), ensure_ascii=False)
        with self.metrics_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def _print_summary(self) -> None:
        metric = self.metric
        print(f"== v0.2.3 容量指标：{metric.test_name} ==")
        print(f"状态：{metric.status}    耗时：{metric.duration_seconds:.3f} 秒")
        if metric.error_type:
            print(f"错误：{metric.error_type}: {metric.error_message}")
        print(f"PDF：{metric.pdf_pages} 页 / {metric.pdf_size_bytes} 字节")
        print(
            f"峰值工作集：{_format_optional_bytes(metric.peak_working_set_bytes)}    "
            f"工作集：{_format_optional_bytes(metric.working_set_start_bytes)} → "
            f"{_format_optional_bytes(metric.working_set_end_bytes)}    "
            f"目录增长：{metric.dir_growth_bytes:+d} 字节（{self.watch_dir}）"
        )
        print(
            f"磁盘可用：{_format_optional_bytes(metric.disk_free_start_bytes)} → "
            f"{_format_optional_bytes(metric.disk_free_end_bytes)}"
        )
        print(
            f"版本：Python {metric.python_version} / PyMuPDF {metric.pymupdf_version} / "
            f"Streamlit {metric.streamlit_version} / {metric.os_version}"
        )
        print(f"记录追加：{self.metrics_path}")


def _reject_formal_path(path: Path) -> None:
    """Refuse a metrics target inside the formal data directory."""

    resolved = path.resolve(strict=False)
    formal = FORMAL_PROJECT_ROOT.resolve(strict=False)
    if resolved == formal or formal in resolved.parents:
        raise FormalPathError(f"拒绝写入或扫描正式数据目录：{resolved}")


def directory_size_bytes(root: Path) -> int:
    """Return the recursive file size total of ``root`` (small test dirs only)."""

    total = 0
    if not root.is_dir():
        return 0
    for current, _directories, filenames in os.walk(root):
        for filename in filenames:
            candidate = Path(current) / filename
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _disk_free_bytes(path: Path) -> int | None:
    """Return free bytes at ``path``, or ``None`` when the OS call fails.

    A failed measurement is reported as unavailable (JSON ``null``), never as
    a fake zero that would look like a full disk.
    """

    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def _package_version(distribution: str) -> str:
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _format_optional_bytes(value: int | None) -> str:
    return "不可用" if value is None else _format_bytes(value)


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


__all__ = [
    "DEFAULT_METRICS_PATH",
    "FormalPathError",
    "ScaleMetric",
    "ScaleMetricsCollector",
    "current_working_set_bytes",
    "directory_size_bytes",
    "peak_working_set_bytes",
]
