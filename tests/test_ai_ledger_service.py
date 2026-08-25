"""Tests for the read-only AI call ledger query service (Phase 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ai.provider import AiCallRecord
from src.ai_ledger_service import AILedgerError, AILedgerService
from src.database import Database
from src.models import AICallLedgerQuery

KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _record(
    index: int,
    *,
    capability: str = "completion",
    source_feature: str = "rag_answer",
    status: str = "success",
    model: str = "qwen3.7-plus",
    created_at: str = "2026-08-25T10:00:00",
    target_refs: tuple[str, ...] = (),
    error_class: str | None = None,
    total_tokens: int | None = 100,
    latency_ms: int | None = 200,
) -> AiCallRecord:
    return AiCallRecord(
        call_uuid=f"{index:032d}",
        capability=capability,
        model=model,
        prompt_sha256="a" * 64,
        input_chars=10,
        status=status,
        source_feature=source_feature,
        target_refs=target_refs,
        error_class=error_class,
        latency_ms=latency_ms,
        total_tokens=total_tokens,
        created_at=created_at,
    )


def _seed(database: Database, *records: AiCallRecord) -> None:
    for record in records:
        database.insert_ai_call(record)


def test_empty_ledger(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    service = AILedgerService(database)

    stats = service.stats()
    page = service.query(AICallLedgerQuery())

    assert stats.total_calls == 0
    assert stats.total_tokens == 0
    assert page.total == 0
    assert page.entries == ()


def test_single_and_multiple_rows(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(database, _record(1), _record(2, source_feature="experience_model"))
    service = AILedgerService(database)

    page = service.query(AICallLedgerQuery(limit=10))

    assert page.total == 2
    assert [entry.call_id for entry in page.entries] == [2, 1]
    assert page.entries[0].source_feature == "experience_model"


def test_pagination_is_stable(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(database, *(_record(index) for index in range(5)))
    service = AILedgerService(database)

    first = service.query(AICallLedgerQuery(limit=2, offset=0))
    second = service.query(AICallLedgerQuery(limit=2, offset=2))

    assert first.total == 5
    assert [entry.call_id for entry in first.entries] == [5, 4]
    assert [entry.call_id for entry in second.entries] == [3, 2]


def test_stable_sort_by_created_at(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(
        database,
        _record(1, created_at="2026-08-25T09:00:00"),
        _record(2, created_at="2026-08-25T11:00:00"),
    )
    service = AILedgerService(database)

    desc = service.query(AICallLedgerQuery(sort="created_at_desc"))
    asc = service.query(AICallLedgerQuery(sort="created_at_asc"))

    assert [entry.call_id for entry in desc.entries] == [2, 1]
    assert [entry.call_id for entry in asc.entries] == [1, 2]


def test_each_filter_and_combination(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(
        database,
        _record(1, source_feature="rag_answer", capability="completion"),
        _record(2, source_feature="experience_model", capability="completion"),
        _record(3, source_feature="page_index", capability="embedding"),
        _record(4, status="error", error_class="http_500", source_feature="experience_model"),
    )
    service = AILedgerService(database)

    assert service.query(AICallLedgerQuery(source_feature="rag_answer")).total == 1
    assert service.query(AICallLedgerQuery(capability="embedding")).total == 1
    assert service.query(AICallLedgerQuery(status="error")).total == 1
    assert service.query(AICallLedgerQuery(model="qwen3.7-plus")).total == 4
    assert (
        service.query(
            AICallLedgerQuery(
                source_feature="experience_model",
                capability="completion",
                status="success",
            )
        ).total
        == 1
    )
    assert service.query(AICallLedgerQuery(since_iso="2026-08-25T10:00:00")).total == 4
    assert (
        service.query(AICallLedgerQuery(since_iso="2026-08-25T10:00:01")).total
        == 0
    )


def test_invalid_inputs_are_rejected(tmp_path: Path) -> None:
    service = AILedgerService(Database(tmp_path / "knowledge.db"))

    with pytest.raises(AILedgerError, match="sort"):
        service.query(AICallLedgerQuery(sort="1; DROP TABLE ai_calls"))
    with pytest.raises(AILedgerError, match="limit"):
        service.query(AICallLedgerQuery(limit=0))
    with pytest.raises(AILedgerError, match="limit"):
        service.query(AICallLedgerQuery(limit=201))
    with pytest.raises(AILedgerError, match="offset"):
        service.query(AICallLedgerQuery(offset=-1))
    with pytest.raises(AILedgerError, match="provider"):
        service.query(AICallLedgerQuery(provider="openai"))
    with pytest.raises(AILedgerError, match="status"):
        service.query(AICallLedgerQuery(status="pending"))


def test_target_refs_parse_and_missing_target(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    database.create_knowledge_object(kind="fact", title="已存在对象", content="内容")
    _seed(
        database,
        _record(
            1,
            target_refs=(
                f"{KB_UUID}:knowledge_object:1",
                f"{KB_UUID}:knowledge_object:999",
            ),
        ),
    )
    service = AILedgerService(database)

    entry = service.query(AICallLedgerQuery()).entries[0]

    assert entry.target_refs == (
        f"{KB_UUID}:knowledge_object:1",
        f"{KB_UUID}:knowledge_object:999",
    )
    assert entry.target_refs_parse_error is False
    assert entry.unavailable_target_refs == (f"{KB_UUID}:knowledge_object:999",)


def test_historical_invalid_target_refs_fail_safe(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(database, _record(1))
    with database._connection() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            "INSERT INTO ai_calls(call_uuid, capability, model, prompt_sha256,"
            " input_chars, status, source_feature, target_refs, created_at)"
            " VALUES ('bad-refs', 'completion', 'm', ?, 1, 'success',"
            " 'rag_answer', 'not-json', '2026-08-25T10:00:00')",
            ("b" * 64,),
        )
    service = AILedgerService(database)

    page = service.query(AICallLedgerQuery())

    assert page.total == 2
    bad = next(
        entry for entry in page.entries if entry.call_uuid == "bad-refs"
    )
    assert bad.target_refs_parse_error is True
    assert bad.target_refs == ()


def test_token_aggregation_ignores_nulls(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(
        database,
        _record(1, total_tokens=100),
        _record(2, total_tokens=None),
        _record(3, total_tokens=50),
    )
    service = AILedgerService(database)

    stats = service.stats()

    assert stats.total_calls == 3
    assert stats.total_tokens == 150
    assert stats.by_source_feature == (("rag_answer", 3),)


def test_error_summary_is_redacted_and_truncated(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(
        database,
        _record(
            1,
            status="error",
            error_class="transport Bearer sk-abcdefgh12345678 "
            + "x" * 300,
        ),
    )
    service = AILedgerService(database)

    entry = service.query(AICallLedgerQuery()).entries[0]

    assert "[REDACTED]" in entry.error_summary
    assert "sk-abcdefgh" not in entry.error_summary
    assert len(entry.error_summary) <= 121


def test_query_and_stats_never_write_the_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    _seed(database, _record(1))
    service = AILedgerService(database)
    before = database.database_path.read_bytes()

    service.query(AICallLedgerQuery())
    service.stats()
    service.distinct_models()
    service.distinct_source_features()

    assert database.database_path.read_bytes() == before


def test_service_has_no_provider_dependency(tmp_path: Path) -> None:
    service = AILedgerService(Database(tmp_path / "unused.db"))
    assert not hasattr(service, "provider")
