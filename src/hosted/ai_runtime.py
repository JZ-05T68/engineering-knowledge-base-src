"""Hosted AI composition: existing Qwen adapter, DB audit and UTC token budgets."""

from __future__ import annotations

from datetime import UTC, datetime

from src.ai.provider import (
    AiCallRecord,
    AIUnavailableError,
    AuditedAIProvider,
    build_production_audited_provider,
    require_production_audited_provider,
)
from src.ai.qwen_client import QwenProvider, urllib_transport
from src.database import Database
from src.hosted_config import HostedSettings


class HostedDatabaseAiCallLedger:
    """Append audit metadata only to the bootstrap-owned Hosted database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, call: AiCallRecord) -> None:
        self._database.insert_ai_call(call)


class HostedDatabaseAiBudgetGuard:
    """Reuse Local UTC period/recorded-token semantics; no new accounting system.

    Zero disables a period, but both zero fail closed in Hosted. This is a
    preflight check of recorded usage, not a token reservation or cost ceiling
    for concurrent in-flight calls. Unknown usage retains existing semantics.
    """

    def __init__(self, database: Database, settings: HostedSettings) -> None:
        self._database = database
        self._daily = settings.ai_daily_token_budget
        self._monthly = settings.ai_monthly_token_budget

    def ensure_allowed(self, capability: str) -> None:
        if not (self._daily > 0 or self._monthly > 0):
            raise AIUnavailableError("AI 预算未配置。")
        now = datetime.now(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for limit, start in ((self._daily, day), (self._monthly, day.replace(day=1))):
            if limit > 0 and self._database.total_ai_tokens_since(
                start.isoformat(timespec="microseconds")
            ) >= limit:
                raise AIUnavailableError("AI 调用被预算限制拒绝。")


def build_hosted_ai_provider(
    settings: HostedSettings, database: Database,
) -> AuditedAIProvider:
    """Wire the real transport without calling it, probing models or requiring a key."""
    provider = QwenProvider(
        api_key=settings.ai_api_key.get_secret_value(),
        llm_model=settings.ai_llm_model,
        llm_model_hard=settings.ai_llm_model_hard,
        embedding_model=settings.ai_embedding_model,
        rerank_model=settings.ai_rerank_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_extra_attempts=settings.ai_max_extra_attempts,
        enable_thinking=False,
        transport=urllib_transport,
    )
    audited = build_production_audited_provider(
        provider,
        default_model=settings.ai_llm_model,
        default_embedding_model=settings.ai_embedding_model,
        source_feature="hosted_agent",
        ledger=HostedDatabaseAiCallLedger(database),
        budget_guard=HostedDatabaseAiBudgetGuard(database, settings),
    )
    return require_production_audited_provider(audited)
