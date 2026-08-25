"""Audited RAG answer service (v0.5.3 Phase 3).

``RagAnswerService`` turns a ``KnowledgeContextPackage`` plus a user question
into one fully traceable ``AuditedAIOutput`` through a completion provider.

Boundaries (frozen for v0.5.x):

- the provider never accesses the database — it only receives the prompt;
- the provider input is always a ``KnowledgeContextPackage``, never raw
  search-result text;
- every output carries its used context ids, citations, exclusions and
  warnings, so the chain can always answer "what knowledge was this based on";
- provider failures propagate as typed AI errors; the service never swallows
  them into a fake answer;
- no knowledge is written, summarised into memory, or modified.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from src.ai.provider import (
    AuditedAIProvider,
    CompletionProvider,
    require_ai_provider,
)
from src.ai.rag_prompt_builder import RagPromptBuilder
from src.knowledge_context_packager import KnowledgeContextPackage
from src.models import AuditedAIOutput

_STABLE_ID_TOKEN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:[a-z_]+:[0-9]+"
)
_CITATION_NUMBER_TOKEN = re.compile(r"#([0-9]{1,3})")

__all__ = [
    "MockCompletionProvider",
    "RagAnswerError",
    "RagAnswerService",
]


class RagAnswerError(ValueError):
    """Raised when the audited answer chain cannot run safely."""


class MockCompletionProvider:
    """Deterministic offline provider for the first-stage implementation.

    It performs no network I/O, reads no API key and returns a clearly
    labelled mock answer. It exists so the audited chain can be exercised and
    tested without any credential or paid call.
    """

    is_configured = False

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ):
        from src.ai.provider import CompletionResult

        return CompletionResult(
            text=(
                "（离线演示回答，未调用真实 AI 模型）\n\n"
                "已收到问题与知识上下文。请以上下文包中的【来源 #编号】"
                "逐条核对事实依据；本演示不产生真实推理结论。\n\n"
                "依据：【来源 #1】。"
            ),
            model=model or "mock-1",
            usage=None,
        )


class RagAnswerService:
    """Run the controlled answer chain over one context package."""

    def __init__(
        self,
        provider: CompletionProvider | None,
        *,
        prompt_builder: RagPromptBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._prompt_builder = prompt_builder or RagPromptBuilder()

    def answer(
        self,
        query: str,
        package: KnowledgeContextPackage,
        *,
        model: str | None = None,
        source_feature: str = "rag_answer",
    ) -> AuditedAIOutput:
        """Answer ``query`` strictly from ``package``, or raise fail-closed."""

        if not package.items:
            raise RagAnswerError("空上下文：没有可用知识，拒绝生成 AI 回答。")
        if all(not item.source_anchors for item in package.items):
            raise RagAnswerError(
                "无来源上下文：全部知识项都没有可回源来源，拒绝生成 AI 回答。"
            )
        provider = require_ai_provider(self._provider)
        prompt = self._prompt_builder.build(query, package)
        target_refs = tuple(item.stable_id for item in package.items)

        if isinstance(provider, AuditedAIProvider):
            result = provider.complete(
                prompt,
                model=model,
                source_feature=source_feature,
                target_refs=target_refs,
            )
        else:
            result = provider.complete(prompt, model=model)

        warnings = tuple(warning.message for warning in package.warnings)
        if not warnings and any(not item.source_anchors for item in package.items):
            warnings = ("部分知识项没有可回源来源。",)
        answer_citations = _validate_answer_citations(result.text, package)
        return AuditedAIOutput(
            output_id=str(uuid.uuid4()),
            query=query.strip(),
            context_package_id=package.package_uuid,
            provider=(
                "mock"
                if isinstance(provider, MockCompletionProvider)
                else "qwen"
            ),
            model=result.model,
            generated_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            answer=result.text,
            citations=package.citations,
            answer_citations=answer_citations,
            warnings=warnings,
            token_usage=result.usage,
            context_stable_ids=target_refs,
            excluded=tuple(
                (item.stable_id, item.reason) for item in package.excluded
            ),
            confidence=None,
        )


def _validate_answer_citations(
    text: str, package: KnowledgeContextPackage
) -> tuple[str, ...]:
    """Validate every citation in the AI answer against the context package.

    Citation markers may be either ``#N`` numbers (mapped through the
    package citation list) or raw ``<kb_uuid>:<type>:<id>`` stable ids. Any
    unknown, forged, blank or malformed citation fails closed; a valid
    citation set is deduplicated while preserving first-seen order.
    """

    package_stable_ids = {item.stable_id for item in package.items}
    citation_by_number: dict[int, str] = {}
    for stable_id, number in package.citations:
        if number.startswith("#") and number[1:].isdigit():
            citation_by_number[int(number[1:])] = stable_id

    found: list[str] = []
    for match in _STABLE_ID_TOKEN.finditer(text):
        token = match.group(0)
        if token not in package_stable_ids:
            raise RagAnswerError(
                f"引用校验失败：回答包含未知或非法的引用 {token}，拒绝显示。"
            )
        if token not in found:
            found.append(token)
    for match in _CITATION_NUMBER_TOKEN.finditer(text):
        number = int(match.group(1))
        if number not in citation_by_number:
            raise RagAnswerError(
                f"引用校验失败：回答引用了不存在的来源编号 #{number}，拒绝显示。"
            )
        stable_id = citation_by_number[number]
        if stable_id not in found:
            found.append(stable_id)
    if not found:
        raise RagAnswerError(
            "引用校验失败：AI 回答未包含任何合法引用，拒绝作为有依据回答显示。"
        )
    return tuple(found)
