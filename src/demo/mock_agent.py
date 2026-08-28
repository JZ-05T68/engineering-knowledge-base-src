"""Deterministic Mode-2 mock demo client (v0.6.1).

``MockDemoClient`` gives the competition frontend one offline seam with the
same call/response semantics as the real Hosted HTTP API:

- ``run_agent(text, correlation_id)`` mirrors ``POST /v0.6/agent/run``:
  request guards (character limit, correlation-id validation) reuse the real
  frozen limits, preset questions resolve deterministically, everything else
  falls back to the honest no-evidence fixture;
- ``get_source(stable_id)`` mirrors ``GET /v0.6/sources/{stable_id}``:
  canonical identity parsing, closed error catalog, metadata-only payloads;
- ``list_presets()`` exposes the frozen question cards for the composer UI.

Hard boundaries: no network, no AI provider, no database, no environment
reads, no randomness beyond ``uuid5`` derivation, and never a fabricated
success: unknown questions return the real no-evidence message with
``grounded=false``.
"""

from __future__ import annotations

from src.demo.catalog import DemoCatalog, load_demo_catalog
from src.demo.contracts import (
    DemoAgentRunResponse,
    DemoRequestError,
    DemoSourceError,
    DemoSourceResponse,
    demo_run_request_id,
    demo_source_request_id,
    max_request_chars,
    validate_correlation_id,
)
from src.demo.presets import DemoPreset
from src.hosted_api.contracts import HTTPFailure, public_error
from src.models import ContextItemType
from src.source_metadata import InvalidSourceId, parse_source_id

_EMPTY_FIXTURE_KEY = "empty_result"


class MockDemoClient:
    """Serve the validated demo catalog with real-API-shaped responses."""

    def __init__(self, catalog: DemoCatalog | None = None) -> None:
        self._catalog = catalog if catalog is not None else load_demo_catalog()

    def list_presets(self) -> tuple[DemoPreset, ...]:
        """Return the frozen demo question cards (composer presets)."""
        return self._catalog.presets

    def run_agent(
        self, text: str, correlation_id: str | None = None
    ) -> DemoAgentRunResponse:
        """Return the deterministic demo answer for ``text``.

        Mirrors the real request guards: non-string or over-limit text and
        invalid correlation ids raise :class:`DemoRequestError` carrying the
        real ``HTTPFailure`` envelope (422, ``invalid_request``). Unknown
        questions return the honest no-evidence response.
        """
        if not isinstance(text, str):
            raise self._request_error()
        validate_correlation_id(correlation_id)
        normalized = text.strip()
        request_id = demo_run_request_id(normalized)
        if len(normalized) > max_request_chars():
            raise self._request_error(request_id)
        preset = self._catalog.preset_for_question(normalized)
        fixture_key = preset.expected_fixture if preset is not None else _EMPTY_FIXTURE_KEY
        template = self._catalog.response(fixture_key).response
        return template.model_copy(update={"request_id": request_id})

    def get_source(self, stable_id: str) -> DemoSourceResponse:
        """Return demo source metadata, mirroring the real source route.

        Malformed ids raise 422 ``invalid_source_id`` without echoing input;
        well-formed but unknown ids (including types the real route does not
        serve, such as ``knowledge_source``) raise 404 ``not_found``.
        """
        if not isinstance(stable_id, str):
            raise DemoSourceError(
                http_status=422,
                failure=HTTPFailure(
                    request_id=demo_source_request_id("invalid"),
                    error=public_error("invalid_source_id"),
                ),
            )
        request_id = demo_source_request_id(stable_id)
        try:
            _, kind, _ = parse_source_id(stable_id)
        except InvalidSourceId:
            raise DemoSourceError(
                http_status=422,
                failure=HTTPFailure(
                    request_id=request_id, error=public_error("invalid_source_id")
                ),
            ) from None
        if kind not in {item.value for item in ContextItemType}:
            raise DemoSourceError(
                http_status=404,
                failure=HTTPFailure(
                    request_id=request_id, error=public_error("not_found")
                ),
            )
        source = self._catalog.source(stable_id)
        if source is None:
            raise DemoSourceError(
                http_status=404,
                failure=HTTPFailure(
                    request_id=request_id, error=public_error("not_found")
                ),
            )
        return source.to_public()

    @staticmethod
    def _request_error(request_id: str | None = None) -> DemoRequestError:
        return DemoRequestError(
            http_status=422,
            failure=HTTPFailure(
                request_id=request_id or demo_run_request_id("invalid-request"),
                error=public_error("invalid_request"),
            ),
        )
