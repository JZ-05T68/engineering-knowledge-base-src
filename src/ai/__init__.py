"""Optional AI capability boundary for Engineering Knowledge Base.

This package holds the vendor-neutral provider contracts
(``src.ai.provider``) and vendor adapters (``src.ai.qwen_client``). It
performs no I/O at import time and is safe to import in any mode,
including the default manual AI mode with no API key configured.
"""

from src.ai.provider import (
    AIBudgetExceededError,
    AiBudgetGuard,
    AiCallLedger,
    AiCallRecord,
    AIError,
    AIExecutionError,
    AiOutputRecord,
    AIProductionCompositionError,
    AIProvider,
    AIUnavailableError,
    AuditedAIProvider,
    CompletionProvider,
    CompletionResult,
    CompletionUsage,
    EmbeddingProvider,
    EmbeddingResult,
    RerankHit,
    RerankProvider,
    RerankResult,
    build_production_audited_provider,
    require_ai_provider,
    require_production_audited_provider,
)

__all__ = [
    "AIError",
    "AIExecutionError",
    "AIProvider",
    "AIBudgetExceededError",
    "AIProductionCompositionError",
    "AIUnavailableError",
    "AiBudgetGuard",
    "AiCallLedger",
    "AiCallRecord",
    "AiOutputRecord",
    "AuditedAIProvider",
    "build_production_audited_provider",
    "CompletionProvider",
    "CompletionResult",
    "CompletionUsage",
    "EmbeddingProvider",
    "EmbeddingResult",
    "RerankHit",
    "RerankProvider",
    "RerankResult",
    "require_ai_provider",
    "require_production_audited_provider",
]
