"""WP2 readiness and source projection against disposable, synthetic v12 DBs."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic_settings.sources import DotEnvSettingsSource

from src.agent import AgentRequest
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.hosted.application import HostedDependencies
from src.hosted.readiness import HostedReadiness, ReadinessReason, check_hosted_database
from src.hosted_api.app import create_hosted_app
from src.hosted_config import HostedSettings, load_hosted_settings
from src.runtime_profile import RuntimeConfigurationError
from src.source_metadata import SourceMetadataService, safe_display_text


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("EKB_") or name == "DASHSCOPE_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setenv("EKB_RUNTIME_PROFILE", "hosted")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Readiness/source HTTP tests forbid network and dotenv")

    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: object) -> None:
        # Permit Windows asyncio's stdlib self-pipe, never application traffic.
        if sys._getframe(1).f_code is getattr(socket.socketpair, "__code__", None):
            return original_connect(sock, address)
        forbidden()

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", forbidden)


@pytest.fixture
def settings(tmp_path: Path) -> HostedSettings:
    return HostedSettings(
        runtime_profile="hosted",
        data_root=tmp_path,
        ai_api_key="TEST_ONLY_FAKE_KEY",
        ai_daily_token_budget=1000,
    )


@pytest.fixture
def database(settings: HostedSettings) -> Database:
    # Migration is fixture setup only; HTTP/readiness must never initialize it.
    return Database(settings.database_path)


def client_for(settings: HostedSettings, readiness: HostedReadiness, sources: object = None):
    agent = Mock()
    dependencies = HostedDependencies(readiness, agent, Mock(), AgentRequest, sources or Mock())
    return TestClient(create_hosted_app(settings=settings, dependencies=dependencies)), agent


def test_readiness_v12_is_readonly_and_health_has_no_io(
    settings: HostedSettings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
    files = set(settings.data_root.rglob("*"))
    original_connect = sqlite3.connect
    connections = []
    statements = []

    def readonly_connect(path: str, **kwargs: object):
        assert path == settings.database_path.as_uri() + "?mode=ro&immutable=1"
        assert kwargs["uri"] is True
        connections.append(path)
        db = original_connect(path, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    client, agent = client_for(settings, HostedReadiness(settings))
    assert client.get("/health").json() == {"status": "ok"}
    assert connections == []
    assert set(settings.data_root.rglob("*")) == files
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "status": "ready", "reasons": []}
    assert len(connections) == 1
    assert all(statement.startswith("SELECT ") for statement in statements)
    assert hashlib.sha256(settings.database_path.read_bytes()).hexdigest() == before
    # WP4 tightens this probe: a quiescent snapshot creates no sidecars; live WAL
    # must be observed through the explicit bootstrap-owned storage connection.
    assert set(settings.data_root.rglob("*")) == files
    agent.run.assert_not_called()


def test_absent_database_not_created(settings: HostedSettings) -> None:
    client, agent = client_for(settings, HostedReadiness(settings))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reasons"] == ["database_unavailable"]
    assert not settings.database_path.exists()
    assert not settings.database_dir.exists()
    assert client.post("/v0.6/agent/run", json={"text": "x"}).status_code == 503
    agent.run.assert_not_called()


@pytest.mark.parametrize("version", [None, 11, 14])
def test_incompatible_schema_not_migrated(settings: HostedSettings, version: int | None) -> None:
    settings.database_dir.mkdir()
    with sqlite3.connect(settings.database_path) as db:
        db.execute("CREATE TABLE schema_migrations (version INTEGER)")
        if version is not None:
            db.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    before = settings.database_path.read_bytes()
    client, _ = client_for(settings, HostedReadiness(settings))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reasons"] == ["schema_incompatible"]
    assert settings.database_path.read_bytes() == before


def test_corrupt_database_is_not_ready(settings: HostedSettings) -> None:
    settings.database_dir.mkdir()
    settings.database_path.write_bytes(b"synthetic non-sqlite data")
    assert check_hosted_database(settings.database_path) == ReadinessReason.SCHEMA_INCOMPATIBLE


def test_unreadable_database_is_not_ready(
    settings: HostedSettings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite3, "connect", Mock(side_effect=sqlite3.OperationalError("PRIVATE")))
    assert check_hosted_database(settings.database_path) == ReadinessReason.DATABASE_UNAVAILABLE


@pytest.mark.parametrize("key", ["", "   "])
def test_missing_key_not_ready_without_ai(
    settings: HostedSettings,
    database: Database,
    key: str,
) -> None:
    settings = HostedSettings(
        runtime_profile="hosted",
        data_root=settings.data_root,
        ai_api_key=key,
        ai_daily_token_budget=1,
    )
    client, agent = client_for(settings, HostedReadiness(settings))
    assert client.get("/ready").json()["reasons"] == ["ai_not_configured"]
    response = client.post("/v0.6/agent/run", json={"text": "x"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    agent.run.assert_not_called()


@pytest.mark.parametrize(
    ("daily", "monthly", "ready"), [(0, 0, False), (1, 0, True), (0, 1, True), (1, 1, True)]
)
def test_finite_budget_semantics(
    settings: HostedSettings,
    database: Database,
    daily: int,
    monthly: int,
    ready: bool,
) -> None:
    settings = HostedSettings(
        runtime_profile="hosted",
        data_root=settings.data_root,
        ai_api_key="TEST_ONLY_FAKE_KEY",
        ai_daily_token_budget=daily,
        ai_monthly_token_budget=monthly,
    )
    client, agent = client_for(settings, HostedReadiness(settings))
    response = client.get("/ready")
    assert response.status_code == (200 if ready else 503)
    assert response.json()["ready"] is ready
    if not ready:
        assert response.json()["reasons"] == ["budget_not_configured"]
        assert client.post("/v0.6/agent/run", json={"text": "x"}).status_code == 503
    agent.run.assert_not_called()


@pytest.mark.parametrize("budget", ["-1", "invalid", "1.5", ""])
def test_invalid_budget_fails_closed(
    settings: HostedSettings,
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    monkeypatch.setenv("EKB_DATA_ROOT", str(settings.data_root))
    monkeypatch.setenv("EKB_AI_DAILY_TOKEN_BUDGET", budget)
    with pytest.raises(RuntimeConfigurationError):
        load_hosted_settings()


def test_budget_uses_env_without_dotenv(settings: HostedSettings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EKB_DATA_ROOT", str(settings.data_root))
    monkeypatch.setenv("EKB_AI_DAILY_TOKEN_BUDGET", "100")
    monkeypatch.setenv("EKB_AI_MONTHLY_TOKEN_BUDGET", "200")
    loaded = load_hosted_settings()
    assert loaded.ai_daily_token_budget == 100 and loaded.ai_monthly_token_budget == 200


@pytest.mark.parametrize("profile", [None, "local", "", "Hosted", "unknown"])
def test_runtime_change_after_factory_fails_readiness(
    settings: HostedSettings,
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
) -> None:
    checker = Mock()
    client, agent = client_for(settings, HostedReadiness(settings, checker))
    if profile is None:
        monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    else:
        monkeypatch.setenv("EKB_RUNTIME_PROFILE", profile)
    assert client.get("/ready").json()["reasons"] == ["runtime_invalid"]
    assert client.post("/v0.6/agent/run", json={"text": "x"}).status_code == 503
    checker.assert_not_called()
    agent.run.assert_not_called()


def test_invalid_root_fails_before_db_check(settings: HostedSettings) -> None:
    invalid = settings.model_copy(update={"data_root": settings.data_root / "absent"})
    check = Mock()
    result = HostedReadiness(invalid, check).check()
    assert result.reasons == (ReadinessReason.RUNTIME_INVALID,)
    check.assert_not_called()


def test_all_four_db_backed_source_types_are_display_only(
    settings: HostedSettings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "TEST_ONLY_PRIVATE_RAW_CONTENT"
    (settings.data_root / "private.pdf").write_bytes(b"synthetic PDF fixture")
    (settings.data_root / "private.png").write_bytes(b"synthetic image fixture")
    document = database.create_document(
        title="公开手册",
        filename="private.pdf",
        source_path=settings.data_root / "private.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=settings.data_root / "private.png",
        extracted_text=private,
    )
    ko = database.create_knowledge_object(kind="concept", title="公开概念", content=private)
    memory = database.create_knowledge_memory_entry(
        kind="experience", title="公开经验", content=private
    )
    evidence_service = EvidenceBasketService(database)
    evidence = evidence_service.add_item(
        document_id=document.id, page_id=page.id, evidence_text=private, user_note=private
    )
    kb_uuid = database.get_knowledge_base_uuid()
    sources = SourceMetadataService(
        kb_uuid,
        database.get_page,
        database.get_document,
        database.get_knowledge_object,
        database.get_knowledge_memory_entry,
        evidence_service.get_item,
    )
    before = settings.database_path.read_bytes()
    # Setup has finished. Permit only SQL reads and transaction control during requests.
    original_connect = sqlite3.connect
    reads = []

    def guarded_connect(*args: object, **kwargs: object):
        connection = original_connect(*args, **kwargs)

        def authorize(action: int, arg1: str, arg2: str, *unused: object) -> int:
            if action == sqlite3.SQLITE_PRAGMA and (arg1, arg2) in {
                ("foreign_keys", "ON"),
                ("busy_timeout", "30000"),
            }:
                return sqlite3.SQLITE_OK
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_RECURSIVE,
            }
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        connection.set_trace_callback(reads.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    client, agent = client_for(settings, HostedReadiness(settings), sources)
    for kind, local_id, title in [
        ("page", page.id, "公开手册"),
        ("knowledge_object", ko.id, "公开概念"),
        ("knowledge_memory", memory.id, "公开经验"),
        ("evidence", evidence.id, "公开手册"),
    ]:
        stable_id = f"{kb_uuid}:{kind}:{local_id}"
        response = client.get("/v0.6/sources/" + stable_id)
        assert response.status_code == 200
        assert set(response.json()) == {"stable_id", "type", "title", "label"}
        assert response.json()["stable_id"] == stable_id
        assert response.json()["title"] == title
        assert private not in response.text and "private.pdf" not in response.text
        assert str(settings.data_root) not in response.text and "a" * 64 not in response.text
    assert reads
    assert settings.database_path.read_bytes() == before
    agent.run.assert_not_called()
    reads.clear()
    assert sources.get("ffffffff-ffff-ffff-ffff-ffffffffffff:page:1") is None
    assert sources.get(f"{kb_uuid}:knowledge_source:1") is None
    assert reads == []
    for kind in ("page", "knowledge_object", "knowledge_memory", "evidence"):
        assert sources.get(f"{kb_uuid}:{kind}:99999") is None
    orphan = replace(sources, read_document=lambda _: None)
    assert orphan.get(f"{kb_uuid}:page:{page.id}") is None


@pytest.mark.parametrize(
    "title", [r"C:\private\x.pdf", "/private/x.pdf", "../x", "x\ny", "x" * 501]
)
def test_unsafe_legacy_display_omitted(title: str) -> None:
    assert safe_display_text(title) is None
