"""Local Agent composition over user-imported, explicitly read documents."""

from __future__ import annotations

from collections.abc import Callable

from src.agent.decision.provider import ModelDecisionProvider
from src.agent.execution.contracts import AgentRequest
from src.agent.execution.executor import SingleStepAgentExecutor
from src.agent.response.contracts import AgentResponse
from src.agent.response.final_answer import FinalAnswerStage
from src.agent.response.pipeline import SingleStepAgentService
from src.agent.tools.bootstrap import build_phase1_handlers, build_phase1_registry
from src.agent_document_reader import (
    AgentDocumentReader,
    AgentReadingStore,
    DocumentReadingReport,
)
from src.ai.provider import CompletionProvider
from src.ai.rag_answer_service import RagAnswerService
from src.database import Database


class LocalDocumentAgent:
    """One local composition for explicit reading and grounded questions.

    Both page understanding and question answering use the configured hard
    model (Qwen 3.8 by default). Page retrieval is restricted to fresh page
    readings, and the Final Answer stage receives complete original page text.
    """

    def __init__(
        self,
        *,
        database: Database,
        provider: CompletionProvider | None,
        readings: AgentReadingStore,
        model: str,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent 模型不能为空")
        self._model = model.strip()
        self._reader = AgentDocumentReader(
            database=database,
            provider=provider,
            store=readings,
            model=self._model,
        )
        registry = build_phase1_registry()
        executor = SingleStepAgentExecutor(
            registry,
            handlers=build_phase1_handlers(
                database,
                page_readings=readings,
                require_agent_read=True,
            ),
        )
        self._decision = ModelDecisionProvider(
            provider,
            registry,
            model=self._model,
            source_feature="agent_decision",
        )
        self._service = SingleStepAgentService(
            executor,
            FinalAnswerStage(
                RagAnswerService(provider),
                model=self._model,
                source_feature="agent_final_answer",
            ),
        )

    @property
    def model(self) -> str:
        return self._model

    def read_document(
        self,
        document_id: int,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> DocumentReadingReport:
        """Run the explicit user-triggered, page-by-page reading action."""

        return self._reader.read_document(
            document_id,
            progress_callback=progress_callback,
        )

    def ask(self, question: str, *, request_id: str | None = None) -> AgentResponse:
        """Answer one question using only fresh pages the Agent has read."""

        request = AgentRequest(request_id=request_id, text=question)
        return self._service.run(request, self._decision)


__all__ = ["LocalDocumentAgent"]
