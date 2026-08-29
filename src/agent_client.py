"""Mode-1 seam: loopback HTTP client for the frozen Hosted /v0.6 API.

The competition UI runs on Mode 2 (deterministic mock) by default. This
module is the explicit adapter for "本机 Agent" mode: it speaks the frozen
public contract (``POST /v0.6/agent/run`` and ``GET /v0.6/sources/{id}``)
against a loopback Hosted service only, with:

- no retries (the runtime AI budget policy forbids unbounded retries; the
  operator sees the failure and can explicitly switch modes);
- no provider/tool/model selection fields — the request body is exactly the
  public ``AgentRunRequest``;
- failures normalized to :class:`~src.demo.contracts.DemoHTTPError` carrying
  the real ``HTTPFailure`` envelope with closed-catalog codes, so one UI
  failure path renders both modes identically;
- no API keys, no external hosts: the base URL must be loopback.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from src.demo.contracts import DemoHTTPError
from src.hosted_api.contracts import (
    AgentRunRequest,
    AgentRunResponse,
    HTTPFailure,
    SourceResponse,
    public_error,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8000"
_REQUEST_TIMEOUT_SECONDS = 8.0

__all__ = [
    "AgentClient",
    "DEFAULT_LOCAL_BASE_URL",
    "HostedAgentClient",
]

_STATUS_FALLBACK_CODES: dict[int, str] = {
    429: "rate_limited",
    503: "runtime_unavailable",
    413: "request_too_large",
}


class AgentClient(Protocol):
    """Transport seam shared by the mock client and the loopback client."""

    def run_agent(self, text: str, correlation_id: str | None = None) -> AgentRunResponse: ...

    def get_source(self, stable_id: str) -> SourceResponse: ...


class HostedAgentClient:
    """Retry-free loopback client over the frozen public API."""

    def __init__(
        self,
        base_url: str = DEFAULT_LOCAL_BASE_URL,
        *,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def run_agent(self, text: str, correlation_id: str | None = None) -> AgentRunResponse:
        """Post one agent run; business and HTTP failures both raise."""

        body = AgentRunRequest(text=text, correlation_id=correlation_id).model_dump_json()
        data = self._request("v0.6/agent/run", body=body)
        return AgentRunResponse.model_validate(data)

    def get_source(self, stable_id: str) -> SourceResponse:
        """Fetch one source's public metadata by stable id."""

        data = self._request(f"v0.6/sources/{quote(stable_id, safe='')}")
        return SourceResponse.model_validate(data)

    def _request(self, path: str, *, body: str | None = None) -> dict[str, Any]:
        url = f"{self._base_url}/{path}"
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise self._http_error(error) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            LOGGER.warning("本机 Agent 服务连接失败：%s %s", url, type(error).__name__)
            raise DemoHTTPError(
                http_status=0,
                failure=HTTPFailure(request_id="", error=public_error("runtime_unavailable")),
            ) from None
        try:
            document = json.loads(payload)
        except ValueError:
            LOGGER.warning("本机 Agent 服务返回了无法解析的响应：%s", url)
            raise DemoHTTPError(
                http_status=0,
                failure=HTTPFailure(request_id="", error=public_error("internal_failure")),
            ) from None
        if not isinstance(document, dict):
            raise DemoHTTPError(
                http_status=0,
                failure=HTTPFailure(request_id="", error=public_error("internal_failure")),
            )
        return document

    @staticmethod
    def _http_error(error: urllib.error.HTTPError) -> DemoHTTPError:
        """Normalize an HTTP failure to the closed-catalog envelope."""

        try:
            document = json.loads(error.read().decode("utf-8"))
        except (ValueError, OSError):
            document = None
        failure: HTTPFailure | None = None
        if isinstance(document, dict) and "error" in document:
            try:
                failure = HTTPFailure.model_validate(document)
            except Exception:
                failure = None
        if failure is None:
            code = _STATUS_FALLBACK_CODES.get(error.code, "internal_failure")
            failure = HTTPFailure(request_id="", error=public_error(code))
        return DemoHTTPError(http_status=error.code, failure=failure)


def _normalize_base_url(base_url: str) -> str:
    """Accept only an explicit loopback http URL; strip the trailing slash."""

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("本机 Agent 地址必须是 http://127.0.0.1:<端口> 形式的回环地址。")
    try:
        host = ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError("本机 Agent 地址主机名无效。") from error
    if not host.is_loopback:
        raise ValueError("本机 Agent 地址必须是 127.0.0.1 回环地址。")
    return base_url.rstrip("/")
