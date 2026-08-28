"""Competition demo layer (v0.6.1): deterministic Mode-2 mock fixtures.

This package is a read-only demo seam over the frozen public Hosted DTOs.
It performs no network I/O, no AI calls, no database access and no
environment reads. It never imports the Local runtime, the Hosted
composition root, the database layer or any AI client, so demo mode can
never reach user production data or paid services.
"""

from __future__ import annotations

from src.demo.catalog import DemoCatalog, load_demo_catalog
from src.demo.contracts import (
    DEMO_MODE,
    DEMO_VERSION,
    DemoAgentRunResponse,
    DemoCitation,
    DemoHTTPError,
    DemoRequestError,
    DemoSourceError,
    DemoSourceResponse,
)
from src.demo.fixtures import (
    DEMO_KB_UUID,
    FIXTURE_KEYS,
    GENERIC_WARNING,
    NO_EVIDENCE_MESSAGE,
    RESPONSES,
    SOURCES,
)
from src.demo.mock_agent import MockDemoClient
from src.demo.presets import PRESETS, DemoPreset

__all__ = [
    "DEMO_KB_UUID",
    "DEMO_MODE",
    "DEMO_VERSION",
    "FIXTURE_KEYS",
    "GENERIC_WARNING",
    "NO_EVIDENCE_MESSAGE",
    "PRESETS",
    "RESPONSES",
    "SOURCES",
    "DemoAgentRunResponse",
    "DemoCatalog",
    "DemoCitation",
    "DemoHTTPError",
    "DemoPreset",
    "DemoRequestError",
    "DemoSourceError",
    "DemoSourceResponse",
    "MockDemoClient",
    "load_demo_catalog",
]
