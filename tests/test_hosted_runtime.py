"""WP5 C1-C12: real composition, synthetic v12 storage, strictly offline."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import fields, replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_hosted_api_readiness import offline as offline  # noqa: F401
from test_hosted_storage import KB_UUID, configured
from test_hosted_storage import demo as demo  # noqa: F401
from test_hosted_storage import protect_production as protect_production  # noqa: F401

import src.hosted.runtime as runtime
from src.agent import AgentRequest, ModelDecisionProvider, SingleStepAgentService
from src.agent.tools import ToolContext, ToolInput, ToolResultStatus, ToolSideEffect
from src.ai.provider import AiCallRecord, AIUnavailableError, AuditedAIProvider
from src.ai.qwen_client import DEFAULT_BASE_URL, QwenProvider, urllib_transport
from src.evidence_basket_service import EvidenceBasketService
from src.hosted.storage import bootstrap_hosted_storage
from src.hosted_api.app import create_hosted_app
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.source_metadata import SourceMetadataService

TOOL_NAMES = {
    "page_search", "knowledge_search", "get_knowledge_object", "get_knowledge_memory",
    "inspect_provenance", "inspect_source_integrity", "get_evidence",
}
MUTATIONS = {
    KnowledgeObjectService: (
        "create", "update_content", "update_epistemic_basis", "confirm", "unconfirm",
        "archive", "unarchive", "supersede", "reactivate", "repoint_supersession", "delete",
        "recapture_source_fingerprint", "link_source", "unlink_source", "add_relation",
        "remove_relation",
    ),
    KnowledgeMemoryService: ("create_entry", "update_entry", "set_status", "delete_entry"),
    EvidenceBasketService: (
        "create_basket", "default_basket", "add_item", "add_page_item", "add_region_item",
        "set_confirmation", "remove_item", "clear", "update_note", "reorder",
    ),
}


@pytest.fixture
def composition(demo):
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        yield storage, runtime.compose_hosted_dependencies(demo.settings, storage)
    finally:
        storage.close()


def test_real_composition_types_provider_and_private_dependency_surface(composition, demo):
    storage, dependencies = composition
    assert dependencies.check_readiness().ready
    assert isinstance(dependencies.agent_service, SingleStepAgentService)
    assert isinstance(dependencies.decision_provider, ModelDecisionProvider)
    assert dependencies.request_factory is AgentRequest
    provider = dependencies.decision_provider._provider
    assert isinstance(provider, AuditedAIProvider)
    assert isinstance(provider.wrapped, QwenProvider)
    assert provider.wrapped._transport is urllib_transport
    assert provider.wrapped._base_url == DEFAULT_BASE_URL
    assert provider.wrapped._max_extra_attempts == 0
    assert provider.wrapped._enable_thinking is False
    assert provider._ledger._database is storage.database
    assert provider._budget_guard._database is storage.database
    assert dependencies.agent_service._final_answer._rag._provider is provider
    definitions = dependencies.agent_service._executor._registry.list_definitions()
    assert len(definitions) == 7 and {d.name for d in definitions} == TOOL_NAMES
    assert all(d.side_effect is ToolSideEffect.READ_ONLY for d in definitions)
    assert dependencies.decision_provider._definitions == definitions
    assert isinstance(dependencies.sources, SourceMetadataService)
    mixed = tuple(MUTATIONS)
    assert all(not isinstance(getattr(dependencies, f.name), mixed) for f in fields(dependencies))
    assert all(not isinstance(getattr(dependencies.sources, f.name), mixed)
               for f in fields(dependencies.sources))
    app = create_hosted_app(settings=demo.settings, dependencies=dependencies)
    assert {r.path for r in app.routes} == {
        "/health", "/ready", "/v0.6/agent/run", "/v0.6/sources/{stable_id}",
    }
    assert all(not isinstance(value, mixed) for value in app.state._state.values())


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_all_seven_use_frozen_reads_and_never_mutate(composition, demo, monkeypatch, name):
    storage, dependencies = composition
    spies = []
    for cls, methods in MUTATIONS.items():
        for method in methods:
            spy = Mock(side_effect=AssertionError("Hosted write path reached"))
            monkeypatch.setattr(cls, method, spy)
            spies.append(spy)
    arguments = {
        "page_search": {"query": "motor"},
        "knowledge_search": {"query": "PID"},
        "get_knowledge_object": {"stable_id": f"{KB_UUID}:knowledge_object:{demo.object_id}"},
        "get_knowledge_memory": {"stable_id": f"{KB_UUID}:knowledge_memory:{demo.memory_id}"},
        "inspect_provenance": {"stable_id": f"{KB_UUID}:knowledge_object:{demo.object_id}"},
        "inspect_source_integrity": {"stable_id": f"{KB_UUID}:knowledge_source:{demo.source_id}"},
        "get_evidence": {"stable_id": f"{KB_UUID}:evidence:1"},
    }
    method = {
        "page_search": "search", "knowledge_search": "search",
        "get_knowledge_object": "get_view", "get_knowledge_memory": "get",
        "inspect_provenance": "project", "inspect_source_integrity": "source_view",
        "get_evidence": "get_item",
    }[name]
    adapter = dependencies.agent_service._executor._handlers[name]
    reader = adapter._projector if name == "inspect_provenance" else adapter._service
    read_spy = Mock(wraps=getattr(reader, method))
    monkeypatch.setattr(reader, method, read_spy)
    with closing(sqlite3.connect(storage.database_path)) as db:
        before = tuple(db.iterdump())
        result = adapter(ToolInput(name, arguments[name]), ToolContext())
        assert result.status in {ToolResultStatus.SUCCESS, ToolResultStatus.PARTIAL}
        read_spy.assert_called_once()
        assert tuple(db.iterdump()) == before
    assert sum(spy.call_count for spy in spies) == 0


@pytest.mark.parametrize("corruption", ["missing", "extra", "write", "handler"])
def test_registry_drift_fails_startup(demo, monkeypatch, corruption):
    storage = bootstrap_hosted_storage(demo.settings)
    original = runtime.build_single_step_executor

    def corrupt(database):
        executor = original(database)
        definitions = executor._registry.list_definitions()
        if corruption == "missing":
            changed = definitions[:-1]
        elif corruption == "extra":
            changed = (*definitions, replace(definitions[0], name="extra_read"))
        elif corruption == "write":
            changed = (replace(definitions[0], side_effect=ToolSideEffect.WRITE_REVERSIBLE),
                       *definitions[1:])
        else:
            changed = definitions
            executor._handlers["extra_read"] = Mock()
        monkeypatch.setattr(executor._registry, "list_definitions", lambda: changed)
        return executor

    monkeypatch.setattr(runtime, "build_single_step_executor", corrupt)
    try:
        with pytest.raises(ValueError, match="hosted_tool_registry_invalid"):
            runtime.compose_hosted_dependencies(demo.settings, storage)
    finally:
        storage.close()


@pytest.mark.parametrize("key,budget,reason", [
    ("", 100, "ai_not_configured"), ("TEST_ONLY_FAKE_KEY", 0, "budget_not_configured"),
])
def test_missing_key_or_budget_health_alive_ready_and_agent_safe503(demo, key, budget, reason):
    settings = configured(demo, ai_api_key=key, ai_daily_token_budget=budget)
    storage = bootstrap_hosted_storage(settings)
    try:
        dependencies = runtime.compose_hosted_dependencies(settings, storage)
        with TestClient(create_hosted_app(settings=settings, dependencies=dependencies)) as client:
            assert client.get("/health").status_code == 200
            response = client.get("/ready")
            assert response.status_code == 503 and response.json()["reasons"] == [reason]
            assert client.post("/v0.6/agent/run", json={"text": "motor"}).status_code == 503
            assert client.get(f"/v0.6/sources/{KB_UUID}:page:1").status_code == 200
    finally:
        storage.close()


@pytest.mark.parametrize("kind,local_id", [
    ("page", 1), ("knowledge_object", 1), ("knowledge_memory", 1), ("evidence", 1),
])
def test_real_source_reader(composition, kind, local_id):
    _, dependencies = composition
    result = dependencies.sources.get(f"{KB_UUID}:{kind}:{local_id}")
    assert result is not None and result.title


def test_audit_append_and_exhausted_budget_never_calls_transport(composition, monkeypatch):
    storage, dependencies = composition
    provider = dependencies.decision_provider._provider
    record = AiCallRecord(
        call_uuid="test-only-budget", capability="completion", model="test-only-model",
        prompt_sha256="a" * 64, input_chars=1, status="success", source_feature="test",
        total_tokens=100, created_at=datetime.now(UTC).isoformat(timespec="microseconds"),
    )
    provider._ledger.record(record)
    transport = Mock(side_effect=AssertionError("No transport after budget exhaustion"))
    monkeypatch.setattr(provider.wrapped, "_transport", transport)
    with pytest.raises(AIUnavailableError, match="预算"):
        provider.complete("TEST_ONLY_PRIVATE_PROMPT")
    transport.assert_not_called()
    with closing(sqlite3.connect(storage.database_path)) as db:
        rows = db.execute(
            "SELECT status,error_class,total_tokens FROM ai_calls ORDER BY id"
        ).fetchall()
        assert rows == [("success", None, 100), ("rejected", "budget", None)]
        assert "TEST_ONLY_PRIVATE_PROMPT" not in "\n".join(db.iterdump())


@pytest.mark.parametrize("daily,monthly", [(10, 0), (0, 10), (10, 20), (0, 0)])
def test_budget_utc_period_semantics(demo, monkeypatch, daily, monthly):
    from src.hosted.ai_runtime import HostedDatabaseAiBudgetGuard

    database = Mock()
    database.total_ai_tokens_since.return_value = 0
    guard = HostedDatabaseAiBudgetGuard(
        database, configured(demo, ai_daily_token_budget=daily, ai_monthly_token_budget=monthly),
    )
    if not daily and not monthly:
        with pytest.raises(AIUnavailableError):
            guard.ensure_allowed("completion")
        database.total_ai_tokens_since.assert_not_called()
        return
    guard.ensure_allowed("completion")
    starts = [datetime.fromisoformat(call.args[0])
              for call in database.total_ai_tokens_since.call_args_list]
    assert len(starts) == int(daily > 0) + int(monthly > 0)
    assert all(s.tzinfo == UTC and (s.hour, s.minute, s.second, s.microsecond) == (0, 0, 0, 0)
               for s in starts)
    if monthly:
        assert starts[-1].day == 1
    database.total_ai_tokens_since.return_value = 20
    with pytest.raises(AIUnavailableError):
        guard.ensure_allowed("completion")


def test_fake_transport_full_http_decision_tool_final_audit(composition, demo, monkeypatch):
    storage, dependencies = composition
    provider = dependencies.decision_provider._provider
    texts = [
        json.dumps({"kind": "call_tool", "tool_name": "page_search",
                    "arguments": {"query": "motor"}}),
        "PID motor control【来源 #1】。",
    ]

    def fake_transport(url, headers, payload, timeout):
        assert payload["enable_thinking"] is False
        return {"model": demo.settings.ai_llm_model,
                "choices": [{"message": {"content": texts.pop(0)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    transport = Mock(side_effect=fake_transport)
    monkeypatch.setattr(provider.wrapped, "_transport", transport)
    with TestClient(create_hosted_app(settings=demo.settings, dependencies=dependencies)) as client:
        response = client.post("/v0.6/agent/run", json={"text": "motor"})
        assert response.status_code == 200, response.text
        assert response.json()["grounded"] is True, response.text
    assert transport.call_count == 2
    with closing(sqlite3.connect(storage.database_path)) as db:
        assert db.execute("SELECT source_feature FROM ai_calls ORDER BY id").fetchall() == [
            ("agent_decision",), ("agent_final_answer",),
        ]
