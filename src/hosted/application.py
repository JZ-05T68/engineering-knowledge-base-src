"""Explicit application dependencies for Hosted transport (no HTTP imports).

The composition root supplies frozen Agent objects, including AgentRequest as
request_factory. Importing src.agent currently eagerly imports its mixed-service
bootstrap: type-only imports here preserve the Hosted import boundary without
altering the frozen Agent/Tool packages. This is not a production bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from src.hosted.readiness import ReadinessChecker, ReadinessReason, ReadinessResult
from src.source_metadata import SourceMetadata

if TYPE_CHECKING:
    from src.agent.execution.contracts import AgentRequest, DecisionProvider
    from src.agent.response.pipeline import SingleStepAgentService


class SourceMetadataReader(Protocol):
    def get(self, stable_id: str) -> SourceMetadata | None:
        """Return display metadata only, or no matching source."""
        ...


@dataclass(frozen=True, slots=True)
class HostedDependencies:
    """Trusted server injection; never constructed from client request fields.

    Pass the existing AgentRequest class as request_factory, never a DTO or a
    replacement application contract. Missing composition keeps health alive
    but fails readiness and Agent admission. No local runtime fallback exists.
    """

    readiness: ReadinessChecker
    agent_service: SingleStepAgentService | None = None
    decision_provider: DecisionProvider | None = None
    request_factory: type[AgentRequest] | None = None
    sources: SourceMetadataReader | None = None

    def check_readiness(self) -> ReadinessResult:
        result = self.readiness.check()
        if not isinstance(result, ReadinessResult):
            raise TypeError("Readiness checker returned an invalid result")
        if any(
            value is None
            for value in (
                self.agent_service,
                self.decision_provider,
                self.request_factory,
                self.sources,
            )
        ):
            return ReadinessResult((*result.reasons, ReadinessReason.COMPOSITION_UNAVAILABLE))
        return result
