"""Phase 1B read-only Tool Adapters.

Each adapter is a thin, dependency-injected boundary between the frozen Tool
Contract and an existing EKB read service. No Agent, no model decision, no
retry loop, no UI, and no provider dependency lives in this package.
"""

from src.agent.tools.adapters.knowledge_read import (
    GET_KNOWLEDGE_MEMORY_DEFINITION,
    GET_KNOWLEDGE_OBJECT_DEFINITION,
    KnowledgeMemoryAdapter,
    KnowledgeObjectAdapter,
)
from src.agent.tools.adapters.knowledge_search import (
    KNOWLEDGE_SEARCH_DEFINITION,
    KnowledgeSearchAdapter,
)
from src.agent.tools.adapters.page_search import (
    PAGE_SEARCH_DEFINITION,
    PageSearchAdapter,
)

__all__ = [
    "GET_KNOWLEDGE_MEMORY_DEFINITION",
    "GET_KNOWLEDGE_OBJECT_DEFINITION",
    "KNOWLEDGE_SEARCH_DEFINITION",
    "PAGE_SEARCH_DEFINITION",
    "KnowledgeMemoryAdapter",
    "KnowledgeObjectAdapter",
    "KnowledgeSearchAdapter",
    "PageSearchAdapter",
]
