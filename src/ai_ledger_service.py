"""Read-only AI call ledger query service (v0.5.3 Phase 5).

The service only ever reads ``ai_calls``. It never calls a provider, never
modifies a ledger row, never repairs historical data and never writes the
database. Mock/offline demonstrations do not appear here because they never
write the ledger (they bypass ``AuditedAIProvider``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from src.database import Database
from src.models import (
    AICallLedgerEntry,
    AICallLedgerPage,
    AICallLedgerQuery,
    AICallLedgerStats,
)

__all__ = ["AILedgerError", "AILedgerService"]

_SORT_SQL = {
    "created_at_desc": "created_at DESC, id DESC",
    "created_at_asc": "created_at ASC, id ASC",
    "id_desc": "id DESC",
    "id_asc": "id ASC",
    "latency_desc": "latency_ms IS NULL, latency_ms DESC, id DESC",
    "total_tokens_desc": "total_tokens IS NULL, total_tokens DESC, id DESC",
}
_MAX_PAGE_SIZE = 200
_SUPPORTED_PROVIDER = "qwen"
_TARGET_TABLES = {
    "knowledge_object": "knowledge_objects",
    "knowledge_memory": "knowledge_memory_entries",
    "page": "pages",
    "evidence": "evidence_items",
    "document": "documents",
    "note": "notes",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[^\s\"']+"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"[A-Za-z0-9_\-]{40,}"),
)


class AILedgerError(ValueError):
    """Raised for invalid query input; never for historical data anomalies."""


class AILedgerService:
    """Read-only query + limited aggregation over the AI call ledger."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def query(self, query: AICallLedgerQuery) -> AICallLedgerPage:
        """Return one stable page of entries plus the filtered total."""

        self._validate_query(query)
        where, params = self._where(query)
        with self._database._connection() as connection:  # noqa: SLF001 - read only
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ai_calls WHERE {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM ai_calls WHERE {where} "
                f"ORDER BY {_SORT_SQL[query.sort]} LIMIT ? OFFSET ?",
                (*params, query.limit, query.offset),
            ).fetchall()
        entries = tuple(self._entry_from_row(row) for row in rows)
        entries = self._with_target_availability(entries)
        return AICallLedgerPage(
            entries=entries,
            total=total,
            limit=query.limit,
            offset=query.offset,
        )

    def stats(self, query: AICallLedgerQuery | None = None) -> AICallLedgerStats:
        """Return limited aggregates, honoring the same filters (no pagination)."""

        base = query or AICallLedgerQuery()
        self._validate_query(base)
        where, params = self._where(base)
        with self._database._connection() as connection:  # noqa: SLF001 - read only
            row = connection.execute(
                f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN total_tokens IS NOT NULL
                                THEN total_tokens ELSE 0 END)
                FROM ai_calls WHERE {where}
                """,
                params,
            ).fetchone()
            feature_rows = connection.execute(
                f"""
                SELECT source_feature, COUNT(*)
                FROM ai_calls WHERE {where}
                GROUP BY source_feature
                ORDER BY COUNT(*) DESC, source_feature ASC
                """,
                params,
            ).fetchall()
        total_tokens = 0 if row[4] is None else int(row[4])
        return AICallLedgerStats(
            total_calls=int(row[0] or 0),
            success_count=int(row[1] or 0),
            error_count=int(row[2] or 0),
            rejected_count=int(row[3] or 0),
            total_tokens=total_tokens,
            by_source_feature=tuple(
                (str(feature), int(count)) for feature, count in feature_rows
            ),
        )

    def distinct_models(self) -> tuple[str, ...]:
        """Return distinct model names for the UI filter."""

        with self._database._connection() as connection:  # noqa: SLF001 - read only
            rows = connection.execute(
                "SELECT DISTINCT model FROM ai_calls ORDER BY model ASC"
            ).fetchall()
        return tuple(str(row["model"]) for row in rows)

    def distinct_source_features(self) -> tuple[str, ...]:
        """Return distinct source_feature values for the UI filter."""

        with self._database._connection() as connection:  # noqa: SLF001 - read only
            rows = connection.execute(
                "SELECT DISTINCT source_feature FROM ai_calls "
                "ORDER BY source_feature ASC"
            ).fetchall()
        return tuple(str(row["source_feature"]) for row in rows)

    def _validate_query(self, query: AICallLedgerQuery) -> None:
        if query.sort not in _SORT_SQL:
            raise AILedgerError(f"非法 sort：{query.sort}")
        if isinstance(query.limit, bool) or not isinstance(query.limit, int):
            raise AILedgerError("limit 必须是整数")
        if not 1 <= query.limit <= _MAX_PAGE_SIZE:
            raise AILedgerError(f"limit 必须在 1～{_MAX_PAGE_SIZE} 之间")
        if (
            isinstance(query.offset, bool)
            or not isinstance(query.offset, int)
            or query.offset < 0
        ):
            raise AILedgerError("offset 必须是非负整数")
        if query.provider not in (None, _SUPPORTED_PROVIDER):
            raise AILedgerError(
                f"provider 仅支持 {_SUPPORTED_PROVIDER}（运行时唯一接入供应商）"
            )
        if query.status not in (None, "success", "error", "rejected"):
            raise AILedgerError("status 仅支持 success / error / rejected")
        if query.capability not in (None, "completion", "embedding", "rerank"):
            raise AILedgerError("capability 仅支持 completion / embedding / rerank")

    def _where(self, query: AICallLedgerQuery) -> tuple[str, list[object]]:
        clauses = ["1 = 1"]
        params: list[object] = []
        for column, value in (
            ("source_feature", query.source_feature),
            ("capability", query.capability),
            ("status", query.status),
            ("model", query.model),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if query.since_iso is not None:
            clauses.append("created_at >= ?")
            params.append(query.since_iso)
        if query.until_iso is not None:
            clauses.append("created_at <= ?")
            params.append(query.until_iso)
        # v12 stores no provider column; every real row comes from the single
        # wired vendor. A 'qwen' filter therefore selects all rows.
        return " AND ".join(clauses), params

    def _entry_from_row(self, row: object) -> AICallLedgerEntry:
        target_refs, parse_error = _parse_target_refs(row["target_refs"])
        error_summary = _sanitize_error(row["error_class"])
        return AICallLedgerEntry(
            call_id=int(row["id"]),
            call_uuid=str(row["call_uuid"]),
            capability=str(row["capability"]),
            source_feature=str(row["source_feature"]),
            provider=None,
            model=str(row["model"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            finished_at=None,
            latency_ms=_int_or_none(row["latency_ms"]),
            prompt_tokens=_int_or_none(row["prompt_tokens"]),
            completion_tokens=_int_or_none(row["completion_tokens"]),
            total_tokens=_int_or_none(row["total_tokens"]),
            target_refs=target_refs,
            target_refs_parse_error=parse_error,
            error_class=(
                str(row["error_class"]) if row["error_class"] is not None else None
            ),
            error_summary=error_summary,
            is_real_call=True,
            retry_count=int(row["retry_count"] or 0),
            finish_reason=(
                str(row["finish_reason"])
                if row["finish_reason"] is not None
                else None
            ),
        )

    def _with_target_availability(
        self, entries: Sequence[AICallLedgerEntry]
    ) -> tuple[AICallLedgerEntry, ...]:
        """Batch-resolve referenced targets; never one query per reference."""

        available: set[str] = set()
        by_table: dict[str, list[int]] = {}
        for entry in entries:
            if entry.target_refs_parse_error:
                continue
            for stable_id in entry.target_refs:
                parsed = _parse_stable_id(stable_id)
                if parsed is None:
                    continue
                stable_type, local_id = parsed
                table = _TARGET_TABLES.get(stable_type)
                if table is None:
                    continue
                by_table.setdefault(table, []).append(local_id)
        with self._database._connection() as connection:  # noqa: SLF001 - read only
            for table, ids in by_table.items():
                unique_ids = sorted(set(ids))
                if not unique_ids:
                    continue
                placeholders = ",".join("?" for _ in unique_ids)
                rows = connection.execute(
                    f"SELECT id FROM {table} WHERE id IN ({placeholders})",
                    unique_ids,
                ).fetchall()
                for row in rows:
                    available.add(f"{table}:{int(row['id'])}")
        resolved: list[AICallLedgerEntry] = []
        for entry in entries:
            unavailable = tuple(
                stable_id
                for stable_id in entry.target_refs
                if not _is_available(stable_id, available)
            )
            resolved.append(
                AICallLedgerEntry(
                    call_id=entry.call_id,
                    call_uuid=entry.call_uuid,
                    capability=entry.capability,
                    source_feature=entry.source_feature,
                    provider=entry.provider,
                    model=entry.model,
                    status=entry.status,
                    created_at=entry.created_at,
                    finished_at=entry.finished_at,
                    latency_ms=entry.latency_ms,
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=entry.completion_tokens,
                    total_tokens=entry.total_tokens,
                    target_refs=entry.target_refs,
                    target_refs_parse_error=entry.target_refs_parse_error,
                    unavailable_target_refs=unavailable,
                    error_class=entry.error_class,
                    error_summary=entry.error_summary,
                    is_real_call=entry.is_real_call,
                    retry_count=entry.retry_count,
                    finish_reason=entry.finish_reason,
                )
            )
        return tuple(resolved)


def _int_or_none(value: object) -> int | None:
    return int(value) if value is not None else None


def _parse_target_refs(raw: object) -> tuple[tuple[str, ...], bool]:
    if not isinstance(raw, str):
        return (), True
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return (), True
    if not isinstance(payload, list):
        return (), True
    refs: list[str] = []
    for item in payload:
        if not isinstance(item, str) or not item.strip():
            return (), True
        refs.append(item.strip())
    return tuple(dict.fromkeys(refs)), False


def _parse_stable_id(stable_id: str) -> tuple[str, int] | None:
    parts = stable_id.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _is_available(stable_id: str, available: set[str]) -> bool:
    parsed = _parse_stable_id(stable_id)
    if parsed is None:
        return False
    stable_type, local_id = parsed
    table = _TARGET_TABLES.get(stable_type)
    if table is None:
        return False
    return f"{table}:{local_id}" in available


def _sanitize_error(error_class: str | None) -> str:
    """Return a short, redacted error summary; never the raw error body."""

    if not error_class:
        return ""
    text = str(error_class).replace("\r", " ").replace("\n", " ").strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = text[:120]
    if len(error_class) > 120:
        text += "…"
    return text
