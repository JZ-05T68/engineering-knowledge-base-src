"""Phase 1 read-only Tool Registry bootstrap and handler composition.

This module is the agent-side composition boundary: it builds the frozen Phase 1
ToolRegistry (definitions only) and the callable handler map backed by existing
EKB services. It never opens a database by itself and never touches the
Streamlit runtime; callers pass an existing :class:`Database` instance.

The Phase 1 registry deliberately registers exactly seven read-only tools:

- ``page_search``
- ``knowledge_search``
- ``get_knowledge_object``
- ``get_knowledge_memory``
- ``inspect_provenance``
- ``inspect_source_integrity``
- ``get_evidence``

``rag_answer`` is the Final Answer Stage (ADR-006 decision 7), not a Tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.agent.tools.adapters import (
    GET_EVIDENCE_DEFINITION,
    GET_KNOWLEDGE_MEMORY_DEFINITION,
    GET_KNOWLEDGE_OBJECT_DEFINITION,
    INSPECT_PROVENANCE_DEFINITION,
    INSPECT_SOURCE_INTEGRITY_DEFINITION,
    KNOWLEDGE_SEARCH_DEFINITION,
    PAGE_SEARCH_DEFINITION,
    GetEvidenceAdapter,
    InspectProvenanceAdapter,
    InspectSourceIntegrityAdapter,
    KnowledgeMemoryAdapter,
    KnowledgeObjectAdapter,
    KnowledgeSearchAdapter,
    PageSearchAdapter,
)
from src.agent.tools.contracts import ToolDefinition, ToolHandler
from src.agent.tools.registry import ToolRegistry
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_context import ContextItemProjector
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.knowledge_search_service import KnowledgeSearchService
from src.search_service import SearchService

if TYPE_CHECKING:
    from src.agent.tools.adapters.page_search import PageReadingLookup


def phase1_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Return the seven frozen Phase 1 read-only Tool definitions."""
    return (
        PAGE_SEARCH_DEFINITION,
        KNOWLEDGE_SEARCH_DEFINITION,
        GET_KNOWLEDGE_OBJECT_DEFINITION,
        GET_KNOWLEDGE_MEMORY_DEFINITION,
        INSPECT_PROVENANCE_DEFINITION,
        INSPECT_SOURCE_INTEGRITY_DEFINITION,
        GET_EVIDENCE_DEFINITION,
    )


def build_phase1_registry() -> ToolRegistry:
    """Build an empty-backed registry containing only the seven Phase 1 tools."""
    registry = ToolRegistry()
    for definition in phase1_tool_definitions():
        registry.register(definition)
    return registry


def build_phase1_handlers(
    database: Database,
    *,
    page_readings: PageReadingLookup | None = None,
    require_agent_read: bool = False,
) -> dict[str, ToolHandler]:
    """Build the callable handler map for the seven Phase 1 tools.

    Services are constructed from the caller-provided ``database``; no global
    mutable singleton and no Streamlit session are used. The knowledge-base
    UUID is read once for stable-id construction.
    """
    kb_uuid = database.get_knowledge_base_uuid()
    return {
        "page_search": PageSearchAdapter(
            SearchService(database),
            kb_uuid=kb_uuid,
            page_readings=page_readings,
            require_agent_read=require_agent_read,
        ),
        "knowledge_search": KnowledgeSearchAdapter(
            KnowledgeSearchService(database)
        ),
        "get_knowledge_object": KnowledgeObjectAdapter(
            KnowledgeObjectService(database), kb_uuid=kb_uuid
        ),
        "get_knowledge_memory": KnowledgeMemoryAdapter(
            KnowledgeMemoryService(database), kb_uuid=kb_uuid
        ),
        "inspect_provenance": InspectProvenanceAdapter(
            ContextItemProjector(database), kb_uuid=kb_uuid
        ),
        "inspect_source_integrity": InspectSourceIntegrityAdapter(
            KnowledgeObjectService(database), kb_uuid=kb_uuid
        ),
        "get_evidence": GetEvidenceAdapter(
            EvidenceBasketService(database), kb_uuid=kb_uuid
        ),
    }


__all__ = [
    "build_phase1_handlers",
    "build_phase1_registry",
    "phase1_tool_definitions",
]
