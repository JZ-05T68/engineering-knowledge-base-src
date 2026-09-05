"""In-process client for the local document Agent used by Streamlit pages."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from src.agent.local_document import LocalDocumentAgent
from src.agent_document_reader import AgentReadingStore, DocumentReadingReport
from src.ai.provider import CompletionProvider
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.hosted_api.contracts import (
    AgentRunResponse,
    SourceResponse,
    project_agent_response,
    project_source,
)
from src.source_metadata import SourceMetadataService


class LocalDocumentAgentClient:
    """Adapt the local Agent to the existing safe UI response contract."""

    base_url = "local://document-agent"

    def __init__(
        self,
        *,
        database: Database,
        provider: CompletionProvider | None,
        readings: AgentReadingStore,
        model: str,
        vision_provider: object | None = None,
        vision_model: str | None = None,
        pages_dir: Path | None = None,
    ) -> None:
        self._agent = LocalDocumentAgent(
            database=database,
            provider=provider,
            readings=readings,
            model=model,
            vision_provider=vision_provider,
            vision_model=vision_model,
            pages_dir=pages_dir,
        )
        self._sources = SourceMetadataService(
            kb_uuid=database.get_knowledge_base_uuid(),
            read_page=database.get_page,
            read_document=database.get_document,
            read_knowledge_object=database.get_knowledge_object,
            read_knowledge_memory=database.get_knowledge_memory_entry,
            read_evidence=EvidenceBasketService(database).get_item,
        )

    def read_document(self, document_id: int, **kwargs: object) -> DocumentReadingReport:
        """Run the explicit page-by-page reading action."""

        return self._agent.read_document(document_id, **kwargs)  # type: ignore[arg-type]

    def run_agent(
        self, text: str, correlation_id: str | None = None
    ) -> AgentRunResponse:
        """Answer from fresh, explicitly read local pages."""

        request_id = correlation_id or f"local-{uuid4().hex}"
        response = self._agent.ask(text, request_id=request_id)
        return project_agent_response(response, request_id)

    def get_source(self, stable_id: str) -> SourceResponse:
        """Return one citation-safe local source label."""

        source = self._sources.get(stable_id)
        if source is None:
            raise LookupError("找不到引用来源。")
        return project_source(source, stable_id)


__all__ = ["LocalDocumentAgentClient"]
