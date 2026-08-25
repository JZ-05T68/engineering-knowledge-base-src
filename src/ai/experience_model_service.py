"""Structured experience-model service (v0.5.3 Phase 4).

``ExperienceModelService`` converts a user-selected ``KnowledgeContextPackage``
into one structured, auditable ``AuditedExperienceOutput``. It is an explicit,
on-demand AI transformation — not an agent, not automatic experience learning
and not background memory.

Frozen boundaries:

- the provider never accesses the database;
- the provider input always comes from the context package, never from raw
  page / knowledge-object / memory / evidence reads;
- only the context the user selected this time is processed;
- no knowledge asset is written, confirmed or modified;
- no prompt, full context text or raw model reply is persisted;
- provider failures become typed domain errors, never half-parsed candidates.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from src.ai.experience_prompt_builder import ExperiencePromptBuilder
from src.ai.provider import (
    AuditedAIProvider,
    CompletionProvider,
    require_ai_provider,
)
from src.ai.rag_answer_service import MockCompletionProvider
from src.knowledge_context_packager import KnowledgeContextPackage
from src.models import AuditedExperienceOutput, ExperienceCandidate

__all__ = [
    "ExperienceModelError",
    "ExperienceModelService",
]

_TEXT_FIELDS = (
    "problem",
    "context",
    "action",
    "result",
    "root_cause",
    "lesson",
    "applicability",
    "limitations",
)
_TITLE_MAX = 200
_FIELD_MAX = 4000


class ExperienceModelError(ValueError):
    """Raised when the experience-model chain cannot run safely."""


class ExperienceModelService:
    """Run one explicit, audited experience-candidate generation."""

    def __init__(
        self,
        provider: CompletionProvider | None,
        *,
        prompt_builder: ExperiencePromptBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._prompt_builder = prompt_builder or ExperiencePromptBuilder()

    def generate(
        self,
        task: str,
        package: KnowledgeContextPackage,
        *,
        model: str | None = None,
        source_feature: str = "experience_model",
    ) -> AuditedExperienceOutput:
        """Generate one structured candidate or raise ``ExperienceModelError``."""

        if not package.items:
            raise ExperienceModelError("空上下文：没有可用知识，拒绝生成经验候选。")
        if all(not item.source_anchors for item in package.items):
            raise ExperienceModelError(
                "无来源上下文：全部知识项都没有可回源来源，拒绝生成经验候选。"
            )
        provider = require_ai_provider(self._provider)
        target_refs = tuple(item.stable_id for item in package.items)

        if isinstance(provider, MockCompletionProvider):
            candidate, warnings = self._mock_candidate(task, package)
            return self._build_output(
                task, package, candidate, warnings, provider="mock",
                model="mock-1", is_mock=True,
            )

        prompt = self._prompt_builder.build(task, package)
        if isinstance(provider, AuditedAIProvider):
            result = provider.complete(
                prompt,
                model=model,
                source_feature=source_feature,
                target_refs=target_refs,
            )
        else:
            result = provider.complete(prompt, model=model)

        candidate = self._parse_candidate(result.text, package)
        warnings = tuple(warning.message for warning in package.warnings)
        if not warnings and any(not item.source_anchors for item in package.items):
            warnings = ("部分知识项没有可回源来源。",)
        return self._build_output(
            task, package, candidate, warnings, provider="qwen",
            model=result.model, is_mock=False,
        )

    def _build_output(
        self,
        task: str,
        package: KnowledgeContextPackage,
        candidate: ExperienceCandidate,
        warnings: tuple[str, ...],
        *,
        provider: str,
        model: str,
        is_mock: bool,
    ) -> AuditedExperienceOutput:
        return AuditedExperienceOutput(
            output_id=str(uuid.uuid4()),
            task=task.strip(),
            context_package_id=package.package_uuid,
            provider=provider,
            model=model,
            audit_call_id=None,
            generated_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            candidate=candidate,
            warnings=warnings,
            is_mock=is_mock,
        )

    def _mock_candidate(
        self, task: str, package: KnowledgeContextPackage
    ) -> tuple[ExperienceCandidate, tuple[str, ...]]:
        """Build a deterministic offline candidate; never random, never fake-real."""

        first = package.items[0]
        candidate = ExperienceCandidate(
            title=f"经验整理：{first.title}",
            problem=task.strip(),
            context="离线演示生成，未调用真实模型。",
            action="",
            result="",
            root_cause="",
            lesson=first.title,
            applicability="",
            limitations="本候选由离线演示生成，仅用于界面与链路验证，不应视为真实经验。",
            citations=(first.stable_id,),
        )
        warnings = (
            "离线演示生成，未调用真实模型。",
            *tuple(warning.message for warning in package.warnings),
        )
        return candidate, warnings

    def _parse_candidate(
        self, text: str, package: KnowledgeContextPackage
    ) -> ExperienceCandidate:
        """Parse and fail-closed validate the structured model output."""

        payload = _extract_json_object(text)
        if payload is None:
            raise ExperienceModelError("AI 返回内容不是可解析的 JSON，拒绝生成经验候选。")
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ExperienceModelError("AI 返回缺少非空 title 字段，拒绝生成经验候选。")
        title = title.strip()
        if len(title) > _TITLE_MAX:
            raise ExperienceModelError(f"title 超过 {_TITLE_MAX} 个字符。")

        fields: dict[str, str] = {}
        for field in _TEXT_FIELDS:
            value = payload.get(field, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ExperienceModelError(f"字段 {field} 必须是字符串。")
            value = value.strip()
            if len(value) > _FIELD_MAX:
                raise ExperienceModelError(f"字段 {field} 超过 {_FIELD_MAX} 个字符。")
            fields[field] = value

        citations = _validated_citations(payload.get("citations"), package)
        return ExperienceCandidate(title=title, citations=citations, **fields)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a possibly fenced model reply."""

    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _validated_citations(
    raw: Any, package: KnowledgeContextPackage
) -> tuple[str, ...]:
    """Validate the citation list is a deduplicated subset of the package ids."""

    package_stable_ids = {item.stable_id for item in package.items}
    if not isinstance(raw, list) or not raw:
        raise ExperienceModelError("AI 返回未包含任何合法 citation，拒绝生成经验候选。")
    citations: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ExperienceModelError("citation 存在空白或非字符串项。")
        stable_id = item.strip()
        if stable_id not in package_stable_ids:
            raise ExperienceModelError(
                f"引用校验失败：未知或非法的 citation {stable_id}，拒绝生成经验候选。"
            )
        if stable_id not in citations:
            citations.append(stable_id)
    return tuple(citations)
