"""One safe failure envelope shared by routes and pre-route ASGI boundaries."""

from __future__ import annotations

from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import Scope

from src.hosted_api.contracts import HTTPFailure, public_error


def request_id(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    if "request_id" not in state:
        state["request_id"] = str(uuid4())
    return state["request_id"]


def failure_response(
    scope: Scope,
    status: int,
    code: str,
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    payload = HTTPFailure(request_id=request_id(scope), error=public_error(code))
    headers = {"Retry-After": str(max(1, retry_after))} if retry_after is not None else None
    return JSONResponse(
        status_code=status, content=payload.model_dump(mode="json"), headers=headers
    )
