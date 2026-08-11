"""Tests for document aggregation impact and deletion integration.

Impact semantics are the frozen S3 effective-association rules: an axis is
affected only when the document really has knowledge entries (notes or
evidence) reaching it. Deletion always goes through
DocumentDeletionService; fixtures live under ``tmp_path`` only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pdf_test_helpers import build_sample_pdf
from PIL import Image

from src.aggregation_service import AggregationError, AggregationService
from src.database import Database
from src.document_deletion_service import (
    DocumentDeletionError,
    DocumentDeletionService,
)
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.note_service import NoteService


def _make_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    service = AggregationService(database)
    deletion = DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
    )
    return database, service, deletion, data_dir, raw_dir, pages_dir, markdown_dir


def _create_document(
    database: Database,
    raw_dir: Path,
    pages_dir: Path,
    *,
    title: str,
    sha_letter: str,
    page_count: int = 1,
):
    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=page_count,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 100)
    pages = []
    for number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        pages.append(
            database.create_page(
                document_id=document.id,
                page_number=number,
                image_path=image_path,
                extracted_text=f"第 {number} 页 阀体 回路 {title}",
            )
        )
    database.update_document_page_count(document.id, page_count)
    return document, pages


def _impact(tmp_path: Path):
    """Document with one note; one project and one tag available."""

    database, service, deletion, *dirs = _make_env(tmp_path)
    document, pages = _create_document(
        database, dirs[1], dirs[2], title="甲文档", sha_letter="a", page_count=2
    )
    project = database.create_project("主项目")
    tag = database.create_tag("泵")
    return database, service, deletion, dirs, document, pages, project, tag


# --- impact semantics -----------------------------------------------------------


def test_nonexistent_document_raises(tmp_path: Path) -> None:
    _, service, *_ = _make_env(tmp_path)
    with pytest.raises(AggregationError, match="找不到文档"):
        service.get_document_aggregation_impacts(9999)


def test_no_association_no_impact(tmp_path: Path) -> None:
    database, service, _, _, raw_dir, pages_dir, _ = _make_env(tmp_path)
    document, pages = _create_document(
        database, raw_dir, pages_dir, title="无关联", sha_letter="b"
    )
    NoteService(database).create_page_note(pages[0].id, "普通笔记")

    impact = service.get_document_aggregation_impacts(document.id)

    assert impact.projects == ()
    assert impact.tags == ()


def test_document_level_project_with_knowledge_is_impact(tmp_path: Path) -> None:
    database, service, _, dirs, document, pages, project, _ = _impact(tmp_path)
    NoteService(database).create_document_note(document.id, "文档级笔记")
    database.set_document_projects(document.id, [project.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert [item.name for item in impact.projects] == ["主项目"]
    assert impact.tags == ()


def test_document_level_relation_without_knowledge_is_not_impact(tmp_path: Path) -> None:
    database, service, _, dirs, document, _, project, tag = _impact(tmp_path)
    database.set_document_projects(document.id, [project.id])
    database.set_document_tags(document.id, [tag.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert impact.projects == ()
    assert impact.tags == ()


def test_page_level_relation_with_page_knowledge_is_impact(tmp_path: Path) -> None:
    database, service, _, dirs, document, pages, project, tag = _impact(tmp_path)
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    database.set_page_projects(pages[0].id, [project.id])
    database.set_page_tags(pages[0].id, [tag.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert [item.name for item in impact.projects] == ["主项目"]
    assert [item.name for item in impact.tags] == ["泵"]


def test_page_level_relation_with_knowledge_on_other_page_is_not_impact(
    tmp_path: Path,
) -> None:
    """A page-level link covers only knowledge anchored on that page."""

    database, service, _, dirs, document, pages, project, _ = _impact(tmp_path)
    NoteService(database).create_page_note(pages[1].id, "另一页的笔记")
    database.set_page_projects(pages[0].id, [project.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert impact.projects == ()


def test_multiple_axes_and_duplicate_paths_dedup(tmp_path: Path) -> None:
    database, service, _, dirs, document, pages, project, tag = _impact(tmp_path)
    second_project = database.create_project("辅项目")
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    # The same note reaches 主项目 via document-level AND page-level links.
    database.set_document_projects(document.id, [project.id])
    database.set_page_projects(pages[0].id, [project.id])
    database.set_page_projects(pages[0].id, [second_project.id])
    database.set_document_tags(document.id, [tag.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert [item.name for item in impact.projects] == ["主项目", "辅项目"]
    assert [item.name for item in impact.tags] == ["泵"]


def test_each_note_type_creates_impact(tmp_path: Path) -> None:
    note_factories = (
        lambda notes, document, pages: notes.create_document_note(document.id, "文档"),
        lambda notes, document, pages: notes.create_page_note(pages[0].id, "页面"),
        lambda notes, document, pages: notes.create_text_selection_note(
            pages[0].id, "阀体", "选区"
        ),
        lambda notes, document, pages: notes.create_image_region_note(
            pages[0].id, 10, 20, 300, 400, "区域"
        ),
    )
    for index, make_note in enumerate(note_factories):
        root = tmp_path / f"case{index}"
        root.mkdir()
        database, service, _, _, raw_dir, pages_dir, _ = _make_env(root)
        document, pages = _create_document(
            database, raw_dir, pages_dir, title="类型", sha_letter="c"
        )
        project = database.create_project("类型项目")
        database.set_document_projects(document.id, [project.id])
        make_note(NoteService(database), document, pages)

        impact = service.get_document_aggregation_impacts(document.id)
        assert [item.name for item in impact.projects] == ["类型项目"]


def test_evidence_only_knowledge_creates_impact(tmp_path: Path) -> None:
    database, service, _, dirs, document, pages, project, _ = _impact(tmp_path)
    EvidenceBasketService(database).add_item(
        document_id=document.id, page_id=pages[0].id, evidence_text="阀体"
    )
    database.set_page_projects(pages[0].id, [project.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert [item.name for item in impact.projects] == ["主项目"]


def test_evidence_without_importance_still_counts_as_knowledge(tmp_path: Path) -> None:
    database, service, _, dirs, document, pages, _, tag = _impact(tmp_path)
    EvidenceBasketService(database).add_item(
        document_id=document.id, page_id=pages[0].id, evidence_text="阀体"
    )
    database.set_document_tags(document.id, [tag.id])

    impact = service.get_document_aggregation_impacts(document.id)

    assert [item.name for item in impact.tags] == ["泵"]


# --- deletion integration ---------------------------------------------------------


def test_preview_lists_impact_names(tmp_path: Path) -> None:
    database, _, deletion, dirs, document, pages, project, tag = _impact(tmp_path)
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    database.set_document_projects(document.id, [project.id])
    database.set_document_tags(document.id, [tag.id])

    preview = deletion.preview_document_deletion(document.id)

    assert len(preview.aggregation_impact.projects) == 1
    assert len(preview.aggregation_impact.tags) == 1
    assert preview.aggregation_impact.projects[0].name == "主项目"
    assert preview.aggregation_impact.tags[0].name == "泵"


def test_preview_impact_survives_missing_source_file(tmp_path: Path) -> None:
    database, _, deletion, dirs, document, pages, project, _ = _impact(tmp_path)
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    database.set_document_projects(document.id, [project.id])
    Path(document.source_path).unlink()

    preview = deletion.preview_document_deletion(document.id)

    assert [item.name for item in preview.aggregation_impact.projects] == ["主项目"]
    assert preview.missing_files


def test_preview_impact_readable_despite_path_anomaly(tmp_path: Path) -> None:
    database, _, deletion, dirs, document, pages, project, _ = _impact(tmp_path)
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    database.set_document_projects(document.id, [project.id])
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET image_path = ? WHERE id = ?",
            (str(dirs[0].parent / "outside.png"), pages[0].id),
        )

    preview = deletion.preview_document_deletion(document.id)

    assert preview.path_anomalies
    assert [item.name for item in preview.aggregation_impact.projects] == ["主项目"]
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        deletion.delete_document(document.id, expected_title=document.title)


def test_deletion_removes_impact_and_preserves_axes(tmp_path: Path) -> None:
    database, service, deletion, dirs, document, pages, project, tag = _impact(
        tmp_path
    )
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    basket = EvidenceBasketService(database)
    basket.add_item(document_id=document.id, page_id=pages[0].id, evidence_text="阀体")
    database.set_document_projects(document.id, [project.id])
    database.set_document_tags(document.id, [tag.id])
    assert service.get_document_aggregation_impacts(document.id).projects

    deletion.delete_document(document.id, expected_title=document.title)

    with pytest.raises(AggregationError, match="找不到文档"):
        service.get_document_aggregation_impacts(document.id)
    assert service.aggregate_by_project(project.id).total_count == 0
    assert service.aggregate_by_tag(tag.id).total_count == 0
    # Shared axis entities and the basket itself survive.
    assert any(item.id == project.id for item in database.list_projects())
    assert any(item.id == tag.id for item in database.list_tags())
    assert basket.list_baskets()


def test_other_document_aggregation_survives_deletion(tmp_path: Path) -> None:
    database, service, deletion, dirs, document, pages, project, _ = _impact(
        tmp_path
    )
    other, other_pages = _create_document(
        database, dirs[1], dirs[2], title="乙文档", sha_letter="d"
    )
    notes = NoteService(database)
    notes.create_page_note(pages[0].id, "甲页面笔记")
    notes.create_page_note(other_pages[0].id, "乙页面笔记")
    database.set_document_projects(document.id, [project.id])
    database.set_document_projects(other.id, [project.id])

    deletion.delete_document(document.id, expected_title=document.title)

    result = service.aggregate_by_project(project.id)
    assert result.total_count == 1
    assert result.items[0].document_id == other.id
    assert result.items[0].content == "乙页面笔记"


def test_failed_deletion_keeps_aggregation_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, deletion, dirs, document, pages, project, _ = _impact(
        tmp_path
    )
    NoteService(database).create_page_note(pages[0].id, "页面笔记")
    database.set_document_projects(document.id, [project.id])
    before = service.aggregate_by_project(project.id).total_count

    def failing_delete(self, document_id):
        raise DocumentDeletionError("模拟数据库删除失败")

    monkeypatch.setattr(
        DocumentDeletionService, "_delete_document_records", failing_delete
    )
    with pytest.raises(DocumentDeletionError):
        deletion.delete_document(document.id, expected_title=document.title)

    after = service.aggregate_by_project(project.id)
    assert after.total_count == before
    assert [item.name for item in service.get_document_aggregation_impacts(
        document.id
    ).projects] == ["主项目"]


def test_reimport_after_delete_can_aggregate_again(tmp_path: Path) -> None:
    database, service, deletion, dirs, *_ = _impact(tmp_path)
    raw_dir, pages_dir, markdown_dir = dirs[1], dirs[2], dirs[3]
    project = database.create_project("重导项目")

    source = build_sample_pdf(tmp_path / "样例.pdf")
    content = source.read_bytes()
    documents = DocumentService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
    )
    first = documents.import_pdf(content, source.name, title="重导样例")
    NoteService(database).create_document_note(first.document.id, "首轮笔记")
    database.set_document_projects(first.document.id, [project.id])
    assert service.aggregate_by_project(project.id).total_count == 1

    deletion.delete_document(first.document.id, expected_title=first.document.title)
    assert service.aggregate_by_project(project.id).total_count == 0

    second = documents.import_pdf(content, source.name, title="重导样例")
    assert second.document.id != first.document.id
    assert service.aggregate_by_project(project.id).total_count == 0
    NoteService(database).create_document_note(second.document.id, "第二轮笔记")
    database.set_document_projects(second.document.id, [project.id])
    result = service.aggregate_by_project(project.id)
    assert result.total_count == 1
    assert result.items[0].document_id == second.document.id
