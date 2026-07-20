"""Tests for atomic page status and direct-classification batch operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from src.batch_service import (
    BatchFailureCode,
    BatchOperationType,
    PageBatchService,
    _BatchRepository,
)
from src.database import Database
from src.models import PageStatus


def _library(
    tmp_path: Path,
    statuses: Sequence[PageStatus],
) -> tuple[Database, PageBatchService, int, list[int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="批量操作测试手册",
        filename="batch.pdf",
        source_path=tmp_path / "raw" / "batch.pdf",
        sha256="b" * 64,
    )
    page_ids = [
        database.create_page(
            document_id=document.id,
            page_number=index,
            image_path=tmp_path / "pages" / str(document.id) / f"page_{index:04d}.png",
            extracted_text=f"第 {index} 页原文",
            status=status,
            processing_error="保留的处理错误" if index == 1 else "",
        ).id
        for index, status in enumerate(statuses, start=1)
    ]
    return database, PageBatchService(database), document.id, page_ids


def _raw_page(database: Database, page_id: int) -> sqlite3.Row:
    with sqlite3.connect(database.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    assert row is not None
    return row


def _relation_pairs(database: Database, table: str) -> set[tuple[int, int]]:
    query = {
        "page_tags": "SELECT page_id, tag_id FROM page_tags",
        "project_pages": "SELECT page_id, project_id FROM project_pages",
    }[table]
    with sqlite3.connect(database.database_path) as connection:
        return {(int(row[0]), int(row[1])) for row in connection.execute(query)}


def _insert_many_pages(database: Database, document_id: int, count: int) -> list[int]:
    """Create many compact rows without N setup transactions obscuring query tests."""

    timestamp = "2026-07-20T10:00:00+00:00"
    with sqlite3.connect(database.database_path) as connection:
        start = int(connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM pages").fetchone()[0])
        rows = [
            (
                start + offset,
                document_id,
                offset + 1,
                f"page_{offset + 1:04d}.png",
                "短文本",
                "短 文本",
                timestamp,
                timestamp,
            )
            for offset in range(count)
        ]
        connection.executemany(
            """
            INSERT INTO pages(
                id, document_id, page_number, image_path, extracted_text,
                search_extracted_text, status, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'text_extracted', 'pending', ?, ?)
            """,
            rows,
        )
    return [start + offset for offset in range(count)]


@pytest.mark.parametrize(
    ("target", "sources", "expected_changed", "expected_unchanged"),
    [
        (PageStatus.REVIEWED, (PageStatus.PENDING, PageStatus.REVIEWED), 1, 1),
        (PageStatus.SKIPPED, (PageStatus.PENDING, PageStatus.SKIPPED), 1, 1),
        (
            PageStatus.PENDING,
            (PageStatus.REVIEWED, PageStatus.SKIPPED, PageStatus.PENDING),
            2,
            1,
        ),
    ],
)
def test_status_batch_applies_allowed_transitions_and_timestamp_semantics(
    tmp_path: Path,
    target: PageStatus,
    sources: tuple[PageStatus, ...],
    expected_changed: int,
    expected_unchanged: int,
) -> None:
    database, service, _, page_ids = _library(tmp_path, sources)
    unchanged_before = _raw_page(database, page_ids[-1])

    plan = service.plan_status(page_ids, target)
    assert plan.executable
    result = service.execute(plan)

    assert result.committed
    assert result.changed_count == expected_changed
    assert result.unchanged_count == expected_unchanged
    assert result.operation is BatchOperationType.SET_PAGE_STATUS
    assert result.target_status is target
    for page_id in result.affected_page_ids:
        row = _raw_page(database, page_id)
        assert row["review_status"] == target.value
        if target is PageStatus.REVIEWED:
            assert row["reviewed_at"] is not None
        else:
            assert row["reviewed_at"] is None
    unchanged_after = _raw_page(database, page_ids[-1])
    assert unchanged_after["updated_at"] == unchanged_before["updated_at"]
    assert unchanged_after["reviewed_at"] == unchanged_before["reviewed_at"]


def test_status_plan_normalizes_duplicates_and_replanned_repeat_is_idempotent(
    tmp_path: Path,
) -> None:
    database, service, _, page_ids = _library(
        tmp_path, (PageStatus.PENDING, PageStatus.PENDING)
    )
    plan = service.plan_status([page_ids[0], page_ids[0], page_ids[1]], "reviewed")
    assert plan.requested_count == 3
    assert plan.page_ids == tuple(page_ids)

    first = service.execute(plan)
    second = service.execute(service.plan_status(page_ids, PageStatus.REVIEWED))

    assert first.committed and first.changed_count == 2
    assert second.committed and second.changed_count == 0
    assert second.unchanged_count == 2
    assert all(_raw_page(database, page_id)["review_status"] == "reviewed" for page_id in page_ids)


@pytest.mark.parametrize("target", [PageStatus.DRAFT, PageStatus.FAILED, "unknown"])
def test_status_plan_rejects_invalid_targets_without_writes(
    tmp_path: Path, target: PageStatus | str
) -> None:
    database, service, _, page_ids = _library(tmp_path, (PageStatus.PENDING,))
    before = _raw_page(database, page_ids[0])

    plan = service.plan_status(page_ids, target)
    result = service.execute(plan)

    assert not plan.executable
    assert plan.invalid_reason
    assert not result.committed
    assert result.failure_code is BatchFailureCode.INVALID_REQUEST
    assert _raw_page(database, page_ids[0])["updated_at"] == before["updated_at"]


def test_status_plan_reports_empty_missing_draft_and_failed_pages(tmp_path: Path) -> None:
    _, service, _, page_ids = _library(
        tmp_path,
        (PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED),
    )
    empty = service.plan_status([], PageStatus.REVIEWED)
    blocked = service.plan_status([*page_ids, 999_999], PageStatus.REVIEWED)

    assert not empty.executable
    assert empty.invalid_reason == "页面 ID 不能为空。"
    assert not blocked.executable
    assert blocked.eligible_page_ids == (page_ids[0],)
    assert {item.page_id for item in blocked.protected_pages} == set(page_ids[1:])
    assert blocked.missing_page_ids == (999_999,)
    result = service.execute(blocked)
    assert not result.committed
    assert result.changed_count == 0


@pytest.mark.parametrize("invalid_id", [0, -1, True, "1"])
def test_status_plan_rejects_non_positive_or_non_integer_ids(
    tmp_path: Path, invalid_id: object
) -> None:
    _, service, _, page_ids = _library(tmp_path, (PageStatus.PENDING,))

    plan = service.plan_status([page_ids[0], invalid_id], PageStatus.REVIEWED)  # type: ignore[list-item]

    assert not plan.executable
    assert "正整数" in str(plan.invalid_reason)
    assert not service.execute(plan).committed


def test_status_execute_rejects_stale_snapshot_atomically(tmp_path: Path) -> None:
    database, service, _, page_ids = _library(
        tmp_path, (PageStatus.PENDING, PageStatus.PENDING)
    )
    plan = service.plan_status(page_ids, PageStatus.REVIEWED)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", page_ids[1]),
        )

    result = service.execute(plan)

    assert not result.committed
    assert result.failure_code is BatchFailureCode.STALE_CONFLICT
    assert result.stale_page_ids == (page_ids[1],)
    assert all(_raw_page(database, page_id)["review_status"] == "pending" for page_id in page_ids)


def test_status_sql_failure_rolls_back_all_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, document_id, _ = _library(tmp_path, ())
    page_ids = _insert_many_pages(database, document_id, 405)
    original = service._repository._write_status_chunk

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("simulated status batch failure")

    monkeypatch.setattr(service._repository, "_write_status_chunk", fail_after_write)
    result = service.execute(service.plan_status(page_ids, PageStatus.REVIEWED))

    assert not result.committed
    assert result.failure_code is BatchFailureCode.EXECUTION_FAILED
    assert result.changed_count == 0
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pages WHERE review_status = 'pending'"
        ).fetchone()[0] == 405


def test_status_batch_preserves_user_content_fts_note_error_and_classifications(
    tmp_path: Path,
) -> None:
    database, service, document_id, page_ids = _library(tmp_path, (PageStatus.PENDING,))
    page_id = page_ids[0]
    tag = database.create_tag("保留标签")
    project = database.create_project("保留项目")
    database.set_page_tags(page_id, [tag.id])
    database.set_page_projects(page_id, [project.id])
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            """
            UPDATE pages SET extracted_text = ?, ocr_text = ?, markdown_content = ?,
                search_extracted_text = ?, search_ocr_text = ?, search_markdown_content = ?,
                note_updated_at = ?, processing_error = ? WHERE id = ?
            """,
            (
                "原始长文本",
                "OCR 长文本",
                "# 人工笔记",
                "原始 长文本",
                "OCR 长文本",
                "人工 笔记",
                "2026-07-19T00:00:00+00:00",
                "必须保留的错误",
                page_id,
            ),
        )
    before = _raw_page(database, page_id)
    database.set_document_tags(document_id, [tag.id])
    database.set_document_projects(document_id, [project.id])

    result = service.execute(service.plan_status([page_id], PageStatus.REVIEWED))

    after = _raw_page(database, page_id)
    assert result.committed
    for field in (
        "extracted_text",
        "ocr_text",
        "markdown_content",
        "search_extracted_text",
        "search_ocr_text",
        "search_markdown_content",
        "note_updated_at",
        "processing_error",
    ):
        assert after[field] == before[field]
    assert _relation_pairs(database, "page_tags") == {(page_id, tag.id)}
    assert _relation_pairs(database, "project_pages") == {(page_id, project.id)}


def test_tag_batch_add_remove_is_additive_idempotent_and_direct_only(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(
        tmp_path, (PageStatus.PENDING, PageStatus.PENDING, PageStatus.PENDING)
    )
    first = database.create_tag("标签 A")
    second = database.create_tag("标签 B")
    database.set_page_tags(page_ids[0], [first.id])
    database.set_document_tags(document_id, [second.id])

    add_plan = service.plan_add_tags(page_ids[:2], [first.id, second.id])
    added = service.execute(add_plan)
    repeated = service.execute(service.plan_add_tags(page_ids[:2], [first.id, second.id]))
    removed = service.execute(service.plan_remove_tags(page_ids, [first.id]))

    assert add_plan.requested_relation_count == 4
    assert added.committed and added.changed_count == 3 and added.unchanged_count == 1
    assert repeated.committed and repeated.changed_count == 0 and repeated.unchanged_count == 4
    assert removed.committed and removed.changed_count == 2 and removed.unchanged_count == 1
    assert _relation_pairs(database, "page_tags") == {
        (page_ids[0], second.id),
        (page_ids[1], second.id),
    }
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ? AND tag_id = ?",
            (document_id, second.id),
        ).fetchone()[0] == 1


def test_project_batch_add_remove_is_additive_idempotent_and_direct_only(
    tmp_path: Path,
) -> None:
    database, service, document_id, page_ids = _library(
        tmp_path, (PageStatus.PENDING, PageStatus.PENDING, PageStatus.PENDING)
    )
    first = database.create_project("项目 A")
    second = database.create_project("项目 B")
    database.set_page_projects(page_ids[0], [first.id])
    database.set_document_projects(document_id, [second.id])

    added = service.execute(service.plan_add_projects(page_ids[:2], [first.id, second.id]))
    repeated = service.execute(
        service.plan_add_projects(page_ids[:2], [first.id, second.id])
    )
    removed = service.execute(service.plan_remove_projects(page_ids, [first.id]))

    assert added.committed and added.changed_count == 3 and added.unchanged_count == 1
    assert repeated.committed and repeated.changed_count == 0 and repeated.unchanged_count == 4
    assert removed.committed and removed.changed_count == 2 and removed.unchanged_count == 1
    assert _relation_pairs(database, "project_pages") == {
        (page_ids[0], second.id),
        (page_ids[1], second.id),
    }
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM project_documents
            WHERE document_id = ? AND project_id = ?""",
            (document_id, second.id),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["tag", "project"])
def test_relation_batch_deduplicates_ids_and_does_not_touch_page_updated_at(
    tmp_path: Path, kind: str
) -> None:
    database, service, _, page_ids = _library(tmp_path, (PageStatus.PENDING,))
    page_id = page_ids[0]
    before = _raw_page(database, page_id)["updated_at"]
    if kind == "tag":
        target_id = database.create_tag("去重标签").id
        plan = service.plan_add_tags([page_id, page_id], [target_id, target_id])
    else:
        target_id = database.create_project("去重项目").id
        plan = service.plan_add_projects([page_id, page_id], [target_id, target_id])

    result = service.execute(plan)

    assert plan.requested_count == 2
    assert plan.page_ids == (page_id,)
    assert plan.target_ids == (target_id,)
    assert result.committed and result.changed_count == 1
    assert _raw_page(database, page_id)["updated_at"] == before


@pytest.mark.parametrize("kind", ["tag", "project"])
def test_relation_plans_reject_empty_missing_pages_and_targets(
    tmp_path: Path, kind: str
) -> None:
    database, service, _, page_ids = _library(tmp_path, (PageStatus.PENDING,))
    if kind == "tag":
        target_id = database.create_tag("存在标签").id
        planner: Callable[[Sequence[int], Sequence[int]], object] = service.plan_add_tags
    else:
        target_id = database.create_project("存在项目").id
        planner = service.plan_add_projects

    empty_pages = planner([], [target_id])
    empty_targets = planner(page_ids, [])
    missing_page = planner([*page_ids, 999_998], [target_id])
    missing_target = planner(page_ids, [target_id, 999_999])

    assert not empty_pages.executable and empty_pages.invalid_reason
    assert not empty_targets.executable and empty_targets.invalid_reason
    assert missing_page.missing_page_ids == (999_998,) and not missing_page.executable
    assert missing_target.missing_target_ids == (999_999,) and not missing_target.executable
    assert not service.execute(missing_page).committed
    assert not service.execute(missing_target).committed


@pytest.mark.parametrize("kind", ["tag", "project"])
def test_relation_sql_failure_rolls_back_all_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    database, service, document_id, _ = _library(tmp_path, ())
    page_ids = _insert_many_pages(database, document_id, 405)
    if kind == "tag":
        target_id = database.create_tag("回滚标签").id
        plan = service.plan_add_tags(page_ids, [target_id])
        table = "page_tags"
    else:
        target_id = database.create_project("回滚项目").id
        plan = service.plan_add_projects(page_ids, [target_id])
        table = "project_pages"
    original = service._repository._write_relation_chunk

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("simulated relation batch failure")

    monkeypatch.setattr(service._repository, "_write_relation_chunk", fail_after_write)
    result = service.execute(plan)

    assert not result.committed
    assert result.changed_count == 0
    assert _relation_pairs(database, table) == set()


def test_relation_execute_rejects_stale_page_and_direct_relation(tmp_path: Path) -> None:
    database, service, _, page_ids = _library(
        tmp_path, (PageStatus.PENDING, PageStatus.PENDING)
    )
    tag = database.create_tag("并发标签")
    stale_page_plan = service.plan_add_tags(page_ids, [tag.id])
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", page_ids[0]),
        )

    stale_page = service.execute(stale_page_plan)
    relation_plan = service.plan_add_tags(page_ids, [tag.id])
    database.set_page_tags(page_ids[1], [tag.id])
    stale_relation = service.execute(relation_plan)

    assert stale_page.failure_code is BatchFailureCode.STALE_CONFLICT
    assert stale_page.stale_page_ids == (page_ids[0],)
    assert stale_relation.failure_code is BatchFailureCode.STALE_CONFLICT
    assert stale_relation.stale_page_ids == (page_ids[1],)
    assert _relation_pairs(database, "page_tags") == {(page_ids[1], tag.id)}


@pytest.mark.parametrize("kind", ["tag", "project"])
def test_relation_batch_chunks_more_than_400_page_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    database, service, document_id, _ = _library(tmp_path, ())
    page_ids = _insert_many_pages(database, document_id, 405)
    if kind == "tag":
        target_id = database.create_tag("分块标签").id
        table = "page_tags"
    else:
        target_id = database.create_project("分块项目").id
        table = "project_pages"

    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("src.batch_service.sqlite3.connect", traced_connect)
    if kind == "tag":
        plan = service.plan_add_tags(page_ids, [target_id])
    else:
        plan = service.plan_add_projects(page_ids, [target_id])

    result = service.execute(plan)
    execution_statements = tuple(statements)

    assert result.committed and result.changed_count == 405
    assert len(_relation_pairs(database, table)) == 405
    selects = [
        statement.upper()
        for statement in execution_statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len([statement for statement in selects if "FROM PAGES" in statement]) == 4
    assert len([statement for statement in selects if f"FROM {table.upper()}" in statement]) == 4
    assert all("MARKDOWN_CONTENT" not in statement for statement in selects)


def test_status_queries_are_chunked_and_do_not_load_long_page_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, document_id, _ = _library(tmp_path, ())
    page_ids = _insert_many_pages(database, document_id, 805)
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("src.batch_service.sqlite3.connect", traced_connect)
    result = service.execute(service.plan_status(page_ids, PageStatus.REVIEWED))

    page_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FROM PAGES" in statement.upper()
    ]
    assert result.committed and result.changed_count == 805
    assert len(page_selects) == 6
    assert all("MARKDOWN_CONTENT" not in statement.upper() for statement in page_selects)
    assert all("EXTRACTED_TEXT" not in statement.upper() for statement in page_selects)
    assert all("OCR_TEXT" not in statement.upper() for statement in page_selects)


def test_repository_write_transactions_start_immediate() -> None:
    assert _BatchRepository.WRITE_BEGIN_SQL == "BEGIN IMMEDIATE"
