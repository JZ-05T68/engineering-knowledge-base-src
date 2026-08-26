"""Phase 1 read-only Tool Adapters (v0.6.0 Phase 1B + 1C).

Each adapter is a thin, dependency-injected boundary between the frozen Tool
Contract and an existing EKB read service. No Agent, no model decision, no
retry loop, no UI, and no provider dependency lives in this package.
"""

from src.agent.tools.adapters.evidence import (
    GET_EVIDENCE_DEFINITION,
    GetEvidenceAdapter,
)
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
from src.agent.tools.adapters.provenance import (
    INSPECT_PROVENANCE_DEFINITION,
    InspectProvenanceAdapter,
)
from src.agent.tools.adapters.source_integrity import (
    INSPECT_SOURCE_INTEGRITY_DEFINITION,
    InspectSourceIntegrityAdapter,
)

__all__ = [
    "GET_EVIDENCE_DEFINITION",
    "GET_KNOWLEDGE_MEMORY_DEFINITION",
    "GET_KNOWLEDGE_OBJECT_DEFINITION",
    "INSPECT_PROVENANCE_DEFINITION",
    "INSPECT_SOURCE_INTEGRITY_DEFINITION",
    "KNOWLEDGE_SEARCH_DEFINITION",
    "PAGE_SEARCH_DEFINITION",
    "GetEvidenceAdapter",
    "InspectProvenanceAdapter",
    "InspectSourceIntegrityAdapter",
    "KnowledgeMemoryAdapter",
    "KnowledgeObjectAdapter",
    "KnowledgeSearchAdapter",
    "PageSearchAdapter",
]
