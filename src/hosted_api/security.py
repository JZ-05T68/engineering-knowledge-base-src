"""Pure ASGI ingress admission, outside DTO parsing and inside standard CORS.

CORS is a browser exposure policy, never authentication. No request, header,
client IP, body, secret or exception detail is written to security logs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.hosted.security import IPNetwork, RateCategory, RateLimiter, effective_client_ip
from src.hosted_api.errors import failure_response, request_id

MAX_HTTP_BODY_BYTES = 512_000
LOGGER = logging.getLogger(__name__)


class RequestIdentityMiddleware:
    """The outer user middleware assigns server identity before any rejection."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = str(uuid4())
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            LOGGER.error(
                "Hosted boundary failed: request_id=%s code=internal_failure", request_id(scope)
            )
            if not started:
                await failure_response(scope, 500, "internal_failure")(scope, receive, send)


def _category(scope: Scope) -> RateCategory | None:
    path, method = scope["path"], scope["method"]
    # Match the router's server-controlled mount/root-path semantics. Using the
    # raw prefixed path would exempt otherwise valid mounted Agent/source routes.
    root = scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    if method == "POST" and path.rstrip("/") == "/v0.6/agent/run":
        return "agent"
    if method == "GET" and path.startswith("/v0.6/sources/"):
        return "source"
    return None


def _declared_oversize(headers: Sequence[tuple[bytes, bytes]]) -> bool:
    for name, value in headers:
        if name.lower() == b"content-length":
            for part in value.split(b","):
                digits = part.strip()
                if digits.isdigit():
                    digits = digits.lstrip(b"0")
                    # Do not convert an attacker-supplied unbounded integer.
                    if len(digits) > 6 or (len(digits) == 6 and digits > b"512000"):
                        return True
    return False


class IngressSecurityMiddleware:
    """Resolve peer -> rate admission -> bounded actual-body admission.

    The one pre-parser buffer never contains more than 512000 bytes. Each chunk
    is checked BEFORE append; overflow stops receiving and never invokes the
    downstream app. Accepted bytes are handed off once, not replayed via
    BaseHTTPMiddleware/Request.body(), and the mutable buffer is released first.
    This also checks bodies on routes that would otherwise never consume them.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        trusted_proxies: Sequence[IPNetwork],
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.trusted_proxies = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._admit(scope, receive, tracked_send)
        except Exception:
            LOGGER.error(
                "Hosted ingress failed: request_id=%s code=internal_failure", request_id(scope)
            )
            if not started:
                await failure_response(scope, 500, "internal_failure")(scope, receive, send)

    async def _admit(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = scope.get("headers", [])
        peer = scope.get("client")
        forwarded = [value for name, value in headers if name.lower() == b"x-forwarded-for"]
        client = effective_client_ip(peer[0] if peer else None, forwarded, self.trusted_proxies)
        category = _category(scope)
        if category is not None:
            retry_after = self.limiter.admit(category, client)
            if retry_after is not None:
                await failure_response(scope, 429, "rate_limited", retry_after=retry_after)(
                    scope,
                    receive,
                    send,
                )
                return
        if _declared_oversize(headers):
            await failure_response(scope, 413, "request_too_large")(scope, receive, send)
            return

        buffer = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                raise ValueError("Unexpected ASGI request message")
            chunk = message.get("body", b"")
            if len(buffer) + len(chunk) > MAX_HTTP_BODY_BYTES:
                await failure_response(scope, 413, "request_too_large")(scope, receive, send)
                return
            buffer.extend(chunk)
            if not message.get("more_body", False):
                break
        payload = bytes(buffer)
        del buffer
        delivered = False

        async def admitted_receive() -> Message:
            nonlocal delivered, payload
            if delivered:
                return await receive()
            delivered = True
            body, payload = payload, b""
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, admitted_receive, send)
