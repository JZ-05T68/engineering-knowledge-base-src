"""Tests for the read-only cross-document aggregation data layer.

Fixtures build real files and real schema v6 rows under ``tmp_path`` only.
Production data and port 8501 are never touched. Deletion-consistency tests
always delete through DocumentDeletionService, never raw SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.aggregation_service import (
    AggregationError,
    AggregationService,
)
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.evidence_basket_service import EvidenceBasketService
from src.models import AggregationSourceKind, NoteImportance, NoteType
from src.note_service import NoteService


def _make_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    return database, data_dir, raw_dir, pages_dir, markdown_dir


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


def _identities(result) -> set[tuple[str, int]]:
    return {(item.source_kind.value, item.source_id) for item in result.items}


def _build_library(tmp_path: Path):
    """Two documents with notes, evidence, tags and projects on both levels."""

    database, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    notes = NoteService(database)
    baskets = EvidenceBasketService(database)

    alpha, alpha_pages = _create_document(
        database, raw_dir, pages_dir, title="甲文档", sha_letter="a", page_count=2
    )
    beta, beta_pages = _create_document(
        database, raw_dir, pages_dir, title="乙文档", sha_letter="b"
    )

    tag_pump = database.create_tag("泵")
    tag_valve = database.create_tag("阀")
    project_main = database.create_project("主项目")
    database.set_document_tags(alpha.id, [tag_pump.id])
    database.set_page_tags(alpha_pages[0].id, [tag_valve.id])
    database.set_document_projects(alpha.id, [project_main.id])
    database.set_page_projects(beta_pages[0].id, [project_main.id])

    notes.create_document_note(alpha.id, "甲文档级笔记", importance="primary")
    notes.create_page_note(alpha_pages[0].id, "甲页面笔记", importance="secondary")
    notes.create_text_selection_note(alpha_pages[0].id, "阀体", "甲选区笔记")
    notes.create_image_region_note(alpha_pages[0].id, 10, 20, 300, 400, "甲区域笔记")
    notes.create_page_note(beta_pages[0].id, "乙页面笔记", importance="primary")
    baskets.add_item(
        document_id=alpha.id,
        page_id=alpha_pages[0].id,
        evidence_text="阀体",
        user_note="关键参数出处",
    )
    return {
        "database": database,
        "service": AggregationService(database),
        "notes": notes,
        "data_dir": data_dir,
        "raw_dir": raw_dir,
        "pages_dir": pages_dir,
        "markdown_dir": markdown_dir,
        "alpha": alpha,
        "alpha_pages": alpha_pages,
        "beta": beta,
        "beta_pages": beta_pages,
        "tag_pump": tag_pump,
        "tag_valve": tag_valve,
        "project_main": project_main,
    }


# --- basics ----------------------------------------------------------------------


def test_empty_library_returns_empty_result(tmp_path: Path) -> None:
    database, *_ = _make_env(tmp_path)
    result = AggregationService(database).aggregate_library()
    assert result.items == ()
    assert (result.total_count, result.note_count, result.evidence_count) == (0, 0, 0)


def test_single_document_single_note(tmp_path: Path) -> None:
    database, _, raw_dir, pages_dir, _ = _make_env(tmp_path)
    document, _ = _create_document(
        database, raw_dir, pages_dir, title="独文档", sha_letter="c"
    )
    NoteService(database).create_document_note(document.id, "唯一笔记")

    result = AggregationService(database).aggregate_library()

    assert result.total_count == 1
    item = result.items[0]
    assert item.source_kind is AggregationSourceKind.NOTE
    assert item.note_type is NoteType.DOCUMENT
    assert item.document_title == "独文档"
    assert item.page_id is None and item.page_number is None
    assert item.importance is NoteImportance.NORMAL
    assert item.content == "唯一笔记"


def test_library_aggregates_across_documents(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_library()

    assert result.total_count == 6  # 5 notes + 1 evidence
    assert result.note_count == 5
    assert result.evidence_count == 1
    document_titles = {item.document_title for item in result.items}
    assert document_titles == {"甲文档", "乙文档"}


def test_all_four_note_types_keep_their_identity(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_library()

    note_types = {
        item.note_type for item in result.items if item.source_kind is AggregationSourceKind.NOTE
    }
    assert note_types == {
        NoteType.DOCUMENT,
        NoteType.PAGE,
        NoteType.TEXT_SELECTION,
        NoteType.IMAGE_REGION,
    }
    selection = next(
        item for item in result.items if item.note_type is NoteType.TEXT_SELECTION
    )
    assert selection.page_number == 1
    region = next(
        item for item in result.items if item.note_type is NoteType.IMAGE_REGION
    )
    assert region.page_id == env["alpha_pages"][0].id


# --- project and tag axes ----------------------------------------------------------


def test_project_aggregation_uses_effective_membership(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_by_project(env["project_main"].id)

    identities = _identities(result)
    assert len(identities) == len(result.items)  # no duplicates
    # 甲文档 is in the project directly: all four of its notes plus its
    # evidence (page inherits membership from the document) are included.
    alpha_items = [
        item for item in result.items if item.document_id == env["alpha"].id
    ]
    assert len(alpha_items) == 5
    # 乙文档 is only linked at page level: its page note is included.
    beta_items = [item for item in result.items if item.document_id == env["beta"].id]
    assert len(beta_items) == 1
    assert beta_items[0].note_type is NoteType.PAGE


def test_document_axis_association_does_not_leak_upwards(tmp_path: Path) -> None:
    """A page-level association must not pull in the document-level note."""

    database, _, raw_dir, pages_dir, _ = _make_env(tmp_path)
    document, pages = _create_document(
        database, raw_dir, pages_dir, title="隔离", sha_letter="d"
    )
    tag = database.create_tag("只标页")
    database.set_page_tags(pages[0].id, [tag.id])
    notes = NoteService(database)
    notes.create_document_note(document.id, "文档级笔记不应命中")
    notes.create_page_note(pages[0].id, "页面笔记应命中")

    result = AggregationService(database).aggregate_by_tag(tag.id)

    assert result.total_count == 1
    assert result.items[0].content == "页面笔记应命中"


def test_tag_aggregation_combines_document_and_page_levels(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    pump = env["service"].aggregate_by_tag(env["tag_pump"].id)
    # 泵 is only on 甲文档 (document level): its pages inherit, so all five
    # entries of that document match — the search layer's shipped semantics.
    assert pump.total_count == 5

    valve = env["service"].aggregate_by_tag(env["tag_valve"].id)
    # 阀 is only on 甲文档 page 1: the three page-anchored notes and the
    # evidence item match, the document-level note does not.
    assert valve.total_count == 4
    assert all(
        item.note_type is not NoteType.DOCUMENT for item in valve.items
    )


def test_missing_axis_raises(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    with pytest.raises(AggregationError, match="找不到项目"):
        env["service"].aggregate_by_project(9999)
    with pytest.raises(AggregationError, match="找不到标签"):
        env["service"].aggregate_by_tag(9999)
    with pytest.raises(AggregationError, match="正整数"):
        env["service"].aggregate_by_tag(-1)


def test_empty_axis_returns_empty_result(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    lonely = env["database"].create_project("空项目")
    result = env["service"].aggregate_by_project(lonely.id)
    assert result.items == ()
    assert result.total_count == 0


# --- importance and note-type filters ----------------------------------------------


def test_importance_filters_notes_only(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    service = env["service"]

    primary = service.aggregate_library(importance="primary")
    assert {item.content for item in primary.items} == {"甲文档级笔记", "乙页面笔记"}
    assert primary.evidence_count == 0

    secondary = service.aggregate_library(importance=NoteImportance.SECONDARY)
    assert {item.content for item in secondary.items} == {"甲页面笔记"}

    normal = service.aggregate_library(importance="normal")
    assert all(
        item.importance is NoteImportance.NORMAL for item in normal.items
    )
    assert normal.evidence_count == 0

    everything = service.aggregate_library(importance=None)
    assert everything.evidence_count == 1


def test_importance_filter_inside_project_axis(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_by_project(
        env["project_main"].id, importance="primary"
    )
    assert {item.content for item in result.items} == {"甲文档级笔记", "乙页面笔记"}


def test_note_type_filter_excludes_evidence(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_library(note_type="text_selection")
    assert result.total_count == 1
    assert result.items[0].note_type is NoteType.TEXT_SELECTION


def test_invalid_filter_values_raise(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    with pytest.raises(AggregationError, match="未知重要性等级"):
        env["service"].aggregate_library(importance="critical")
    with pytest.raises(AggregationError, match="未知笔记类型"):
        env["service"].aggregate_library(note_type="voice")
    with pytest.raises(AggregationError, match="limit"):
        env["service"].aggregate_library(limit=0)
    with pytest.raises(AggregationError, match="offset"):
        env["service"].aggregate_library(offset=-1)


def test_evidence_keeps_user_note_and_basket(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_library()
    evidence = next(
        item
        for item in result.items
        if item.source_kind is AggregationSourceKind.EVIDENCE
    )
    assert evidence.content == "阀体"
    assert evidence.user_note == "关键参数出处"
    assert evidence.basket_id is not None
    assert evidence.importance is None
    assert evidence.note_type is None


# --- dedup, sorting, pagination ------------------------------------------------------


def test_multi_path_membership_deduplicates(tmp_path: Path) -> None:
    """Document and page both in the project: every entry appears once."""

    env = _build_library(tmp_path)
    database = env["database"]
    database.set_page_projects(
        env["alpha_pages"][0].id, [env["project_main"].id]
    )
    result = env["service"].aggregate_by_project(env["project_main"].id)

    identities = [
        (item.source_kind.value, item.source_id) for item in result.items
    ]
    assert len(identities) == len(set(identities))


def test_multi_tag_document_and_multi_project_page(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    database = env["database"]
    extra_tag = database.create_tag("辅标签")
    database.set_document_tags(env["alpha"].id, [env["tag_pump"].id, extra_tag.id])
    extra_project = database.create_project("辅项目")
    database.set_page_projects(
        env["beta_pages"][0].id, [env["project_main"].id, extra_project.id]
    )

    service = env["service"]
    assert service.aggregate_by_tag(extra_tag.id).total_count == 5
    assert service.aggregate_by_project(extra_project.id).total_count == 1

    result = service.aggregate_library()
    alpha_note = next(
        item for item in result.items if item.content == "甲文档级笔记"
    )
    assert alpha_note.tags == ("泵", "辅标签")
    beta_note = next(item for item in result.items if item.content == "乙页面笔记")
    assert beta_note.projects == ("主项目", "辅项目")


def test_stable_ordering_importance_then_time(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    result = env["service"].aggregate_library(limit=50)
    rank_of = {"primary": 0, "secondary": 1, "normal": 2, None: 3}
    ranks = [
        rank_of[item.importance.value if item.importance else None]
        for item in result.items
    ]
    assert ranks == sorted(ranks)
    # Within one importance level, newer entries come first.
    primaries = [
        item for item in result.items if item.importance is NoteImportance.PRIMARY
    ]
    assert [item.content for item in primaries] == ["乙页面笔记", "甲文档级笔记"]
    # Evidence always sorts after notes.
    assert result.items[-1].source_kind is AggregationSourceKind.EVIDENCE


def test_pagination_pages_and_totals(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    service = env["service"]
    first = service.aggregate_library(limit=2, offset=0)
    second = service.aggregate_library(limit=2, offset=2)
    third = service.aggregate_library(limit=2, offset=4)

    assert first.total_count == 6 and second.total_count == 6
    assert len(first.items) == 2 and len(second.items) == 2 and len(third.items) == 2
    assert _identities(first).isdisjoint(_identities(second))
    assert _identities(first).isdisjoint(_identities(third))
    assert len(_identities(first) | _identities(second) | _identities(third)) == 6


def test_larger_library_aggregates_correctly(tmp_path: Path) -> None:
    database, _, raw_dir, pages_dir, _ = _make_env(tmp_path)
    notes = NoteService(database)
    tag = database.create_tag("批量")
    project = database.create_project("批量项目")
    for index in range(10):
        document, pages = _create_document(
            database,
            raw_dir,
            pages_dir,
            title=f"批量{index:02d}",
            sha_letter=f"{index:x}",
            page_count=2,
        )
        database.set_document_tags(document.id, [tag.id])
        database.set_document_projects(document.id, [project.id])
        for page in pages:
            notes.create_page_note(
                page.id,
                f"笔记{index}-{page.page_number}",
                importance="primary" if index % 2 == 0 else "normal",
            )
            notes.create_text_selection_note(
                page.id, "阀体", f"选区{index}-{page.page_number}"
            )
    service = AggregationService(database)

    everything = service.aggregate_library(limit=500)
    assert everything.total_count == 40
    assert len(_identities(everything)) == 40
    by_tag = service.aggregate_by_tag(tag.id, limit=500)
    assert by_tag.total_count == 40
    by_project = service.aggregate_by_project(
        project.id, importance="primary", limit=500
    )
    assert by_project.total_count == 10


# --- deletion consistency and read-only discipline -----------------------------------


def test_deleted_document_disappears_from_aggregation(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    service = env["service"]
    before = service.aggregate_library()
    assert any(item.document_id == env["alpha"].id for item in before.items)

    deletion_service = DocumentDeletionService(
        database=env["database"],
        raw_dir=env["raw_dir"],
        pages_dir=env["pages_dir"],
        markdown_dir=env["markdown_dir"],
        data_dir=env["data_dir"],
    )
    deletion_service.delete_document(env["alpha"].id)

    after = service.aggregate_library()
    assert all(item.document_id != env["alpha"].id for item in after.items)
    assert after.total_count == 1  # only 乙页面笔记 remains
    assert after.items[0].document_id == env["beta"].id

    by_tag = service.aggregate_by_tag(env["tag_pump"].id)
    assert by_tag.total_count == 0  # 泵 only existed on the deleted document
    by_project = service.aggregate_by_project(env["project_main"].id)
    assert {item.document_id for item in by_project.items} == {env["beta"].id}


def test_missing_source_file_does_not_break_aggregation(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    Path(env["alpha"].source_path).unlink()
    env["alpha_pages"][0].image_path.unlink()

    result = env["service"].aggregate_library()
    assert result.total_count == 6


def test_aggregation_never_writes_to_the_database(tmp_path: Path) -> None:
    env = _build_library(tmp_path)
    database = env["database"]

    def snapshot() -> tuple[list[str], int, int, int]:
        with sqlite3.connect(database.database_path) as connection:
            tables = sorted(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            )
            notes = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            evidence = connection.execute(
                "SELECT COUNT(*) FROM evidence_items"
            ).fetchone()[0]
            documents = connection.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
        return tables, notes, evidence, documents

    before = snapshot()
    service = env["service"]
    service.aggregate_library()
    service.aggregate_by_project(env["project_main"].id)
    service.aggregate_by_tag(env["tag_pump"].id)
    service.aggregate_library(importance="primary", limit=10, offset=1)
    assert snapshot() == before
