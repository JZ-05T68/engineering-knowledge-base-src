"""Optional AI capability boundary for Engineering Knowledge Base.

This package holds the vendor-neutral provider contracts
(``src.ai.provider``) and vendor adapters (``src.ai.qwen_client``). It
performs no I/O at import time and is safe to import in any mode,
including the default manual AI mode with no API key configured.
"""

from src.ai.provider import (
    AiBudgetGuard,
    AiCallLedger,
    AiCallRecord,
    AIError,
    AIExecutionError,
    AiOutputRecord,
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
    require_ai_provider,
)

__all__ = [
    "AIError",
    "AIExecutionError",
    "AIProvider",
    "AIUnavailableError",
    "AiBudgetGuard",
    "AiCallLedger",
    "AiCallRecord",
    "AiOutputRecord",
    "AuditedAIProvider",
    "CompletionProvider",
    "CompletionResult",
    "CompletionUsage",
    "EmbeddingProvider",
    "EmbeddingResult",
    "RerankHit",
    "RerankProvider",
    "RerankResult",
    "require_ai_provider",
]
