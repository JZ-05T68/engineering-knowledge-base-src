"""WP3 attack matrices with in-process ASGI, a fake clock and event-driven concurrency."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_network
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from test_hosted_api import PRIVATE, SOURCE
from test_hosted_api import offline as offline  # reuse the network/dotenv guard
from test_hosted_api import setup as setup  # reuse frozen Agent-shaped fake dependencies

from src.agent.response import AgentResponse, AgentResponseError, AgentResponseErrorCode
from src.hosted.readiness import ReadinessReason, ReadinessResult
from src.hosted.security import RateLimiter, effective_client_ip
from src.hosted_api.app import create_hosted_app
from src.hosted_api.security import (
    MAX_HTTP_BODY_BYTES,
    IngressSecurityMiddleware,
    RequestIdentityMiddleware,
)
from src.hosted_config import HostedSettings

AGENT = "/v0.6/agent/run"
SOURCES = "/v0.6/sources/"
ORIGIN = "https://demo.example.com"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def app_for(setup: SimpleNamespace, *, clock: object = None, **values: object):
    settings = HostedSettings(
        runtime_profile="hosted", data_root=setup.settings.data_root, **values
    )
    return create_hosted_app(
        settings=settings, dependencies=setup.dependencies, security_clock=clock or Clock()
    )


def client_for(app: object, ip: str = "198.51.100.20") -> TestClient:
    return TestClient(app, client=(ip, 12345))


def assert_failure(response: object, status: int, code: str) -> None:
    assert response.status_code == status
    payload = response.json()
    assert set(payload) == {"request_id", "status", "error"}
    assert UUID(payload["request_id"]).version == 4
    assert payload["status"] == "failed"
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    assert PRIVATE not in response.text


def test_agent_rate_first_ten_then_429_and_window_reset(setup: SimpleNamespace) -> None:
    clock = Clock()
    app = app_for(setup, clock=clock)
    client = client_for(app)
    for _ in range(10):
        assert client.post(AGENT, json={"text": "x"}).status_code == 200
    blocked = client.post(AGENT, json={"text": PRIVATE})
    assert_failure(blocked, 429, "rate_limited")
    assert blocked.headers["Retry-After"] == "60"
    assert setup.agent.run.call_count == 10
    assert setup.readiness.check.call_count == 10
    clock.now = 59.5
    assert client.post(AGENT, json={"text": "x"}).headers["Retry-After"] == "1"
    clock.now = 60
    assert client.post(AGENT, json={"text": "x"}).status_code == 200
    assert setup.agent.run.call_count == 11


def test_source_sixty_then_429_and_agent_bucket_independent(setup: SimpleNamespace) -> None:
    client = client_for(app_for(setup))
    for _ in range(60):
        assert client.get(SOURCES + SOURCE).status_code == 200
    assert_failure(client.get(SOURCES + SOURCE), 429, "rate_limited")
    assert setup.sources.get.call_count == 60
    assert client.post(AGENT, json={"text": "x"}).status_code == 200


def test_health_ready_and_options_are_exempt(setup: SimpleNamespace) -> None:
    app = app_for(setup, agent_rate_limit_per_minute=1, source_rate_limit_per_minute=1)
    client = client_for(app)
    for _ in range(12):
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.options(AGENT).status_code == 405
    setup.agent.run.assert_not_called()
    setup.sources.get.assert_not_called()
    assert setup.readiness.check.call_count == 12
    assert app.state.rate_limiter.bucket_count == 0


@pytest.mark.parametrize("invalid", ["json", "extra", "source"])
def test_validation_failures_still_consume_rate(setup: SimpleNamespace, invalid: str) -> None:
    client = client_for(
        app_for(setup, agent_rate_limit_per_minute=1, source_rate_limit_per_minute=1)
    )
    if invalid == "source":
        first = client.get(SOURCES + "invalid")
        second = client.get(SOURCES + "different-invalid")
    elif invalid == "json":
        first = client.post(AGENT, content=b"{")
        second = client.post(AGENT, content=b"{")
    else:
        first = client.post(AGENT, json={"text": "x", "tool": PRIVATE})
        second = client.post(AGENT, json={"text": "x", "model": PRIVATE})
    assert first.status_code == 422
    assert_failure(second, 429, "rate_limited")
    setup.agent.run.assert_not_called()
    setup.sources.get.assert_not_called()
    setup.readiness.check.assert_not_called()


@pytest.mark.parametrize("kind", ["agent", "source"])
def test_server_root_path_cannot_bypass_route_rate_limit(setup: SimpleNamespace, kind: str) -> None:
    app = app_for(setup, agent_rate_limit_per_minute=1, source_rate_limit_per_minute=1)
    client = TestClient(app, root_path="/demo", client=("198.51.100.1", 123))
    if kind == "agent":
        first = client.post("/demo" + AGENT, json={"text": "x"})
        second = client.post("/demo" + AGENT, json={"text": "x"})
    else:
        first = client.get("/demo" + SOURCES + SOURCE)
        second = client.get("/demo" + SOURCES + SOURCE)
    assert first.status_code == 200
    assert_failure(second, 429, "rate_limited")


def test_untrusted_headers_cannot_rotate_buckets_and_direct_clients_are_separate(
    setup: SimpleNamespace,
) -> None:
    app = app_for(setup, agent_rate_limit_per_minute=1)
    first = client_for(app, "198.51.100.1")
    second = client_for(app, "198.51.100.2")
    for index in range(3):
        response = first.post(
            AGENT,
            json={"text": "x"},
            headers={
                "X-Forwarded-For": f"203.0.113.{index}",
                "X-Real-IP": f"203.0.113.{index}",
                "Forwarded": f"for=203.0.113.{index}",
            },
        )
        assert response.status_code == (200 if index == 0 else 429)
    assert second.post(AGENT, json={"text": "x"}).status_code == 200


def test_trusted_proxy_client_separation_and_rightmost_untrusted_hop(
    setup: SimpleNamespace,
) -> None:
    client = client_for(
        app_for(setup, agent_rate_limit_per_minute=1, trusted_proxy_cidrs="10.0.0.0/8"), "10.0.0.1"
    )
    for forged in ("192.0.2.1", "192.0.2.2"):
        response = client.post(
            AGENT,
            json={"text": "x"},
            headers={
                "X-Forwarded-For": f"{forged}, 198.51.100.1, 10.0.0.2",
            },
        )
        assert response.status_code == (200 if forged.endswith(".1") else 429)
    assert (
        client.post(
            AGENT,
            json={"text": "x"},
            headers={
                "X-Forwarded-For": "198.51.100.2, 10.0.0.2",
            },
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("peer", "forwarded", "networks", "expected"),
    [
        ("198.51.100.1", [b"203.0.113.1"], [], "198.51.100.1"),
        ("198.51.100.1", [b"203.0.113.1"], ["10.0.0.0/8"], "198.51.100.1"),
        ("10.0.0.1", [b"198.51.100.1"], ["10.0.0.0/8"], "198.51.100.1"),
        ("10.0.0.1", [b"192.0.2.1, 198.51.100.1, 10.0.0.2"], ["10.0.0.0/8"], "198.51.100.1"),
        (
            "2001:db8:1::1",
            [b"2001:db8:2::123, 2001:db8:1::2"],
            ["2001:db8:1::/48"],
            "2001:db8:2::123",
        ),
        ("::ffff:198.51.100.1", [], [], "198.51.100.1"),
        ("10.0.0.1", [b"10.0.0.2"], ["10.0.0.0/8"], "10.0.0.1"),
        (None, [b"198.51.100.1"], ["10.0.0.0/8"], "unknown-peer"),
        ("not-an-ip", [b"198.51.100.1"], ["10.0.0.0/8"], "unknown-peer"),
        ("fe80::1%unsafe", [], [], "unknown-peer"),
    ],
)
def test_client_ip_authority(peer, forwarded, networks, expected) -> None:
    assert (
        effective_client_ip(peer, forwarded, tuple(ip_network(item) for item in networks))
        == expected
    )


@pytest.mark.parametrize(
    "forwarded",
    [
        [],
        [b""],
        [b"garbage"],
        [b"198.51.100.1\r\nInjected: 1"],
        [b"bad::ipv6"],
        [b"198.51.100.1:80"],
        [b"fe80::1%eth0"],
        [b"198.51.100.1,"],
        [b"\xff"],
        [b"198.51.100.1", b"198.51.100.2"],
        [b"x" * 4097],
        [b",".join([b"10.0.0.2"] * 33)],
    ],
    ids=[
        "absent",
        "empty",
        "garbage",
        "crlf",
        "ipv6",
        "port",
        "zone",
        "empty-hop",
        "non-ascii",
        "duplicate",
        "long",
        "many-hops",
    ],
)
def test_invalid_proxy_header_falls_back_to_same_direct_bucket(forwarded) -> None:
    assert effective_client_ip("10.0.0.1", forwarded, (ip_network("10.0.0.0/8"),)) == "10.0.0.1"


def test_stale_keys_capacity_fail_closed_and_cleanup() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 1, clock=clock, max_buckets=2)
    assert limiter.admit("agent", "a") is None
    assert limiter.admit("agent", "b") is None
    assert limiter.admit("agent", "c") == 60
    assert limiter.bucket_count == 2
    assert limiter.admit("agent", "a") == 60  # no eviction/reset of live buckets
    clock.now = 60
    assert limiter.admit("agent", "c") is None
    assert limiter.bucket_count == 1
    clock.now = 120
    limiter.prune()
    assert limiter.bucket_count == 0


def test_rate_counter_is_thread_safe() -> None:
    limiter = RateLimiter(10, 60, clock=Clock())
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.admit("agent", "same"), range(100)))
    assert results.count(None) == 10
    assert results.count(60) == 90
    assert limiter.bucket_count == 1


def test_malformed_proxy_variants_share_one_http_bucket(setup: SimpleNamespace) -> None:
    client = client_for(
        app_for(setup, agent_rate_limit_per_minute=1, trusted_proxy_cidrs="10.0.0.0/8"),
        "10.0.0.1",
    )
    for index, value in enumerate(("invalid-a", "invalid-b", "bad::ipv6")):
        response = client.post(AGENT, json={"text": "x"}, headers={"X-Forwarded-For": value})
        assert response.status_code == (200 if index == 0 else 429)
    assert setup.agent.run.call_count == 1


def test_ipv6_equivalent_spellings_share_http_quota(setup: SimpleNamespace) -> None:
    client = client_for(
        app_for(setup, agent_rate_limit_per_minute=1, trusted_proxy_cidrs="2001:db8:1::/48"),
        "2001:db8:1::1",
    )
    for index, address in enumerate(("2001:db8:2::123", "2001:0db8:0002:0:0:0:0:0123")):
        response = client.post(AGENT, json={"text": "x"}, headers={"X-Forwarded-For": address})
        assert response.status_code == (200 if index == 0 else 429)
    assert setup.agent.run.call_count == 1


def direct_asgi(app, chunks, *, headers=(), path=AGENT, method="POST", disconnect=False):
    """Handcrafted ASGI receives, with a counter proving overflow stops consumption."""
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    if disconnect:
        messages = [{"type": "http.disconnect"}]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("198.51.100.90", 123),
        "server": ("testserver", 80),
    }
    sent = []
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next((message for message in sent if message["type"] == "http.response.start"), None)
    body = b"".join(message.get("body", b"") for message in sent)
    return SimpleNamespace(
        status=start["status"] if start else None,
        headers=dict(start["headers"]) if start else {},
        body=body,
        received=received,
        scope=scope,
    )


@pytest.mark.parametrize("size", [511999, 512000, 512001])
def test_exact_body_boundary_counts_json_envelope(setup: SimpleNamespace, size: int) -> None:
    body = b'{"text":"x"}'
    body += b" " * (size - len(body))
    result = direct_asgi(
        app_for(setup), [body[:100], body[100:]], headers=[(b"content-type", b"application/json")]
    )
    assert result.status == (200 if size <= MAX_HTTP_BODY_BYTES else 413)
    assert setup.agent.run.call_count == (1 if size <= MAX_HTTP_BODY_BYTES else 0)
    if result.status == 413:
        payload = json.loads(result.body)
        assert payload["error"]["code"] == "request_too_large"
        assert UUID(payload["request_id"]).version == 4


@pytest.mark.parametrize("length", [None, b"1", b"garbage", b"-1"])
def test_actual_overflow_stops_receiving_before_downstream(setup: SimpleNamespace, length) -> None:
    headers = [] if length is None else [(b"content-length", length)]
    result = direct_asgi(
        app_for(setup), [b"a" * 512000, b"b", b"UNREAD_TRAILING_BODY"], headers=headers
    )
    assert result.status == 413 and result.received == 2
    setup.agent.run.assert_not_called()
    setup.readiness.check.assert_not_called()


@pytest.mark.parametrize("length", [b"512001", b"99999999999999999999", b"000512001"])
def test_large_content_length_rejected_without_receiving(setup: SimpleNamespace, length) -> None:
    result = direct_asgi(app_for(setup), [b"{}"], headers=[(b"content-length", length)])
    assert result.status == 413 and result.received == 0
    setup.agent.run.assert_not_called()


def test_utf8_bytes_and_agent_character_limit_remain_distinct(setup: SimpleNamespace) -> None:
    client = client_for(app_for(setup))
    assert client.post(AGENT, json={"text": "😀" * 100000}).status_code == 200
    assert client.post(AGENT, json={"text": "a" * 120001}).status_code == 422
    body = json.dumps({"text": "汉" * 171000}, ensure_ascii=False).encode("utf-8")
    assert len(body) > 512000
    assert (
        client.post(AGENT, content=body, headers={"Content-Type": "application/json"}).status_code
        == 413
    )
    assert setup.agent.run.call_count == 1


def test_body_limit_also_applies_to_ignored_get_bodies(setup: SimpleNamespace) -> None:
    result = direct_asgi(app_for(setup), [b"x" * 512001], path="/health", method="GET")
    assert result.status == 413
    setup.readiness.check.assert_not_called()


def test_disconnect_before_body_complete_never_runs_agent(setup: SimpleNamespace) -> None:
    result = direct_asgi(app_for(setup), [], disconnect=True)
    assert result.status is None
    setup.agent.run.assert_not_called()


def test_cors_exact_allowlist_no_credentials_and_no_auth(setup: SimpleNamespace) -> None:
    client = client_for(app_for(setup, cors_allowed_origins=ORIGIN))
    for origin in (ORIGIN, "https://evil.example.com", "null", ORIGIN + "/path"):
        response = client.post(
            AGENT,
            json={"text": "x", "correlation_id": "client_1"},
            headers={"Origin": origin, "Authorization": "Bearer TEST_ONLY_NO_AUTH"},
        )
        assert response.status_code == 200  # CORS is not authorization
        assert response.headers.get("Access-Control-Allow-Origin") == (
            ORIGIN if origin == ORIGIN else None
        )
        assert "Access-Control-Allow-Credentials" not in response.headers
    assert client.post(AGENT, json={"text": "x"}).status_code == 200


def test_empty_cors_does_not_grant_cross_origin(setup: SimpleNamespace) -> None:
    response = client_for(app_for(setup)).post(
        AGENT, json={"text": "x"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_bypasses_rate_body_readiness_and_agent(setup: SimpleNamespace) -> None:
    app = app_for(setup, cors_allowed_origins=ORIGIN, agent_rate_limit_per_minute=1)
    client = client_for(app)
    for _ in range(12):
        response = client.options(
            AGENT,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
        assert "*" not in response.headers["Access-Control-Allow-Headers"]
        assert "Access-Control-Allow-Credentials" not in response.headers
    assert app.state.rate_limiter.bucket_count == 0
    setup.readiness.check.assert_not_called()
    setup.agent.run.assert_not_called()
    setup.sources.get.assert_not_called()
    assert client.post(AGENT, json={"text": "x"}).status_code == 200


@pytest.mark.parametrize("bad", ["origin", "method", "header"])
def test_disallowed_preflight_uses_standard_cors_semantics(
    setup: SimpleNamespace, bad: str
) -> None:
    headers = {
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
    }
    headers[
        {
            "origin": "Origin",
            "method": "Access-Control-Request-Method",
            "header": "Access-Control-Request-Headers",
        }[bad]
    ] = {
        "origin": "https://evil.example.com",
        "method": "DELETE",
        "header": "Authorization",
    }[bad]
    response = client_for(app_for(setup, cors_allowed_origins=ORIGIN)).options(
        AGENT, headers=headers
    )
    assert response.status_code == 400
    if bad == "origin":
        assert "Access-Control-Allow-Origin" not in response.headers
    setup.agent.run.assert_not_called()


def test_order_cors_covers_413_429_and_rate_precedes_body(setup: SimpleNamespace) -> None:
    app = app_for(setup, cors_allowed_origins=ORIGIN, agent_rate_limit_per_minute=1)
    assert [item.cls for item in app.user_middleware] == [
        RequestIdentityMiddleware,
        CORSMiddleware,
        IngressSecurityMiddleware,
    ]
    client = client_for(app)
    headers = {"Origin": ORIGIN, "Content-Length": "512001", "X-Request-ID": "CLIENT_NOT_AUTHORITY"}
    first = client.post(AGENT, content=PRIVATE, headers=headers)
    second = client.post(AGENT, content=PRIVATE, headers=headers)
    assert_failure(first, 413, "request_too_large")
    assert_failure(second, 429, "rate_limited")
    assert first.json()["request_id"] != second.json()["request_id"]
    for response in (first, second):
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert response.json()["request_id"] != "CLIENT_NOT_AUTHORITY"
    setup.agent.run.assert_not_called()
    setup.readiness.check.assert_not_called()


def test_unexpected_security_exception_is_safe_and_cors_covered(
    setup: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = app_for(setup, clock=Mock(side_effect=RuntimeError(PRIVATE)), cors_allowed_origins=ORIGIN)
    response = client_for(app).post(AGENT, json={"text": PRIVATE}, headers={"Origin": ORIGIN})
    assert_failure(response, 500, "internal_failure")
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert PRIVATE not in caplog.text and "Traceback" not in caplog.text
    assert response.json()["request_id"] in caplog.text
    setup.agent.run.assert_not_called()


def test_four_active_fifth_fails_fast_without_queue_and_then_recovers(
    setup: SimpleNamespace,
) -> None:
    app = app_for(setup, cors_allowed_origins=ORIGIN)
    entered, release = Event(), Event()
    lock = Lock()
    calls = 0
    completed = setup.agent.run.return_value
    decision, tool, final = Mock(), Mock(), Mock()

    def blocking_run(*args):
        nonlocal calls
        with lock:
            calls += 1
            decision()
            tool()
            final()
            if calls == 4:
                entered.set()
        assert release.wait(15), "Test failed to release fake Agent"
        return completed

    setup.agent.run.side_effect = blocking_run
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(client_for(app).post, AGENT, json={"text": "x"}) for _ in range(4)]
        try:
            assert entered.wait(10)
            fifth = pool.submit(
                client_for(app).post, AGENT, json={"text": "x"}, headers={"Origin": ORIGIN}
            ).result(timeout=3)
            assert_failure(fifth, 429, "concurrency_limited")
            assert fifth.headers["Access-Control-Allow-Origin"] == ORIGIN
            assert calls == decision.call_count == tool.call_count == final.call_count == 4
        finally:
            release.set()
        assert all(future.result(timeout=5).status_code == 200 for future in futures)
    assert client_for(app).post(AGENT, json={"text": "after"}).status_code == 200
    assert calls == 5


def test_agent_exception_releases_permit(setup: SimpleNamespace) -> None:
    client = client_for(app_for(setup, max_active_agent_runs=1))
    setup.agent.run.side_effect = [RuntimeError(PRIVATE), setup.agent.run.return_value]
    assert_failure(client.post(AGENT, json={"text": "x"}), 500, "internal_failure")
    assert client.post(AGENT, json={"text": "x"}).status_code == 200
    assert setup.agent.run.call_count == 2


@pytest.mark.parametrize("code", list(AgentResponseErrorCode))
def test_application_failed_outcome_releases_permit_without_retry(
    setup: SimpleNamespace, code
) -> None:
    client = client_for(app_for(setup, max_active_agent_runs=1))
    setup.agent.run.return_value = AgentResponse(
        status="failed", answer="", grounded=False, error=AgentResponseError(code, PRIVATE)
    )
    for _ in range(2):
        response = client.post(AGENT, json={"text": "x"})
        assert response.status_code == 200 and response.json()["error"]["code"] == code
    assert setup.agent.run.call_count == 2


def test_not_ready_and_rate_limited_do_not_consume_permits(setup: SimpleNamespace) -> None:
    app = app_for(setup, max_active_agent_runs=1, agent_rate_limit_per_minute=1)
    assert app.state.agent_slots.acquire(blocking=False)
    setup.readiness.check.return_value = ReadinessResult((ReadinessReason.AI_NOT_CONFIGURED,))
    client = client_for(app)
    try:
        assert_failure(client.post(AGENT, json={"text": "x"}), 503, "provider_unavailable")
        assert_failure(client.post(AGENT, json={"text": "x"}), 429, "rate_limited")
        assert not app.state.agent_slots.acquire(blocking=False)
    finally:
        app.state.agent_slots.release()
    assert app.state.agent_slots.acquire(blocking=False)
    app.state.agent_slots.release()
    assert setup.readiness.check.call_count == 1
    setup.agent.run.assert_not_called()


def test_app_instances_isolate_rate_and_concurrency_state(setup: SimpleNamespace) -> None:
    first = app_for(setup, max_active_agent_runs=1, agent_rate_limit_per_minute=1)
    second = app_for(setup, max_active_agent_runs=1, agent_rate_limit_per_minute=1)
    assert client_for(first).post(AGENT, json={"text": "x"}).status_code == 200
    assert first.state.agent_slots.acquire(blocking=False)
    try:
        assert_failure(client_for(first).post(AGENT, json={"text": "x"}), 429, "rate_limited")
        assert client_for(second).post(AGENT, json={"text": "x"}).status_code == 200
    finally:
        first.state.agent_slots.release()
