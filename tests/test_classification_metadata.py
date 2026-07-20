"""Tests for unified, uncached classification metadata reads."""

from __future__ import annotations

from pathlib import Path

from src.classification_metadata import (
    ClassificationDocumentSort,
    ClassificationMetadataService,
)
from src.database import Database


class CountingDatabase(Database):
    """Count public metadata reads and fail on accidental per-page relations."""

    def __init__(self, database_path: Path) -> None:
        self.metadata_reads = {"documents": 0, "tags": 0, "projects": 0}
        self.page_relation_reads = 0
        super().__init__(database_path)

    def list_documents(self, **kwargs):
        self.metadata_reads["documents"] += 1
        return super().list_documents(**kwargs)

    def list_tags(self):
        self.metadata_reads["tags"] += 1
        return super().list_tags()

    def list_projects(self):
        self.metadata_reads["projects"] += 1
        return super().list_projects()

    def get_page_tags(self, page_id: int):
        self.page_relation_reads += 1
        return super().get_page_tags(page_id)

    def get_page_projects(self, page_id: int):
        self.page_relation_reads += 1
        return super().get_page_projects(page_id)


def _create_document(database: Database, root: Path, title: str, suffix: str):
    return database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=root / f"{title}.pdf",
        sha256=suffix * 64,
    )


def test_metadata_load_reads_each_classification_once_and_never_per_page(
    tmp_path: Path,
) -> None:
    database = CountingDatabase(tmp_path / "metadata.db")
    second = _create_document(database, tmp_path, "B 文档", "b")
    _create_document(database, tmp_path, "A 文档", "a")
    for page_number in range(1, 26):
        database.create_page(
            document_id=second.id,
            page_number=page_number,
            image_path=tmp_path / f"page-{page_number}.png",
        )
    database.create_tag("B 标签")
    database.create_tag("A 标签")
    database.create_project("B 项目")
    database.create_project("A 项目")

    metadata = ClassificationMetadataService(database).load(
        document_sort=ClassificationDocumentSort.NAME_ASC
    )

    assert database.metadata_reads == {"documents": 1, "tags": 1, "projects": 1}
    assert database.page_relation_reads == 0
    assert [document.title for document in metadata.documents] == ["A 文档", "B 文档"]
    assert [tag.name for tag in metadata.tags] == ["A 标签", "B 标签"]
    assert [project.name for project in metadata.projects] == ["A 项目", "B 项目"]


def test_metadata_load_refreshes_after_changes_without_process_cache(tmp_path: Path) -> None:
    database = CountingDatabase(tmp_path / "refresh.db")
    _create_document(database, tmp_path, "初始文档", "a")
    service = ClassificationMetadataService(database)

    initial = service.load()
    tag = database.create_tag("新标签")
    project = database.create_project("新项目")
    refreshed = service.load()
    database.delete_tag(tag.id)
    database.update_project(
        project.id,
        name="重命名项目",
        description=project.description,
        status=project.status,
    )
    changed_again = service.load()

    assert initial.tags == () and initial.projects == ()
    assert [item.name for item in refreshed.tags] == ["新标签"]
    assert [item.name for item in refreshed.projects] == ["新项目"]
    assert changed_again.tags == ()
    assert [item.name for item in changed_again.projects] == ["重命名项目"]
    assert database.metadata_reads == {"documents": 3, "tags": 3, "projects": 3}


def test_metadata_services_for_different_databases_do_not_share_state(tmp_path: Path) -> None:
    first_database = Database(tmp_path / "first.db")
    second_database = Database(tmp_path / "second.db")
    _create_document(first_database, tmp_path, "第一库", "a")
    _create_document(second_database, tmp_path, "第二库", "b")
    first_database.create_tag("只在第一库")
    second_database.create_project("只在第二库")

    first = ClassificationMetadataService(first_database).load()
    second = ClassificationMetadataService(second_database).load()

    assert [document.title for document in first.documents] == ["第一库"]
    assert [tag.name for tag in first.tags] == ["只在第一库"]
    assert first.projects == ()
    assert [document.title for document in second.documents] == ["第二库"]
    assert second.tags == ()
    assert [project.name for project in second.projects] == ["只在第二库"]
