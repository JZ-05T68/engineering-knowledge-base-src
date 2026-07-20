"""Tests for batch UI plan dispatch, summaries, and atomic feedback."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.batch_service import (
    BatchFailureCode,
    BatchOperationPlan,
    BatchOperationResult,
    BatchOperationType,
    PageRelation,
    ProtectedPage,
)
from src.batch_ui import (
    BatchUiError,
    create_batch_plan,
    result_feedback,
    summarize_batch_plan,
)
from src.models import PageStatus


class RecordingBatchService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...], object]] = []

    def _plan(self, name: str, page_ids, target, operation):
        pages = tuple(page_ids)
        self.calls.append((name, pages, target))
        return BatchOperationPlan(
            operation=operation,
            requested_count=len(pages),
            page_ids=pages,
            target_ids=tuple(target) if not isinstance(target, PageStatus) else (),
            target_status=target if isinstance(target, PageStatus) else None,
            eligible_page_ids=pages,
            executable=True,
        )

    def plan_status(self, page_ids, target):
        return self._plan("status", page_ids, target, BatchOperationType.SET_PAGE_STATUS)

    def plan_add_tags(self, page_ids, targets):
        return self._plan("add_tags", page_ids, targets, BatchOperationType.ADD_PAGE_TAGS)

    def plan_remove_tags(self, page_ids, targets):
        return self._plan(
            "remove_tags", page_ids, targets, BatchOperationType.REMOVE_PAGE_TAGS
        )

    def plan_add_projects(self, page_ids, targets):
        return self._plan(
            "add_projects", page_ids, targets, BatchOperationType.ADD_PAGE_PROJECTS
        )

    def plan_remove_projects(self, page_ids, targets):
        return self._plan(
            "remove_projects", page_ids, targets, BatchOperationType.REMOVE_PAGE_PROJECTS
        )


@pytest.mark.parametrize(
    ("operation", "target_status", "target_ids", "expected_call"),
    [
        (BatchOperationType.SET_PAGE_STATUS, PageStatus.REVIEWED, (), "status"),
        (BatchOperationType.ADD_PAGE_TAGS, None, (3, 4), "add_tags"),
        (BatchOperationType.REMOVE_PAGE_TAGS, None, (3, 4), "remove_tags"),
        (BatchOperationType.ADD_PAGE_PROJECTS, None, (5,), "add_projects"),
        (BatchOperationType.REMOVE_PAGE_PROJECTS, None, (5,), "remove_projects"),
    ],
)
def test_create_batch_plan_dispatches_exactly_once(
    operation: BatchOperationType,
    target_status: PageStatus | None,
    target_ids: tuple[int, ...],
    expected_call: str,
) -> None:
    service = RecordingBatchService()

    plan = create_batch_plan(
        service,  # type: ignore[arg-type]
        (11, 12),
        operation,
        target_status=target_status,
        target_ids=target_ids,
    )

    assert plan.operation is operation
    assert len(service.calls) == 1
    assert service.calls[0][0] == expected_call


@pytest.mark.parametrize(
    ("operation", "target_status", "target_ids"),
    [
        (BatchOperationType.SET_PAGE_STATUS, None, ()),
        (BatchOperationType.ADD_PAGE_TAGS, None, ()),
        (BatchOperationType.REMOVE_PAGE_PROJECTS, None, ()),
    ],
)
def test_create_batch_plan_requires_an_explicit_target(
    operation: BatchOperationType,
    target_status: PageStatus | None,
    target_ids: tuple[int, ...],
) -> None:
    with pytest.raises(BatchUiError):
        create_batch_plan(
            RecordingBatchService(),  # type: ignore[arg-type]
            (11,),
            operation,
            target_status=target_status,
            target_ids=target_ids,
        )


def test_status_plan_summary_reports_protected_and_missing_pages() -> None:
    plan = BatchOperationPlan(
        operation=BatchOperationType.SET_PAGE_STATUS,
        requested_count=5,
        page_ids=(1, 2, 3, 4, 999),
        target_status=PageStatus.REVIEWED,
        eligible_page_ids=(1,),
        unchanged_page_ids=(2,),
        protected_pages=(
            ProtectedPage(3, PageStatus.DRAFT, "草稿受保护"),
            ProtectedPage(4, PageStatus.FAILED, "失败受保护"),
        ),
        missing_page_ids=(999,),
        executable=False,
    )

    summary = summarize_batch_plan(plan)

    assert summary.requested_pages == 5
    assert summary.changeable_pages == 1
    assert summary.unchanged_pages == 1
    assert summary.protected_pages == 2
    assert summary.draft_pages == 1
    assert summary.failed_pages == 1
    assert summary.missing_or_invalid == 1
    assert not summary.executable


def test_relation_plan_summary_distinguishes_pages_and_relationship_rows() -> None:
    plan = BatchOperationPlan(
        operation=BatchOperationType.ADD_PAGE_TAGS,
        requested_count=2,
        page_ids=(1, 2),
        target_ids=(7, 8),
        eligible_page_ids=(1, 2),
        existing_relations=(PageRelation(1, 7),),
        eligible_relation_count=3,
        unchanged_relation_count=1,
        executable=True,
    )

    summary = summarize_batch_plan(plan)

    assert summary.requested_pages == 2
    assert summary.changeable_pages == 2
    assert summary.changeable_relations == 3
    assert summary.unchanged_relations == 1


def test_result_feedback_never_describes_atomic_failure_as_partial_success() -> None:
    stale = BatchOperationResult(
        requested_count=3,
        changed_count=0,
        unchanged_count=0,
        affected_page_ids=(),
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_ids=(),
        target_status=PageStatus.REVIEWED,
        committed=False,
        failure_code=BatchFailureCode.STALE_CONFLICT,
        failure_reason="stale",
    )
    failed = replace(
        stale,
        failure_code=BatchFailureCode.EXECUTION_FAILED,
        failure_reason="sql",
    )

    assert "本次操作已取消且未修改任何页面" in result_feedback(stale).message
    assert "所有修改均已回滚" in result_feedback(failed).message
    assert "成功" not in result_feedback(stale).message
    assert "成功" not in result_feedback(failed).message


def test_result_feedback_handles_success_and_all_unchanged() -> None:
    changed = BatchOperationResult(
        requested_count=3,
        changed_count=2,
        unchanged_count=1,
        affected_page_ids=(1, 2),
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_ids=(),
        target_status=PageStatus.REVIEWED,
        committed=True,
    )
    unchanged = replace(
        changed,
        requested_count=3,
        changed_count=0,
        unchanged_count=3,
        affected_page_ids=(),
    )

    assert "修改 2 页" in result_feedback(changed).message
    assert "1 页原本已满足目标" in result_feedback(changed).message
    assert "没有执行数据库写入" in result_feedback(unchanged).message
