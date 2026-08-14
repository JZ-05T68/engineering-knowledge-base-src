"""Tests for the stdlib urllib transport. All HTTP is faked; no network."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from src.ai.provider import AIExecutionError
from src.ai.qwen_client import QwenTransportError, urllib_transport

SECRET = "sk-transport-test-never-real"
URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
PAYLOAD = {"model": "qwen3.7-plus", "messages": []}


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> Any:
        captured.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": request.data,
                "timeout": timeout,
            }
        )
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def test_transport_sends_json_post_with_headers_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_urlopen(monkeypatch, _FakeResponse(b'{"ok": true}'))

    result = urllib_transport(URL, HEADERS, PAYLOAD, 7.5)

    assert result == {"ok": True}
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == URL
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["headers"]["Content-type"] == "application/json"
    assert json.loads(call["body"].decode("utf-8")) == PAYLOAD
    assert call["timeout"] == 7.5


def test_http_error_maps_to_status_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(URL, 401, "Unauthorized", None, None)
    _patch_urlopen(monkeypatch, error)

    with pytest.raises(QwenTransportError) as captured_exc:
        urllib_transport(URL, HEADERS, PAYLOAD, 7.5)

    assert captured_exc.value.status_code == 401
    assert SECRET not in str(captured_exc.value)


def test_http_429_and_5xx_keep_status_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    for status in (429, 500, 503):
        error = urllib.error.HTTPError(URL, status, "err", None, None)
        _patch_urlopen(monkeypatch, error)
        with pytest.raises(QwenTransportError) as captured_exc:
            urllib_transport(URL, HEADERS, PAYLOAD, 7.5)
        assert captured_exc.value.status_code == status


def test_network_failure_maps_to_statusless_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError("connection refused"))

    with pytest.raises(QwenTransportError) as captured_exc:
        urllib_transport(URL, HEADERS, PAYLOAD, 7.5)

    assert captured_exc.value.status_code is None
    assert SECRET not in str(captured_exc.value)


def test_timeout_maps_to_statusless_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, TimeoutError("timed out"))

    with pytest.raises(QwenTransportError) as captured_exc:
        urllib_transport(URL, HEADERS, PAYLOAD, 7.5)

    assert captured_exc.value.status_code is None


def test_non_json_body_is_execution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _FakeResponse(b"<html>oops</html>"))

    with pytest.raises(AIExecutionError, match="JSON"):
        urllib_transport(URL, HEADERS, PAYLOAD, 7.5)


def test_non_object_json_body_is_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, _FakeResponse(b"[1, 2, 3]"))

    with pytest.raises(AIExecutionError, match="JSON 对象"):
        urllib_transport(URL, HEADERS, PAYLOAD, 7.5)
