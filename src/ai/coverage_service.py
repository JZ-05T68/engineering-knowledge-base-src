"""Read-only page-embedding coverage summary (v0.5.1 Phase 6A).

This module exposes a **public, read-only** coverage view over the existing
embedding persistence and freshness semantics. It never constructs an
``EmbeddingProvider``, never embeds text, never touches the network or an API
key, and never writes to the database: it only classifies each non-empty page
as indexed (fresh), missing, or stale, reusing the exact readiness policy the
indexer and recall layers already share.

This is a visibility remediation for D-01: making coverage observable does not
complete indexing, and must never be mistaken for a FIXED defect state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.ai.page_indexer import (
    EMBEDDING_CONFIG_VERSION,
    EMBEDDING_DIMENSIONS,
    prepare_page_text,
)
from src.config import Settings
from src.database import Database

__all__ = [
    "CoverageSummary",
    "PageEmbeddingCoverageService",
]

#: Re-export the indexer's shared constants so coverage classification can never
#: drift from indexing/recall freshness semantics. The embedding ``model`` follows
#: the single ``Settings.ai_embedding_model`` source (its field default, read
#: without constructing settings), never a duplicated literal.
coverage_model: Final = Settings.model_fields["ai_embedding_model"].default
coverage_dimensions: Final = EMBEDDING_DIMENSIONS
coverage_config_version: Final = EMBEDDING_CONFIG_VERSION


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Zero-cost, read-only classification of every page's embedding state."""

    indexable: int  # indexable pages = indexed + missing + stale (excludes skipped_empty)
    indexed: int  # fresh embedding present (recall-eligible)
    missing: int  # no stored embedding at all
    stale: int  # stored but source_text_sha256 no longer matches
    skipped_empty: int  # no indexable text; never reduces coverage

    @property
    def coverage_ratio(self) -> float:
        """Fresh embeddings / indexable pages (0.0 when nothing is indexable)."""

        if self.indexable == 0:
            return 0.0
        return self.indexed / self.indexable


class PageEmbeddingCoverageService:
    """Read-only coverage classification reusing the shared readiness policy.

    Classification mirrors ``PageEmbeddingIndexer._classify_pages`` but without
    any provider or write path: for every page, empty text is ``skipped_empty``;
    otherwise a fresh stored row (matching ``source_text_sha256``) counts as
    ``indexed``, and any non-fresh page is ``missing`` when no row exists and
    ``stale`` when a row exists under the current configuration.
    """

    def __init__(
        self,
        *,
        database: Database,
        model: str = coverage_model,
        dimensions: int = coverage_dimensions,
        config_version: int = coverage_config_version,
    ) -> None:
        if not model.strip():
            raise ValueError("embedding model 不能为空")
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError(f"dimensions 必须为正整数：{dimensions!r}")
        if (
            isinstance(config_version, bool)
            or not isinstance(config_version, int)
            or config_version <= 0
        ):
            raise ValueError(f"config_version 必须为正整数：{config_version!r}")
        self._database = database
        self._model = model.strip()
        self._dimensions = dimensions
        self._config_version = config_version

    def coverage_summary(self) -> CoverageSummary:
        """Classify every local page without any provider call or write."""

        indexed = 0
        missing = 0
        stale = 0
        skipped_empty = 0
        for document in self._database.list_documents():
            for page in self._database.list_pages(document.id):
                prepared = prepare_page_text(page.searchable_content)
                if prepared is None:
                    skipped_empty += 1
                    continue
                fresh = self._database.get_fresh_page_embedding(
                    page_id=page.id,
                    source_text_sha256=prepared.sha256,
                    model=self._model,
                    dimensions=self._dimensions,
                    config_version=self._config_version,
                )
                if fresh is not None:
                    indexed += 1
                    continue
                stored = self._database.get_page_embedding(
                    page_id=page.id,
                    model=self._model,
                    dimensions=self._dimensions,
                    config_version=self._config_version,
                )
                if stored is not None:
                    stale += 1
                else:
                    missing += 1
        total = indexed + missing + stale
        return CoverageSummary(
            indexable=total,
            indexed=indexed,
            missing=missing,
            stale=stale,
            skipped_empty=skipped_empty,
        )
