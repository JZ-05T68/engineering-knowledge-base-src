"""WP2 HTTP transport tests: real frozen pipeline, fake models, in-process HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic_settings.sources import DotEnvSettingsSource

from src.agent import (
    AgentRequest,
    FinalAnswerStage,
    ModelDecisionProvider,
    SingleStepAgentExecutor,
    SingleStepAgentService,
)
from src.agent.response import (
    AgentResponse,
    AgentResponseError,
    AgentResponseErrorCode,
    AgentResponseStatus,
)
from src.agent.tools import ToolReference, ToolResult, ToolResultStatus, build_phase1_registry
from src.ai.provider import CompletionResult, CompletionUsage
from src.ai.rag_answer_service import RagAnswerService
from src.hosted.application import HostedDependencies
from src.hosted.readiness import ReadinessReason, ReadinessResult
from src.hosted_api.app import create_hosted_app
from src.hosted_config import HostedSettings
from src.knowledge_context_packager import KnowledgeContextPackager
from src.models import ContextItemType
from src.runtime_profile import RuntimeConfigurationError
from src.source_metadata import SourceMetadata

KB = "12345678-1234-1234-1234-123456789abc"
SOURCE = f"{KB}:page:1"
PRIVATE = r"C:\private\secret\file.db Bearer TEST_ONLY_PRIVATE_MARKER"
PUBLIC_FIELDS = {"request_id", "status", "answer", "grounded", "citations", "warnings", "error"}


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("EKB_") or name == "DASHSCOPE_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", "hosted")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("HTTP tests forbid network and dotenv access")

    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: object) -> None:
        # Windows asyncio creates its self-pipe through socket.socketpair().
        # Allow only that stdlib caller; all application connections still fail.
        if sys._getframe(1).f_code is getattr(socket.socketpair, "__code__", None):
            return original_connect(sock, address)
        forbidden()

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", forbidden)


@pytest.fixture
def setup(tmp_path: Path) -> SimpleNamespace:
    settings = HostedSettings(runtime_profile="hosted", data_root=tmp_path)
    agent = Mock(spec=SingleStepAgentService)
    agent.run.return_value = AgentResponse(
        status=AgentResponseStatus.COMPLETED,
        answer="safe answer",
        grounded=True,
        citations=(SOURCE,),
        context_stable_ids=(PRIVATE,),
        model=PRIVATE,
        token_usage=CompletionUsage(10, 5, 15),
    )
    readiness = Mock()
    readiness.check.return_value = ReadinessResult()
    sources = Mock()
    sources.get.return_value = SourceMetadata(SOURCE, ContextItemType.PAGE, "手册", "第 1 页")
    dependencies = HostedDependencies(readiness, agent, Mock(), AgentRequest, sources)
    app = create_hosted_app(settings=settings, dependencies=dependencies)
    return SimpleNamespace(
        settings=settings,
        dependencies=dependencies,
        agent=agent,
        readiness=readiness,
        sources=sources,
        app=app,
        client=TestClient(app),
    )


def test_factory_is_lazy_and_exposes_only_four_business_routes(setup: SimpleNamespace) -> None:
    setup.readiness.check.assert_not_called()
    setup.agent.run.assert_not_called()
    setup.sources.get.assert_not_called()
    routes = {(route.path, tuple(sorted(route.methods))) for route in setup.app.routes}
    assert routes == {
        ("/health", ("GET",)),
        ("/ready", ("GET",)),
        ("/v0.6/agent/run", ("POST",)),
        ("/v0.6/sources/{stable_id}", ("GET",)),
    }


@pytest.mark.parametrize("profile", [None, "local", "", " ", "Hosted", "HOSTED", "cloud"])
def test_factory_requires_explicit_hosted(
    setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
) -> None:
    if profile is None:
        monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    else:
        monkeypatch.setenv("EKB_RUNTIME_PROFILE", profile)
    with pytest.raises(RuntimeConfigurationError):
        create_hosted_app(settings=setup.settings, dependencies=setup.dependencies)


def test_factory_rejects_non_hosted_settings(setup: SimpleNamespace) -> None:
    with pytest.raises(RuntimeConfigurationError):
        create_hosted_app(settings=object(), dependencies=setup.dependencies)


def test_server_id_and_explicit_public_response(setup: SimpleNamespace) -> None:
    response = setup.client.post(
        "/v0.6/agent/run", json={"text": "问题", "correlation_id": "client_1"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == PUBLIC_FIELDS
    assert payload["status"] == "completed" and payload["grounded"] is True
    assert payload["citations"] == [SOURCE]
    assert UUID(payload["request_id"]).version == 4
    assert response.headers["X-Correlation-ID"] == "client_1"
    request, provider = setup.agent.run.call_args.args
    assert isinstance(request, AgentRequest)
    assert request.request_id == payload["request_id"] != "client_1"
    assert request.text == "问题" and not hasattr(request, "correlation_id")
    assert provider is setup.dependencies.decision_provider
    assert PRIVATE not in response.text
    second = setup.client.post("/v0.6/agent/run", json={"text": "问题"})
    assert second.json()["request_id"] != payload["request_id"]


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "tool",
        "tool_name",
        "model",
        "provider",
        "api_key",
        "budget",
        "role",
        "permission",
        "database_path",
        "file_path",
        "max_tokens",
        "temperature",
        "retry",
        "system_prompt",
        "unknown",
    ],
)
def test_unknown_or_authority_fields_rejected(setup: SimpleNamespace, field: str) -> None:
    response = setup.client.post("/v0.6/agent/run", json={"text": PRIVATE, field: PRIVATE})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert PRIVATE not in response.text
    setup.agent.run.assert_not_called()
    setup.readiness.check.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": None}, {"text": 123}, {"text": True}, {"text": []}, {"text": {}}, [], "prompt"],
)
def test_invalid_input_types(setup: SimpleNamespace, payload: object) -> None:
    assert setup.client.post("/v0.6/agent/run", json=payload).status_code == 422
    setup.agent.run.assert_not_called()


def test_malformed_json_is_sanitized(setup: SimpleNamespace) -> None:
    response = setup.client.post(
        "/v0.6/agent/run",
        content='{"text":"' + PRIVATE,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "TEST_ONLY_PRIVATE_MARKER" not in response.text


@pytest.mark.parametrize(
    "text",
    ["", "   ", "a" * 120_000, "汉" * 120_000],
    ids=["empty", "whitespace", "ascii-limit", "unicode-limit"],
)
def test_authoritative_agent_limit_preserves_valid_text(setup: SimpleNamespace, text: str) -> None:
    assert setup.client.post("/v0.6/agent/run", json={"text": text}).status_code == 200
    assert setup.agent.run.call_args.args[0].text == text


@pytest.mark.parametrize(
    "text", ["a" * 120_001, "汉" * 120_001], ids=["ascii-over-limit", "unicode-over-limit"]
)
def test_authoritative_agent_limit_rejects_oversize(setup: SimpleNamespace, text: str) -> None:
    assert setup.client.post("/v0.6/agent/run", json={"text": text}).status_code == 422
    setup.agent.run.assert_not_called()
    setup.readiness.check.assert_not_called()


@pytest.mark.parametrize(
    "correlation", ["", "x" * 129, " ", "a\n", "x\r\nInjected:1", "../path", PRIVATE, "汉", 1, []]
)
def test_unsafe_correlation_rejected(setup: SimpleNamespace, correlation: object) -> None:
    assert (
        setup.client.post(
            "/v0.6/agent/run",
            json={
                "text": "x",
                "correlation_id": correlation,
            },
        ).status_code
        == 422
    )
    setup.agent.run.assert_not_called()


@pytest.mark.parametrize("correlation", [None, "x" * 128, "A.z-9_0"])
def test_safe_optional_correlation(setup: SimpleNamespace, correlation: str | None) -> None:
    assert (
        setup.client.post(
            "/v0.6/agent/run",
            json={
                "text": "x",
                "correlation_id": correlation,
            },
        ).status_code
        == 200
    )


@pytest.mark.parametrize("code", list(AgentResponseErrorCode))
def test_failed_agent_is_200_sanitized_without_retry(setup: SimpleNamespace, code: str) -> None:
    setup.agent.run.return_value = AgentResponse(
        status=AgentResponseStatus.FAILED,
        answer="",
        grounded=False,
        error=AgentResponseError(code, PRIVATE, PRIVATE),
        warnings=(PRIVATE,),
    )
    response = setup.client.post("/v0.6/agent/run", json={"text": "x"})
    assert response.status_code == 200
    assert set(response.json()) == PUBLIC_FIELDS
    assert response.json()["status"] == "failed"
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["warnings"]
    assert PRIVATE not in response.text
    setup.agent.run.assert_called_once()


@pytest.mark.parametrize(
    "stable_id",
    [
        "bad",
        "1",
        f"{KB}:page:0",
        f"{KB}:page:-1",
        f"{KB}:page:+1",
        f"{KB}:page:01",
        f"{KB}:page:1 ",
        f" {SOURCE}",
        f"{KB.upper()}:page:1",
        f"{KB}:PAGE:1",
        f"{KB}:page:9223372036854775808",
        f"{KB}:page:1' OR 1=1",
        "..",
        "../private.db",
        r"..\private.db",
        r"C:\private\file.db",
        "/etc/passwd",
        "file:///private.db",
    ],
)
def test_invalid_ids_paths_and_traversal_never_reach_reader(
    setup: SimpleNamespace,
    stable_id: str,
) -> None:
    response = setup.client.get("/v0.6/sources/" + quote(stable_id, safe=""))
    assert response.status_code in {404, 422}
    setup.sources.get.assert_not_called()
    assert "private.db" not in response.text


def test_valid_source_display_projection(setup: SimpleNamespace) -> None:
    response = setup.client.get("/v0.6/sources/" + SOURCE)
    assert response.status_code == 200
    assert response.json() == {
        "stable_id": SOURCE,
        "type": "page",
        "title": "手册",
        "label": "第 1 页",
    }
    setup.sources.get.assert_called_once_with(SOURCE)


@pytest.mark.parametrize("kind", ["knowledge_source", "relation", "revision", "unknown"])
def test_unsupported_canonical_type_is_404_without_read(setup: SimpleNamespace, kind: str) -> None:
    assert setup.client.get(f"/v0.6/sources/{KB}:{kind}:1").status_code == 404
    setup.sources.get.assert_not_called()


def test_missing_source_is_404(setup: SimpleNamespace) -> None:
    setup.sources.get.return_value = None
    assert setup.client.get("/v0.6/sources/" + SOURCE).status_code == 404


def test_pathlike_legacy_metadata_omitted(setup: SimpleNamespace) -> None:
    setup.sources.get.return_value = SourceMetadata(SOURCE, ContextItemType.PAGE, PRIVATE, PRIVATE)
    payload = setup.client.get("/v0.6/sources/" + SOURCE).json()
    assert payload["title"] is None and payload["label"] is None


@pytest.mark.parametrize("target", ["agent", "source", "ready", "projection"])
def test_unexpected_exception_sanitized_in_response_and_adapter_log(
    setup: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    target: str,
) -> None:
    if target == "agent":
        setup.agent.run.side_effect = RuntimeError(PRIVATE)
    elif target == "source":
        setup.sources.get.side_effect = RuntimeError(PRIVATE)
    elif target == "ready":
        setup.readiness.check.side_effect = RuntimeError(PRIVATE)
    else:
        setup.agent.run.return_value = object()
    if target in {"agent", "projection"}:
        response = setup.client.post("/v0.6/agent/run", json={"text": PRIVATE})
    else:
        response = setup.client.get("/ready" if target == "ready" else "/v0.6/sources/" + SOURCE)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_failure"
    assert PRIVATE not in response.text and PRIVATE not in caplog.text
    assert "Traceback" not in caplog.text
    assert response.json()["request_id"] in caplog.text


def test_mismatched_source_projection_fails_closed(setup: SimpleNamespace) -> None:
    setup.sources.get.return_value = SourceMetadata(f"{KB}:page:2", ContextItemType.PAGE)
    assert setup.client.get("/v0.6/sources/" + SOURCE).status_code == 500


@pytest.mark.parametrize(
    "missing", ["agent_service", "decision_provider", "request_factory", "sources"]
)
def test_missing_composition_is_503_health_remains_alive(
    setup: SimpleNamespace, missing: str
) -> None:
    client = TestClient(
        create_hosted_app(
            settings=setup.settings,
            dependencies=replace(setup.dependencies, **{missing: None}),
        )
    )
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["reasons"] == ["composition_unavailable"]
    assert client.post("/v0.6/agent/run", json={"text": "x"}).status_code == 503
    setup.agent.run.assert_not_called()


@pytest.mark.parametrize("reason", list(ReadinessReason))
def test_not_ready_blocks_agent(setup: SimpleNamespace, reason: ReadinessReason) -> None:
    setup.readiness.check.return_value = ReadinessResult((reason,))
    assert setup.client.get("/ready").status_code == 503
    response = setup.client.post("/v0.6/agent/run", json={"text": "x"})
    assert response.status_code == 503
    expected = (
        "provider_unavailable"
        if reason == ReadinessReason.AI_NOT_CONFIGURED
        else "runtime_unavailable"
    )
    assert response.json()["error"]["code"] == expected
    setup.agent.run.assert_not_called()


def test_health_does_not_touch_dependencies_and_ready_is_200(setup: SimpleNamespace) -> None:
    assert setup.client.get("/health").json() == {"status": "ok"}
    setup.readiness.check.assert_not_called()
    setup.agent.run.assert_not_called()
    setup.sources.get.assert_not_called()
    assert setup.client.get("/ready").json() == {"ready": True, "status": "ready", "reasons": []}


@pytest.mark.parametrize("invalid_final", [False, True])
def test_http_runs_frozen_pipeline_with_real_citation_validation(
    setup: SimpleNamespace,
    invalid_final: bool,
) -> None:
    registry = build_phase1_registry()
    decision_model = Mock()
    decision_model.complete.return_value = CompletionResult(
        text='{"kind":"CALL_TOOL","tool_name":"page_search","arguments":{"query":"x"}}',
        model="fake-decision",
    )
    handler = Mock(
        return_value=ToolResult(
            status=ToolResultStatus.SUCCESS,
            data={"results": [{"id": 1, "document_title": "手册", "snippet": "WP2_OK"}]},
            references=(ToolReference(stable_id=SOURCE, anchor_label="第 1 页"),),
        )
    )
    final_model = Mock()
    final_model.complete.return_value = CompletionResult(
        text="答案是 WP2_OK。依据：【来源 #99】。"
        if invalid_final
        else "答案是 WP2_OK。依据：【来源 #1】。",
        model="fake-final",
        usage=CompletionUsage(10, 5, 15),
    )
    pipeline = SingleStepAgentService(
        SingleStepAgentExecutor(
            registry, handlers={item.name: handler for item in registry.list_definitions()}
        ),
        FinalAnswerStage(
            RagAnswerService(final_model),
            packager=KnowledgeContextPackager(
                kb_uuid=KB,
                app_version="test",
            ),
        ),
    )
    recording_service = Mock(wraps=pipeline)
    dependencies = replace(
        setup.dependencies,
        agent_service=recording_service,
        decision_provider=ModelDecisionProvider(decision_model, registry),
    )
    client = TestClient(create_hosted_app(settings=setup.settings, dependencies=dependencies))
    response = client.post("/v0.6/agent/run", json={"text": "WP2_OK是什么？"})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == PUBLIC_FIELDS
    assert payload["status"] == ("failed" if invalid_final else "completed")
    assert payload["grounded"] is (not invalid_final)
    if invalid_final:
        assert payload["error"]["code"] == "citation_invalid"
        assert "#99" not in response.text
    else:
        assert payload["citations"] == [SOURCE]
    decision_model.complete.assert_called_once()
    final_model.complete.assert_called_once()
    handler.assert_called_once()
    recording_service.run.assert_called_once()


def test_fresh_import_is_io_safe_and_has_no_write_service_imports(tmp_path: Path) -> None:
    script = """
import os, sqlite3, socket
from pydantic_settings.sources import DotEnvSettingsSource
def fail(*args, **kwargs):
    raise AssertionError("Hosted import performed forbidden I/O")
sqlite3.connect = socket.create_connection = fail
socket.socket.connect = fail
DotEnvSettingsSource._read_env_file = fail
os.environ["EKB_RUNTIME_PROFILE"] = "INVALID_MUST_NOT_BE_READ_ON_IMPORT"
import src.hosted_api.app
import sys
for module in (
    "src.agent", "src.runtime", "src.database", "src.knowledge_object_service",
    "src.knowledge_memory_service", "src.evidence_basket_service",
    "src.document_service", "streamlit",
):
    assert module not in sys.modules, module
assert not hasattr(src.hosted_api.app, "app")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
