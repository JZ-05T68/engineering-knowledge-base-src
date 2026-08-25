"""Canonical source fingerprints (ADR-03, recipe version 1).

A fingerprint is the SHA-256 of a version-prefixed canonical rendering of one
source entity. It is computed from the entity's durable content only; display
fields and workflow state are deliberately excluded. The read path is
read-only: callers compare the freshly computed value with the stored snapshot
and never write back during a read.
"""

from __future__ import annotations

import hashlib
import sqlite3

from src.models import KnowledgeObjectSourceType

FINGERPRINT_VERSION = 1

_DOCUMENT_PREFIX = "ekb-doc-fp-v1\n"
_PAGE_PREFIX = "ekb-page-fp-v1\n"
_NOTE_PREFIX = "ekb-note-fp-v1\n"
_EVIDENCE_PREFIX = "ekb-evidence-fp-v1\n"


def compute_source_fingerprint(
    connection: sqlite3.Connection,
    source_type: KnowledgeObjectSourceType | str,
    source_id: int,
) -> str | None:
    """Return the canonical fingerprint, or ``None`` when it cannot be computed.

    ``None`` means the target row is missing or has no usable canonical input
    (for example a page without any text layer). Callers treat ``None`` as
    "unverifiable"; the write path rejects it with a clear Chinese error.
    """

    normalized_type = KnowledgeObjectSourceType(source_type)
    if normalized_type is KnowledgeObjectSourceType.DOCUMENT:
        row = connection.execute(
            "SELECT sha256 FROM documents WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        sha256 = str(row["sha256"] or "").strip()
        if not sha256:
            return None
        return _sha256(_DOCUMENT_PREFIX + sha256)
    if normalized_type is KnowledgeObjectSourceType.PAGE:
        row = connection.execute(
            "SELECT extracted_text, ocr_text FROM pages WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        page_source_text = str(row["extracted_text"] or "").strip() or str(
            row["ocr_text"] or ""
        ).strip()
        if not page_source_text:
            return None
        return _sha256(_PAGE_PREFIX + page_source_text)
    if normalized_type is KnowledgeObjectSourceType.NOTE:
        row = connection.execute(
            "SELECT note_type, personal_note, user_excerpt, region_image_sha256"
            " FROM notes WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        canonical = "\n".join(
            (
                str(row["note_type"]),
                str(row["personal_note"]),
                str(row["user_excerpt"] or ""),
                str(row["region_image_sha256"] or ""),
            )
        )
        return _sha256(_NOTE_PREFIX + canonical)
    if normalized_type is KnowledgeObjectSourceType.EVIDENCE:
        row = connection.execute(
            "SELECT evidence_type, source_text_sha256, selection_sha256,"
            " evidence_text FROM evidence_items WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        canonical = "\n".join(
            (
                str(row["evidence_type"]),
                str(row["source_text_sha256"]),
                str(row["selection_sha256"]),
                str(row["evidence_text"]),
            )
        )
        return _sha256(_EVIDENCE_PREFIX + canonical)
    raise ValueError(f"未知来源类型：{normalized_type.value}")


def _sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
