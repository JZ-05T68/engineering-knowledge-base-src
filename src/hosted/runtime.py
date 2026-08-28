"""Narrow Hosted production composition; never import the Local runtime.

Mixed services stay private behind the frozen read-only adapters. Their class
definitions may contain writes; no write capability is mounted in HTTP or tools.
"""

from __future__ import annotations

from src.agent import (
    AgentRequest,
    FinalAnswerStage,
    ModelDecisionProvider,
    SingleStepAgentService,
    build_single_step_executor,
)
from src.agent.tools import ToolSideEffect
from src.ai.rag_answer_service import RagAnswerService
from src.evidence_basket_service import EvidenceBasketService
from src.hosted.ai_runtime import build_hosted_ai_provider
from src.hosted.application import HostedDependencies
from src.hosted.readiness import HostedReadiness
from src.hosted.storage import HostedStorage
from src.hosted_config import HostedSettings
from src.runtime_profile import RuntimeProfile, require_runtime_profile
from src.source_metadata import SourceMetadataService

_EXPECTED_TOOLS = frozenset({
    "page_search", "knowledge_search", "get_knowledge_object", "get_knowledge_memory",
    "inspect_provenance", "inspect_source_integrity", "get_evidence",
})


def compose_hosted_dependencies(
    settings: HostedSettings, storage: HostedStorage,
) -> HostedDependencies:
    """Use the exact existing executor/registry; return only application capabilities."""
    require_runtime_profile(RuntimeProfile.HOSTED)
    if settings != storage.settings or storage.readiness_reason(storage.database_path) is not None:
        raise ValueError("hosted_storage_invalid")
    database = storage.database
    provider = build_hosted_ai_provider(settings, database)
    executor = build_single_step_executor(database)
    # Read the existing private registry at this composition boundary only; do
    # not alter the frozen executor API or construct a parallel tool catalog.
    registry = executor._registry
    definitions = registry.list_definitions()
    if (
        len(definitions) != 7
        or {item.name for item in definitions} != _EXPECTED_TOOLS
        or any(item.side_effect is not ToolSideEffect.READ_ONLY for item in definitions)
        or set(executor._handlers) != _EXPECTED_TOOLS
    ):
        raise ValueError("hosted_tool_registry_invalid")
    return HostedDependencies(
        readiness=HostedReadiness(settings, storage.readiness_reason),
        agent_service=SingleStepAgentService(
            executor, FinalAnswerStage(RagAnswerService(provider), model=settings.ai_llm_model),
        ),
        decision_provider=ModelDecisionProvider(provider, registry, model=settings.ai_llm_model),
        request_factory=AgentRequest,
        sources=SourceMetadataService(
            kb_uuid=storage.kb_uuid,
            read_page=database.get_page,
            read_document=database.get_document,
            read_knowledge_object=database.get_knowledge_object,
            read_knowledge_memory=database.get_knowledge_memory_entry,
            read_evidence=EvidenceBasketService(database).get_item,
        ),
    )
