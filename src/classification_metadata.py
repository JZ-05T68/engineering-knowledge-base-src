"""Unified, explicit per-rerun reads for classification picker metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.models import Document, Project, Tag


class ClassificationDocumentSort(StrEnum):
    """Document orders used by current classification-aware pages."""

    IMPORTED_DESC = "imported_desc"
    NAME_ASC = "name_asc"


class ClassificationMetadataRepository(Protocol):
    """Minimum bulk-read interface used by classification pickers."""

    def list_documents(self, *, sort_by: str = "imported_desc") -> list[Document]: ...

    def list_tags(self) -> list[Tag]: ...

    def list_projects(self) -> list[Project]: ...


@dataclass(frozen=True, slots=True)
class ClassificationMetadata:
    """Typed document, tag, and project options from one explicit load."""

    documents: tuple[Document, ...]
    tags: tuple[Tag, ...]
    projects: tuple[Project, ...]


class ClassificationMetadataService:
    """Read each classification type once per call, without cross-rerun cache."""

    def __init__(self, repository: ClassificationMetadataRepository) -> None:
        self.repository = repository

    def load(
        self,
        *,
        document_sort: ClassificationDocumentSort | str = (
            ClassificationDocumentSort.IMPORTED_DESC
        ),
    ) -> ClassificationMetadata:
        """Load fresh picker options so database changes are visible next rerun."""

        normalized_sort = ClassificationDocumentSort(document_sort)
        documents = tuple(self.repository.list_documents(sort_by=normalized_sort.value))
        tags = tuple(self.repository.list_tags())
        projects = tuple(self.repository.list_projects())
        return ClassificationMetadata(
            documents=documents,
            tags=tags,
            projects=projects,
        )
