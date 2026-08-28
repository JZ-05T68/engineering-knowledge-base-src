"""Demo-layer contracts (v0.6.1): validated supersets of the frozen public DTOs.

The competition demo (Mode 2, deterministic mock) must never invent a second
response schema. Every demo response therefore extends the real public Hosted
DTOs (``src.hosted_api.contracts``) and stays constructible through them, so a
frontend can render Mode 1 (real HTTP) and Mode 2 (mock) with one renderer and
treat the demo-only fields as optional enrichment.

Demo-only enrichment is clearly marked and carries no authority:

- ``DemoAgentRunResponse.mode`` is always ``"mock_demo"`` so a UI can never
  mistake a fixture for a real model answer;
- ``citations_detail`` provides the ``#N`` display mapping and anchor labels
  that the real HTTP contract deliberately does not expose; a UI must fall
  back to the plain ``citations`` list when the field is absent (Mode 1);
- ``DemoSourceResponse.integrity_state`` / ``demo_note`` are preset demo
  states, never live verification results.

Error shapes reuse ``HTTPFailure`` and the closed ``public_error`` catalog so
demo failures render exactly like real HTTP failures.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, ValidationError

from src.agent.execution.contracts import MAX_AGENT_REQUEST_CHARS
from src.hosted_api.contracts import (
    AgentRunRequest,
    AgentRunResponse,
    HTTPFailure,
    PublicDTO,
    SourceResponse,
    public_error,
)
from src.models import ContextFingerprintState, ContextItemType

DEMO_VERSION = "0.6.1"
DEMO_MODE = "mock_demo"

__all__ = [
    "DEMO_MODE",
    "DEMO_VERSION",
    "DemoAgentRunResponse",
    "DemoCitation",
    "DemoHTTPError",
    "DemoRequestError",
    "DemoSourceError",
    "DemoSourceResponse",
    "demo_run_request_id",
    "demo_source_request_id",
    "validate_correlation_id",
]


class DemoCitation(PublicDTO):
    """One rendered citation line: the ``#N`` mapping the real API omits."""

    display_index: StrictInt = Field(ge=1)
    stable_id: StrictStr
    anchor_label: StrictStr
    source_type: ContextItemType
    title: StrictStr | None
    label: StrictStr | None


class DemoAgentRunResponse(AgentRunResponse):
    """Public ``AgentRunResponse`` plus explicit mock-demo enrichment only."""

    mode: Literal["mock_demo"] = DEMO_MODE
    citations_detail: tuple[DemoCitation, ...] = ()


class DemoSourceResponse(SourceResponse):
    """Public ``SourceResponse`` plus optional preset demo viewer state."""

    integrity_state: ContextFingerprintState | None = None
    demo_note: StrictStr | None = None


class DemoHTTPError(Exception):
    """Demo failure carrying the real ``HTTPFailure`` envelope."""

    def __init__(self, *, http_status: int, failure: HTTPFailure) -> None:
        super().__init__(f"demo error http_status={http_status} code={failure.error.code}")
        self.http_status = http_status
        self.failure = failure


class DemoRequestError(DemoHTTPError):
    """Mirrors the real 422/413 envelope for invalid demo agent requests."""


class DemoSourceError(DemoHTTPError):
    """Mirrors the real 404/422 envelope for demo source lookups."""


def demo_run_request_id(normalized_text: str) -> str:
    """Deterministic request id; real HTTP uses one uuid4 per request."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-run:{normalized_text}"))


def demo_source_request_id(stable_id: str) -> str:
    """Deterministic request id for one demo source lookup."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-source:{stable_id}"))


def validate_correlation_id(correlation_id: str | None) -> None:
    """Reuse the real ``AgentRunRequest`` field validation, fail closed."""

    try:
        AgentRunRequest(text="", correlation_id=correlation_id)
    except ValidationError:
        raise DemoRequestError(
            http_status=422,
            failure=HTTPFailure(
                request_id=demo_run_request_id("invalid-correlation-id"),
                error=public_error("invalid_request"),
            ),
        ) from None


def max_request_chars() -> int:
    """The frozen ``AgentRequest`` character guard, mirrored by the mock."""

    return MAX_AGENT_REQUEST_CHARS
