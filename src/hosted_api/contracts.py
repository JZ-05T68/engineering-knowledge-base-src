"""Explicit public DTOs; internal Agent objects are never serialized wholesale."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from src.hosted.readiness import ReadinessReason, ReadinessResult
from src.models import ContextItemType
from src.source_metadata import SourceMetadata, parse_source_id, safe_display_text

if TYPE_CHECKING:
    from src.agent.response.contracts import AgentResponse

CorrelationId = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
]


class PublicDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AgentRunRequest(PublicDTO):
    # AgentRequest remains the authoritative 120k-character guard. No trimming,
    # extra minimum length or second application-limit constant is introduced.
    text: StrictStr
    correlation_id: CorrelationId | None = None


class PublicError(PublicDTO):
    code: Literal[
        "invalid_request",
        "invalid_source_id",
        "not_found",
        "method_not_allowed",
        "runtime_unavailable",
        "provider_unavailable",
        "tool_failed",
        "final_answer_failed",
        "citation_invalid",
        "budget_exceeded",
        "internal_failure",
    ]
    message: StrictStr


_ERROR_MESSAGES = {
    "invalid_request": "请求格式无效或超过允许的文本长度。",
    "invalid_source_id": "来源标识格式无效。",
    "not_found": "未找到请求的资源。",
    "method_not_allowed": "不支持此请求方法。",
    "runtime_unavailable": "服务尚未就绪。",
    "provider_unavailable": "AI 服务尚未配置。",
    "tool_failed": "知识库工具执行失败。",
    "final_answer_failed": "答案生成失败。",
    "citation_invalid": "答案引用校验失败。",
    "budget_exceeded": "AI 调用被预算限制拒绝。",
    "internal_failure": "服务处理请求失败。",
}


def public_error(code: str) -> PublicError:
    """Use a closed message catalog, never dependency-provided error text."""
    if code not in _ERROR_MESSAGES:
        code = "internal_failure"
    return PublicError(code=code, message=_ERROR_MESSAGES[code])


class HTTPFailure(PublicDTO):
    request_id: StrictStr
    status: Literal["failed"] = "failed"
    error: PublicError


class AgentRunResponse(PublicDTO):
    request_id: StrictStr
    status: Literal["completed", "failed"]
    answer: StrictStr
    grounded: StrictBool
    citations: tuple[StrictStr, ...]
    warnings: tuple[StrictStr, ...]
    error: PublicError | None


def project_agent_response(response: AgentResponse, request_id: str) -> AgentRunResponse:
    """Only approved fields; raw warning/exception text is not public metadata."""
    for citation in response.citations:
        parse_source_id(citation)
    return AgentRunResponse(
        request_id=request_id,
        status=response.status,
        answer=response.answer,
        grounded=response.grounded,
        citations=tuple(response.citations),
        warnings=("来源存在限制，请核对引用资料。",) if response.warnings else (),
        error=public_error(response.error.code) if response.error is not None else None,
    )


class SourceResponse(PublicDTO):
    stable_id: StrictStr
    type: ContextItemType
    title: StrictStr | None
    label: StrictStr | None


def project_source(source: SourceMetadata, requested_id: str) -> SourceResponse:
    """Enforce identity and display-only fields even for an injected reader."""
    _, kind, _ = parse_source_id(source.stable_id)
    if source.stable_id != requested_id or kind != source.type:
        raise ValueError("Source projection identity mismatch")
    return SourceResponse(
        stable_id=source.stable_id,
        type=source.type,
        title=safe_display_text(source.title),
        label=safe_display_text(source.label),
    )


class HealthResponse(PublicDTO):
    status: Literal["ok"] = "ok"


class ReadyResponse(PublicDTO):
    ready: StrictBool
    status: Literal["ready", "not_ready"]
    reasons: tuple[ReadinessReason, ...]


def project_readiness(result: ReadinessResult) -> ReadyResponse:
    return ReadyResponse(
        ready=result.ready,
        status="ready" if result.ready else "not_ready",
        reasons=result.reasons,
    )
