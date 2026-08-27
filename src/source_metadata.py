"""Small DB-backed source display projection over explicitly supplied read methods.

No service construction, SQL, filesystem access, UI or HTTP dependency. Callers
bind the existing Database get methods and EvidenceBasketService.get_item; mixed
read/write service modules are deliberately not imported here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from src.models import (
    ContextItemType,
    Document,
    EvidenceItem,
    KnowledgeMemoryEntry,
    KnowledgeObject,
    Page,
    build_stable_id,
)

_STABLE_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r":([a-z][a-z0-9_]{0,63}):([1-9][0-9]{0,18})"
)


class InvalidSourceId(ValueError):
    """Client identity is not canonical; the message never echoes input."""


def parse_source_id(stable_id: str) -> tuple[str, str, int]:
    """Strict public spelling: lowercase UUID, type, positive SQLite integer.

    The older Tool helper accepts whitespace and int aliases, so it is not the
    public parser. Unknown well-formed types are parsed but never guessed/read.
    """

    match = _STABLE_ID.fullmatch(stable_id) if isinstance(stable_id, str) else None
    if match is None or int(match[3]) > 2**63 - 1:
        raise InvalidSourceId("来源标识格式无效。")
    return match[1], match[2], int(match[3])


def safe_display_text(value: str | None) -> str | None:
    """Omit path-like/control-bearing legacy titles rather than expose a path.

    Conservative display filtering, not a corpus privacy scanner. It neither
    parses nor resolves a filesystem path and never invents replacement titles.
    """

    if not value or len(value) > 500 or any(ord(char) < 32 for char in value):
        return None
    if any(marker in value for marker in ("/", "\\", ":")):
        return None
    return value


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    stable_id: str
    type: ContextItemType
    title: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class SourceMetadataService:
    """Reuse existing DB read capabilities, without mounting their write APIs."""

    kb_uuid: str
    read_page: Callable[[int], Page | None]
    read_document: Callable[[int], Document | None]
    read_knowledge_object: Callable[[int], KnowledgeObject | None]
    read_knowledge_memory: Callable[[int], KnowledgeMemoryEntry | None]
    read_evidence: Callable[[int], EvidenceItem | None]

    def get(self, stable_id: str) -> SourceMetadata | None:
        kb_uuid, kind, local_id = parse_source_id(stable_id)
        if kb_uuid != self.kb_uuid or kind not in {item.value for item in ContextItemType}:
            return None
        title: str | None
        label: str | None
        if kind == ContextItemType.PAGE:
            page = self.read_page(local_id)
            if page is None:
                return None
            document = self.read_document(page.document_id)
            if document is None:
                return None
            title, label = document.title, f"第 {page.page_number} 页"
        elif kind == ContextItemType.KNOWLEDGE_OBJECT:
            item = self.read_knowledge_object(local_id)
            if item is None:
                return None
            title, label = item.title, item.kind.label
        elif kind == ContextItemType.KNOWLEDGE_MEMORY:
            entry = self.read_knowledge_memory(local_id)
            if entry is None:
                return None
            title, label = entry.title, entry.kind.label
        else:
            evidence = self.read_evidence(local_id)
            if evidence is None:
                return None
            title, label = evidence.document_title, f"第 {evidence.page_number} 页"
        return SourceMetadata(
            stable_id=build_stable_id(self.kb_uuid, kind, local_id),
            type=ContextItemType(kind),
            title=safe_display_text(title),
            label=safe_display_text(label),
        )
