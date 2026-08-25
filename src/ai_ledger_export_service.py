"""AI ledger export service (v0.5.3 Phase 6B).

Independent audit package for ``ai_calls`` metadata. JSON/JSONL are the
authoritative lossless formats. Per-record ``provider`` and any cost field are
deliberately absent (v12 does not persist them); the manifest documents both
omissions instead of guessing.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.ai_ledger_service import AILedgerService
from src.backup_service import sha256_file
from src.database import Database
from src.migrations import SCHEMA_VERSION
from src.models import AICallLedgerEntry, AICallLedgerQuery

AI_LEDGER_EXPORT_FORMAT = "engineering-knowledge-base-ai-ledger-export"
AI_LEDGER_EXPORT_FORMAT_VERSION = 1
_PAGE_SIZE = 200


class AILedgerExportError(RuntimeError):
    """Raised when an AI ledger export cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class AILedgerExportResult:
    export_path: Path
    manifest: dict[str, object]
    record_count: int
    file_count: int


class AILedgerExportService:
    """Read-only, verified AI call ledger export with atomic publish."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._ledger = AILedgerService(database)

    def export(
        self,
        target_dir: Path,
        *,
        query: AICallLedgerQuery | None = None,
        app_version: str,
        export_name: str | None = None,
    ) -> AILedgerExportResult:
        root = target_dir.expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        destination = root / (export_name or f"ai-ledger-export-{timestamp}")
        if destination.exists():
            raise AILedgerExportError(f"导出目标已存在，不会覆盖：{destination}")
        staging = root / f".ai-ledger-export-{timestamp}.incomplete-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=False)
            active_query = query or AICallLedgerQuery(limit=_PAGE_SIZE)
            entries = self._read_all(active_query)
            stats = self._ledger.stats(active_query)
            records = [self._record(entry) for entry in entries]
            files: list[dict[str, object]] = []
            files.append(self._write_json(staging, "ai_calls.json", records))
            files.append(self._write_jsonl(staging, "ai_calls.jsonl", records))
            manifest = self._manifest(active_query, stats, records, files, app_version)
            files.append(self._write_json(staging, "manifest.json", manifest))
            files.sort(key=lambda item: str(item["path"]))
            self._validate(staging, files)
            staging.rename(destination)
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, AILedgerExportError):
                raise
            raise AILedgerExportError(f"AI 台账导出失败：{exc}") from exc
        return AILedgerExportResult(
            export_path=destination,
            manifest=manifest,
            record_count=len(records),
            file_count=len(files),
        )

    def _read_all(self, base: AICallLedgerQuery) -> list[AICallLedgerEntry]:
        entries: list[AICallLedgerEntry] = []
        offset = 0
        while True:
            page = self._ledger.query(
                AICallLedgerQuery(
                    source_feature=base.source_feature,
                    capability=base.capability,
                    status=base.status,
                    provider=base.provider,
                    model=base.model,
                    since_iso=base.since_iso,
                    until_iso=base.until_iso,
                    sort=base.sort,
                    limit=_PAGE_SIZE,
                    offset=offset,
                )
            )
            entries.extend(page.entries)
            offset += len(page.entries)
            if offset >= page.total or not page.entries:
                break
        return entries

    def _record(self, entry: AICallLedgerEntry) -> dict[str, object]:
        target_refs_state: dict[str, str] = {}
        for stable_id in entry.target_refs:
            if stable_id in entry.unavailable_target_refs:
                target_refs_state[stable_id] = "missing"
            else:
                target_refs_state[stable_id] = "available"
        if entry.target_refs_parse_error:
            target_refs_state = {}
        return {
            "call_id": entry.call_id,
            "call_uuid": entry.call_uuid,
            "capability": entry.capability,
            "source_feature": entry.source_feature,
            "status": entry.status,
            "model": entry.model,
            "started_at": entry.created_at,
            "finished_at": entry.finished_at,
            "latency_ms": entry.latency_ms,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "total_tokens": entry.total_tokens,
            "retry_count": entry.retry_count,
            "finish_reason": entry.finish_reason,
            "error_category": entry.error_class,
            "error_summary": entry.error_summary,
            "target_refs": list(entry.target_refs),
            "target_refs_parse_error": entry.target_refs_parse_error,
            "target_refs_state_at_export": target_refs_state,
        }

    def _manifest(
        self,
        query: AICallLedgerQuery,
        stats,
        records: list[dict[str, object]],
        files: list[dict[str, object]],
        app_version: str,
    ) -> dict[str, object]:
        by_feature: dict[str, int] = {}
        by_capability: dict[str, int] = {}
        for record in records:
            feature = str(record["source_feature"])
            capability = str(record["capability"])
            by_feature[feature] = by_feature.get(feature, 0) + 1
            by_capability[capability] = by_capability.get(capability, 0) + 1
        return {
            "export_format": AI_LEDGER_EXPORT_FORMAT,
            "ai_ledger_export_format_version": AI_LEDGER_EXPORT_FORMAT_VERSION,
            "application_version": app_version,
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "record_count": len(records),
            "success_count": stats.success_count,
            "error_count": stats.error_count,
            "rejected_count": stats.rejected_count,
            "by_source_feature": by_feature,
            "by_capability": by_capability,
            "filter": {
                "source_feature": query.source_feature,
                "capability": query.capability,
                "status": query.status,
                "model": query.model,
                "since_iso": query.since_iso,
                "until_iso": query.until_iso,
            },
            "privacy": (
                "不包含 API Key、认证头、模型提示词正文、知识正文、页面正文、"
                "模型输出正文、本地敏感路径或环境变量。"
            ),
            "omissions": (
                "provider 未持久化，逐条记录不生成 provider 字段；"
                "费用未持久化，未导出任何 guessed_cost/estimated_cost。"
            ),
            "files": files,
        }

    def _write_json(self, root: Path, name: str, value: object) -> dict[str, object]:
        path = root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return _record(path, root)

    def _write_jsonl(
        self, root: Path, name: str, records: list[dict[str, object]]
    ) -> dict[str, object]:
        path = root / name
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return _record(path, root)

    def _validate(self, root: Path, files: list[dict[str, object]]) -> None:
        seen: set[str] = set()
        for record in files:
            path = str(record["path"])
            if path in seen:
                raise AILedgerExportError(f"文件清单重复：{path}")
            seen.add(path)
            target = root / path
            if not target.is_file():
                raise AILedgerExportError(f"导出文件缺失：{path}")
            if target.stat().st_size != int(record["size"]):
                raise AILedgerExportError(f"导出文件大小不一致：{path}")
            if sha256_file(target) != str(record["sha256"]):
                raise AILedgerExportError(f"导出文件哈希不一致：{path}")


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }
