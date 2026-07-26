"""Tests for the v0.2.3 scale metric collector (JSONL, stdlib only)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.scale_metrics import (
    FormalPathError,
    ScaleMetricsCollector,
    peak_working_set_bytes,
)

REQUIRED_FIELDS = {
    "test_name",
    "started_at",
    "finished_at",
    "duration_seconds",
    "pdf_pages",
    "pdf_size_bytes",
    "peak_working_set_bytes",
    "disk_free_start_bytes",
    "disk_free_end_bytes",
    "dir_growth_bytes",
    "python_version",
    "pymupdf_version",
    "streamlit_version",
    "os_version",
    "status",
    "error_type",
    "error_message",
}


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_success_record_is_complete_and_parseable(tmp_path: Path) -> None:
    metrics_path = tmp_path / "nested" / "metrics.jsonl"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "before.bin").write_bytes(b"x" * 100)

    collector = ScaleMetricsCollector(
        "unit-success", metrics_path=metrics_path, watch_dir=watch_dir
    )
    collector.start()
    (watch_dir / "grown.bin").write_bytes(b"y" * 60)
    metric = collector.finish(pdf_pages=3, pdf_size_bytes=12345)

    assert metric.status == "success"
    assert metric.error_type == ""
    assert metric.pdf_pages == 3
    assert metric.pdf_size_bytes == 12345
    assert metric.duration_seconds >= 0
    assert metric.dir_growth_bytes == 60
    assert metric.disk_free_start_bytes > 0
    assert metric.disk_free_end_bytes > 0
    if os.name == "nt":
        assert metric.peak_working_set_bytes > 0

    records = _read_jsonl(metrics_path)
    assert len(records) == 1
    record = records[0]
    assert REQUIRED_FIELDS <= set(record)
    assert record["test_name"] == "unit-success"
    assert record["status"] == "success"
    assert record["python_version"]
    assert record["pymupdf_version"]
    assert record["os_version"]


def test_failure_record_captures_error_type(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics.jsonl"

    with pytest.raises(ValueError, match="boom"):
        with ScaleMetricsCollector(
            "unit-failure", metrics_path=metrics_path, watch_dir=tmp_path
        ):
            raise ValueError("boom")

    records = _read_jsonl(metrics_path)
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "failed"
    assert record["error_type"] == "ValueError"
    assert "boom" in record["error_message"]


def test_context_manager_finishes_successful_block(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics.jsonl"

    with ScaleMetricsCollector(
        "unit-context", metrics_path=metrics_path, watch_dir=tmp_path
    ):
        pass

    records = _read_jsonl(metrics_path)
    assert len(records) == 1
    assert records[0]["status"] == "success"


def test_records_append_to_the_same_jsonl(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics.jsonl"

    for name in ("first", "second"):
        collector = ScaleMetricsCollector(
            name, metrics_path=metrics_path, watch_dir=tmp_path
        )
        collector.start()
        collector.finish()

    records = _read_jsonl(metrics_path)
    assert [record["test_name"] for record in records] == ["first", "second"]


def test_explicit_finish_is_not_duplicated_on_exit(tmp_path: Path) -> None:
    metrics_path = tmp_path / "metrics.jsonl"

    with ScaleMetricsCollector(
        "unit-explicit", metrics_path=metrics_path, watch_dir=tmp_path
    ) as collector:
        collector.finish(pdf_pages=7)

    assert len(_read_jsonl(metrics_path)) == 1


def test_peak_working_set_helper() -> None:
    value = peak_working_set_bytes()

    if os.name == "nt":
        assert value is not None and value > 0
    else:
        assert value is None


def test_formal_metrics_targets_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(FormalPathError):
        ScaleMetricsCollector(
            "unit-formal-path",
            metrics_path="D:/Projects/engineering-kb/logs/metrics.jsonl",
            watch_dir=tmp_path,
        )
    with pytest.raises(FormalPathError):
        ScaleMetricsCollector(
            "unit-formal-watch",
            metrics_path=tmp_path / "metrics.jsonl",
            watch_dir="D:/Projects/engineering-kb/data",
        )
