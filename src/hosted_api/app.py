"""Four thin synchronous routes over explicitly injected Hosted dependencies.

No global app, Local runtime, service bootstrap, DB construction or provider.
Production storage/provider composition belongs to WP4; all routes are testable
in process without binding a listening socket.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from src.hosted.application import HostedDependencies
from src.hosted.readiness import ReadinessReason
from src.hosted_api.contracts import (
    AgentRunRequest,
    AgentRunResponse,
    HealthResponse,
    HTTPFailure,
    ReadyResponse,
    SourceResponse,
    project_agent_response,
    project_readiness,
    project_source,
    public_error,
)
from src.hosted_config import HostedSettings
from src.models import ContextItemType
from src.runtime_profile import (
    RuntimeConfigurationError,
    RuntimeConfigurationErrorCode,
    RuntimeProfile,
    require_runtime_profile,
)
from src.source_metadata import InvalidSourceId, parse_source_id

LOGGER = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    if not hasattr(request.state, "request_id"):
        request.state.request_id = str(uuid4())
    return request.state.request_id


def _failure(request: Request, status: int, code: str) -> JSONResponse:
    payload = HTTPFailure(request_id=_request_id(request), error=public_error(code))
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))


class _SafeRoute(APIRoute):
    """Catch adapter failures before ASGI re-raises them into raw server logs."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original(request)
            except (RequestValidationError, HTTPException):
                raise
            except Exception:
                # Fixed category only: even exception class names can be supplied
                # by an injected dependency. Never log request/exception repr.
                LOGGER.error(
                    "Hosted request failed: request_id=%s code=internal_failure",
                    _request_id(request),
                )
                return _failure(request, 500, "internal_failure")

        return safe_handler


def create_hosted_app(*, settings: HostedSettings, dependencies: HostedDependencies) -> FastAPI:
    """Build transport only, requiring exact process Hosted opt-in even in tests.

    Configuration is supplied by the trusted caller (normally load_hosted_settings).
    Unusable storage/missing AI remain readiness failures, not liveness failures.
    This factory never constructs or invokes application dependencies at import
    or construction time. No unrestricted Local service can be used as fallback.
    """

    require_runtime_profile(RuntimeProfile.HOSTED)
    if (
        not isinstance(settings, HostedSettings)
        or settings.runtime_profile is not RuntimeProfile.HOSTED
    ):
        raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH)
    app = FastAPI(title="EKB Hosted Agent API", version="0.6.0", debug=False)
    app.router.route_class = _SafeRoute

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not serialize exc.errors(): Pydantic includes rejected input/prompt.
        return _failure(request, 422, "invalid_request")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return _failure(request, exc.status_code, code)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/ready", response_model=ReadyResponse, responses={503: {"model": ReadyResponse}})
    def ready(response: Response) -> ReadyResponse:
        result = dependencies.check_readiness()
        response.status_code = 200 if result.ready else 503
        return project_readiness(result)

    @app.post(
        "/v0.6/agent/run",
        response_model=AgentRunResponse,
        responses={
            422: {"model": HTTPFailure},
            503: {"model": HTTPFailure},
            500: {"model": HTTPFailure},
        },
    )
    def run_agent(
        payload: AgentRunRequest, request: Request, response: Response
    ) -> AgentRunResponse | JSONResponse:
        request_id = _request_id(request)
        if dependencies.request_factory is None:
            return _failure(request, 503, "runtime_unavailable")
        try:
            agent_request = dependencies.request_factory(request_id=request_id, text=payload.text)
        except (ValueError, TypeError):
            return _failure(request, 422, "invalid_request")
        readiness = dependencies.check_readiness()
        if not readiness.ready:
            code = (
                "provider_unavailable"
                if ReadinessReason.AI_NOT_CONFIGURED in readiness.reasons
                else "runtime_unavailable"
            )
            return _failure(request, 503, code)
        # correlation_id is transport metadata only. It never enters AgentRequest.
        if payload.correlation_id is not None:
            response.headers["X-Correlation-ID"] = payload.correlation_id
        result = dependencies.agent_service.run(agent_request, dependencies.decision_provider)
        return project_agent_response(result, request_id)

    @app.get(
        "/v0.6/sources/{stable_id}",
        response_model=SourceResponse,
        responses={
            422: {"model": HTTPFailure},
            404: {"model": HTTPFailure},
            503: {"model": HTTPFailure},
            500: {"model": HTTPFailure},
        },
    )
    def source(stable_id: str, request: Request) -> SourceResponse | JSONResponse:
        try:
            _, kind, _ = parse_source_id(stable_id)
        except InvalidSourceId:
            return _failure(request, 422, "invalid_source_id")
        if kind not in {item.value for item in ContextItemType}:
            return _failure(request, 404, "not_found")
        if dependencies.sources is None:
            return _failure(request, 503, "runtime_unavailable")
        result = dependencies.sources.get(stable_id)
        if result is None:
            return _failure(request, 404, "not_found")
        return project_source(result, stable_id)

    return app
