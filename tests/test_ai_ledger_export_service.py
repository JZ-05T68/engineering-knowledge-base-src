"""Tests for the AI ledger export service (v0.5.3 Phase 6B)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ai.provider import AiCallRecord
from src.ai_ledger_export_service import (
    AI_LEDGER_EXPORT_FORMAT_VERSION,
    AILedgerExportError,
    AILedgerExportService,
)
from src.database import Database
from src.models import AICallLedgerQuery

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _record(index: int, **overrides) -> AiCallRecord:
    defaults = {
        "call_uuid": f"{index:032d}",
        "capability": "completion",
        "model": "qwen3.7-plus",
        "prompt_sha256": "a" * 64,
        "input_chars": 10,
        "status": "success",
        "source_feature": "rag_answer",
        "target_refs": (f"{KB_UUID}:knowledge_object:1",),
        "created_at": f"2026-08-25T10:00:{index:02d}",
        "total_tokens": 100,
    }
    defaults.update(overrides)
    return AiCallRecord(**defaults)


def test_empty_ledger_export(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    result = AILedgerExportService(database).export(
        tmp_path / "out", app_version="0.5.2"
    )

    manifest = json.loads((result.export_path / "manifest.json").read_text("utf-8"))
    assert manifest["ai_ledger_export_format_version"] == AI_LEDGER_EXPORT_FORMAT_VERSION
    assert manifest["record_count"] == 0
    assert result.record_count == 0


def test_full_export_is_lossless_and_private(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    database.insert_ai_call(_record(1))
    database.insert_ai_call(
        _record(2, status="error", error_class="transport sk-abcdefgh12345678",
                source_feature="experience_model", target_refs=())
    )
    result = AILedgerExportService(database).export(
        tmp_path / "out", app_version="0.5.2"
    )
    root = result.export_path

    records = json.loads((root / "ai_calls.json").read_text("utf-8"))
    assert len(records) == 2
    first = next(item for item in records if item["source_feature"] == "rag_answer")
    assert first["target_refs"] == [f"{KB_UUID}:knowledge_object:1"]
    assert "provider" not in first
    assert "cost" not in first
    error_record = next(item for item in records if item["status"] == "error")
    assert "[REDACTED]" in error_record["error_summary"]
    jsonl_lines = (root / "ai_calls.jsonl").read_text("utf-8").strip().splitlines()
    assert len(jsonl_lines) == 2
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert manifest["success_count"] == 1
    assert manifest["error_count"] == 1
    text = "\n".join(
        path.read_text("utf-8", errors="ignore")
        for path in root.rglob("*")
        if path.is_file()
    ).lower()
    for forbidden in ("prompt_sha256", "回答", "api_key", "authorization", "上下文正文"):
        assert forbidden not in text


def test_filtered_export_respects_query(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    database.insert_ai_call(_record(1, source_feature="rag_answer"))
    database.insert_ai_call(_record(2, source_feature="experience_model"))
    result = AILedgerExportService(database).export(
        tmp_path / "out",
        query=AICallLedgerQuery(source_feature="experience_model"),
        app_version="0.5.2",
    )

    records = json.loads((result.export_path / "ai_calls.json").read_text("utf-8"))
    assert len(records) == 1
    assert records[0]["source_feature"] == "experience_model"


def test_output_conflict_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    service = AILedgerExportService(database)
    service.export(tmp_path / "out", app_version="0.5.2", export_name="dup")

    with pytest.raises(AILedgerExportError, match="不会覆盖"):
        service.export(tmp_path / "out", app_version="0.5.2", export_name="dup")
