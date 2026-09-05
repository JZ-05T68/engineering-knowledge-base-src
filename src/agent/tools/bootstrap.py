"""Phase 1 read-only Tool Registry bootstrap and handler composition.

This module is the agent-side composition boundary: it builds the frozen Phase 1
ToolRegistry (definitions only) and the callable handler map backed by existing
EKB services. It never opens a database by itself and never touches the
Streamlit runtime; callers pass an existing :class:`Database` instance.

The registry registers seven read-only tools, plus ``page_visual_search``
since v0.7.2 when a vision-capable provider is available:

- ``page_search``
- ``knowledge_search``
- ``get_knowledge_object``
- ``get_knowledge_memory``
- ``inspect_provenance``
- ``inspect_source_integrity``
- ``get_evidence``
- ``page_visual_search`` (optional, v0.7.2)

``rag_answer`` is the Final Answer Stage (ADR-006 decision 7), not a Tool.
"""

from __future__ import annotations

from pathlib import Path
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
from src.agent.tools.adapters.page_visual import (
    PAGE_VISUAL_SEARCH_DEFINITION,
    PageVisualAdapter,
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


def phase1_tool_definitions(*, include_visual: bool = False) -> tuple[ToolDefinition, ...]:
    """Return the frozen Phase 1 read-only Tool definitions.

    v0.7.2 adds ``page_visual_search`` when a vision-capable provider is
    available; AI-less compositions keep the original seven-tool surface.
    """

    definitions = (
        PAGE_SEARCH_DEFINITION,
        KNOWLEDGE_SEARCH_DEFINITION,
        GET_KNOWLEDGE_OBJECT_DEFINITION,
        GET_KNOWLEDGE_MEMORY_DEFINITION,
        INSPECT_PROVENANCE_DEFINITION,
        INSPECT_SOURCE_INTEGRITY_DEFINITION,
        GET_EVIDENCE_DEFINITION,
    )
    if include_visual:
        return definitions + (PAGE_VISUAL_SEARCH_DEFINITION,)
    return definitions


def build_phase1_registry(*, include_visual: bool = False) -> ToolRegistry:
    """Build an empty-backed registry containing the read-only Tool set."""
    registry = ToolRegistry()
    for definition in phase1_tool_definitions(include_visual=include_visual):
        registry.register(definition)
    return registry


def build_phase1_handlers(
    database: Database,
    *,
    page_readings: PageReadingLookup | None = None,
    require_agent_read: bool = False,
    vision_provider: object | None = None,
    vision_model: str | None = None,
    pages_dir: Path | None = None,
) -> dict[str, ToolHandler]:
    """Build the callable handler map for the read-only Tool set.

    Services are constructed from the caller-provided ``database``; no global
    mutable singleton and no Streamlit session is used. The knowledge-base
    UUID is read once for stable-id construction. ``page_visual_search`` is
    only added when ``vision_provider`` exposes a vision completion.
    """
    kb_uuid = database.get_knowledge_base_uuid()
    handlers = {
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
    if vision_provider is not None and pages_dir is not None:
        handlers["page_visual_search"] = PageVisualAdapter(
            SearchService(database),
            kb_uuid=kb_uuid,
            vision_provider=vision_provider,
            pages_dir=pages_dir,
            vision_model=vision_model,
        )
    return handlers


__all__ = [
    "build_phase1_handlers",
    "build_phase1_registry",
    "phase1_tool_definitions",
]
