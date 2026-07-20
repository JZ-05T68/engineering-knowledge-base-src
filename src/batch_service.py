"""Atomic page-status and direct-classification batch operations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from src.models import PageStatus

LOGGER = logging.getLogger(__name__)
MAX_SQL_PARAMETERS: Final[int] = 400
_RELATION_TARGET_CHUNK_SIZE: Final[int] = 100


class BatchDatabase(Protocol):
    """Minimum database handle required by :class:`PageBatchService`."""

    database_path: Path


class BatchOperationType(StrEnum):
    """Whitelisted batch mutations supported by the stage-one data layer."""

    SET_PAGE_STATUS = "set_page_status"
    ADD_PAGE_TAGS = "add_page_tags"
    REMOVE_PAGE_TAGS = "remove_page_tags"
    ADD_PAGE_PROJECTS = "add_page_projects"
    REMOVE_PAGE_PROJECTS = "remove_page_projects"


class BatchFailureCode(StrEnum):
    """Stable reason codes for a batch result that was not committed."""

    INVALID_REQUEST = "invalid_request"
    STALE_CONFLICT = "stale_conflict"
    EXECUTION_FAILED = "execution_failed"


class BatchOperationError(RuntimeError):
    """Base exception for an internal safe-batch failure."""


class BatchValidationError(BatchOperationError):
    """Raised when an operation or plan is not supported by this service."""


class BatchConflictError(BatchOperationError):
    """Raised inside a transaction when a preflight snapshot is stale."""

    def __init__(self, message: str, stale_page_ids: Sequence[int] = ()) -> None:
        super().__init__(message)
        self.stale_page_ids = tuple(stale_page_ids)


class BatchExecutionError(BatchOperationError):
    """Raised when SQLite reports an unexpected mutation count."""


@dataclass(frozen=True, slots=True)
class PageBatchSnapshot:
    """Small concurrency snapshot that deliberately excludes page content."""

    page_id: int
    status: PageStatus
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProtectedPage:
    """Page that cannot take the requested status transition by default."""

    page_id: int
    status: PageStatus
    reason: str


@dataclass(frozen=True, slots=True)
class PageRelation:
    """One direct page/tag or page/project relationship."""

    page_id: int
    target_id: int


@dataclass(frozen=True, slots=True)
class BatchOperationPlan:
    """Immutable, non-writing preflight result for one batch request."""

    operation: BatchOperationType
    requested_count: int
    page_ids: tuple[int, ...] = ()
    target_ids: tuple[int, ...] = ()
    target_status: PageStatus | None = None
    eligible_page_ids: tuple[int, ...] = ()
    unchanged_page_ids: tuple[int, ...] = ()
    existing_relations: tuple[PageRelation, ...] = ()
    eligible_relation_count: int = 0
    unchanged_relation_count: int = 0
    protected_pages: tuple[ProtectedPage, ...] = ()
    missing_page_ids: tuple[int, ...] = ()
    missing_target_ids: tuple[int, ...] = ()
    snapshots: tuple[PageBatchSnapshot, ...] = ()
    invalid_target: str | None = None
    invalid_reason: str | None = None
    executable: bool = False

    @property
    def requested_relation_count(self) -> int:
        """Return requested direct-relation rows after stable ID deduplication."""

        return len(self.page_ids) * len(self.target_ids)


@dataclass(frozen=True, slots=True)
class BatchOperationResult:
    """Typed outcome; failed transactions always report zero changed rows."""

    requested_count: int
    changed_count: int
    unchanged_count: int
    affected_page_ids: tuple[int, ...]
    operation: BatchOperationType
    target_ids: tuple[int, ...]
    target_status: PageStatus | None
    committed: bool
    requested_relation_count: int = 0
    failure_code: BatchFailureCode | None = None
    failure_reason: str | None = None
    stale_page_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _RelationSpec:
    table: str
    page_column: str
    target_column: str
    target_table: str
    target_label: str


_TAG_SPEC: Final[_RelationSpec] = _RelationSpec(
    table="page_tags",
    page_column="page_id",
    target_column="tag_id",
    target_table="tags",
    target_label="标签",
)
_PROJECT_SPEC: Final[_RelationSpec] = _RelationSpec(
    table="project_pages",
    page_column="page_id",
    target_column="project_id",
    target_table="projects",
    target_label="项目",
)
_RELATION_SPECS: Final[dict[BatchOperationType, _RelationSpec]] = {
    BatchOperationType.ADD_PAGE_TAGS: _TAG_SPEC,
    BatchOperationType.REMOVE_PAGE_TAGS: _TAG_SPEC,
    BatchOperationType.ADD_PAGE_PROJECTS: _PROJECT_SPEC,
    BatchOperationType.REMOVE_PAGE_PROJECTS: _PROJECT_SPEC,
}
_ADD_OPERATIONS: Final[set[BatchOperationType]] = {
    BatchOperationType.ADD_PAGE_TAGS,
    BatchOperationType.ADD_PAGE_PROJECTS,
}
_ALLOWED_STATUS_SOURCES: Final[dict[PageStatus, set[PageStatus]]] = {
    PageStatus.REVIEWED: {PageStatus.PENDING},
    PageStatus.SKIPPED: {PageStatus.PENDING},
    PageStatus.PENDING: {PageStatus.REVIEWED, PageStatus.SKIPPED},
}


class PageBatchService:
    """Plan and atomically execute safe page batch operations on schema v4."""

    def __init__(self, database: BatchDatabase) -> None:
        self._repository = _BatchRepository(database.database_path)

    def plan_status(
        self,
        page_ids: Sequence[int],
        target_status: PageStatus | str,
    ) -> BatchOperationPlan:
        """Preflight one manual status request without modifying SQLite."""

        requested_count, normalized_ids, id_error = _normalize_ids(page_ids, "页面")
        normalized_target, target_error = _normalize_status_target(target_status)
        invalid_reason = id_error or target_error
        if not normalized_ids and invalid_reason is None:
            invalid_reason = "页面 ID 不能为空。"
        if invalid_reason is not None or normalized_target is None:
            return BatchOperationPlan(
                operation=BatchOperationType.SET_PAGE_STATUS,
                requested_count=requested_count,
                page_ids=normalized_ids,
                target_status=normalized_target,
                invalid_target=str(target_status) if target_error is not None else None,
                invalid_reason=invalid_reason,
            )

        snapshots = self._repository.read_page_snapshots(normalized_ids)
        by_id = {snapshot.page_id: snapshot for snapshot in snapshots}
        missing = tuple(page_id for page_id in normalized_ids if page_id not in by_id)
        eligible: list[int] = []
        unchanged: list[int] = []
        protected: list[ProtectedPage] = []
        allowed_sources = _ALLOWED_STATUS_SOURCES[normalized_target]
        for page_id in normalized_ids:
            snapshot = by_id.get(page_id)
            if snapshot is None:
                continue
            if snapshot.status is normalized_target:
                unchanged.append(page_id)
            elif snapshot.status in allowed_sources:
                eligible.append(page_id)
            else:
                protected.append(
                    ProtectedPage(
                        page_id=page_id,
                        status=snapshot.status,
                        reason=_protected_status_reason(snapshot.status, normalized_target),
                    )
                )
        executable = not missing and not protected
        return BatchOperationPlan(
            operation=BatchOperationType.SET_PAGE_STATUS,
            requested_count=requested_count,
            page_ids=normalized_ids,
            target_status=normalized_target,
            eligible_page_ids=tuple(eligible),
            unchanged_page_ids=tuple(unchanged),
            protected_pages=tuple(protected),
            missing_page_ids=missing,
            snapshots=tuple(by_id[page_id] for page_id in normalized_ids if page_id in by_id),
            executable=executable,
        )

    def plan_add_tags(
        self, page_ids: Sequence[int], tag_ids: Sequence[int]
    ) -> BatchOperationPlan:
        """Preflight additive direct page/tag relationships."""

        return self._plan_relations(BatchOperationType.ADD_PAGE_TAGS, page_ids, tag_ids)

    def plan_remove_tags(
        self, page_ids: Sequence[int], tag_ids: Sequence[int]
    ) -> BatchOperationPlan:
        """Preflight removal of only the requested direct page/tag relationships."""

        return self._plan_relations(BatchOperationType.REMOVE_PAGE_TAGS, page_ids, tag_ids)

    def plan_add_projects(
        self, page_ids: Sequence[int], project_ids: Sequence[int]
    ) -> BatchOperationPlan:
        """Preflight additive direct page/project relationships."""

        return self._plan_relations(
            BatchOperationType.ADD_PAGE_PROJECTS, page_ids, project_ids
        )

    def plan_remove_projects(
        self, page_ids: Sequence[int], project_ids: Sequence[int]
    ) -> BatchOperationPlan:
        """Preflight removal of only requested direct page/project relationships."""

        return self._plan_relations(
            BatchOperationType.REMOVE_PAGE_PROJECTS, page_ids, project_ids
        )

    def execute(self, plan: BatchOperationPlan) -> BatchOperationResult:
        """Revalidate and commit one plan in a single ``BEGIN IMMEDIATE`` transaction."""

        if not plan.executable:
            return _failed_result(
                plan,
                BatchFailureCode.INVALID_REQUEST,
                plan.invalid_reason or _blocked_plan_reason(plan),
            )
        try:
            with self._repository.transaction() as connection:
                self._validate_page_snapshots(connection, plan)
                if plan.operation is BatchOperationType.SET_PAGE_STATUS:
                    changed_count, affected_page_ids = self._execute_status(connection, plan)
                    unchanged_count = len(plan.unchanged_page_ids)
                elif plan.operation in _RELATION_SPECS:
                    changed_count, unchanged_count, affected_page_ids = (
                        self._execute_relations(connection, plan)
                    )
                else:
                    raise BatchValidationError(f"不支持的批量操作：{plan.operation}")
        except BatchConflictError as exc:
            return _failed_result(
                plan,
                BatchFailureCode.STALE_CONFLICT,
                str(exc),
                stale_page_ids=exc.stale_page_ids,
            )
        except Exception as exc:
            LOGGER.exception("页面批量操作已回滚：operation=%s", plan.operation.value)
            return _failed_result(
                plan,
                BatchFailureCode.EXECUTION_FAILED,
                f"批量操作执行失败，事务已回滚：{exc}",
            )
        return BatchOperationResult(
            requested_count=plan.requested_count,
            requested_relation_count=plan.requested_relation_count,
            changed_count=changed_count,
            unchanged_count=unchanged_count,
            affected_page_ids=affected_page_ids,
            operation=plan.operation,
            target_ids=plan.target_ids,
            target_status=plan.target_status,
            committed=True,
        )

    def _plan_relations(
        self,
        operation: BatchOperationType,
        page_ids: Sequence[int],
        target_ids: Sequence[int],
    ) -> BatchOperationPlan:
        spec = _RELATION_SPECS[operation]
        requested_count, normalized_pages, page_error = _normalize_ids(page_ids, "页面")
        _, normalized_targets, target_error = _normalize_ids(target_ids, spec.target_label)
        invalid_reason = page_error or target_error
        if not normalized_pages and invalid_reason is None:
            invalid_reason = "页面 ID 不能为空。"
        if not normalized_targets and invalid_reason is None:
            invalid_reason = f"{spec.target_label} ID 不能为空。"
        if invalid_reason is not None:
            return BatchOperationPlan(
                operation=operation,
                requested_count=requested_count,
                page_ids=normalized_pages,
                target_ids=normalized_targets,
                invalid_reason=invalid_reason,
            )

        snapshots, found_targets, existing = self._repository.read_relation_preflight(
            normalized_pages, normalized_targets, spec
        )
        snapshot_by_id = {snapshot.page_id: snapshot for snapshot in snapshots}
        missing_pages = tuple(
            page_id for page_id in normalized_pages if page_id not in snapshot_by_id
        )
        missing_targets = tuple(
            target_id for target_id in normalized_targets if target_id not in found_targets
        )
        requested_relation_count = len(normalized_pages) * len(normalized_targets)
        existing_counts: dict[int, int] = {}
        for item in existing:
            existing_counts[item.page_id] = existing_counts.get(item.page_id, 0) + 1
        if operation in _ADD_OPERATIONS:
            eligible_relation_count = requested_relation_count - len(existing)
            unchanged_relation_count = len(existing)
            eligible_page_set = {
                page_id
                for page_id in normalized_pages
                if existing_counts.get(page_id, 0) < len(normalized_targets)
            }
        else:
            eligible_relation_count = len(existing)
            unchanged_relation_count = requested_relation_count - len(existing)
            eligible_page_set = set(existing_counts)
        eligible_page_ids = tuple(
            page_id for page_id in normalized_pages if page_id in eligible_page_set
        )
        unchanged_page_ids = tuple(
            page_id for page_id in normalized_pages if page_id not in eligible_page_set
        )
        return BatchOperationPlan(
            operation=operation,
            requested_count=requested_count,
            page_ids=normalized_pages,
            target_ids=normalized_targets,
            eligible_page_ids=eligible_page_ids,
            unchanged_page_ids=unchanged_page_ids,
            existing_relations=tuple(
                sorted(existing, key=lambda item: (item.page_id, item.target_id))
            ),
            eligible_relation_count=eligible_relation_count,
            unchanged_relation_count=unchanged_relation_count,
            missing_page_ids=missing_pages,
            missing_target_ids=missing_targets,
            snapshots=tuple(
                snapshot_by_id[page_id]
                for page_id in normalized_pages
                if page_id in snapshot_by_id
            ),
            executable=not missing_pages and not missing_targets,
        )

    def _validate_page_snapshots(
        self, connection: sqlite3.Connection, plan: BatchOperationPlan
    ) -> None:
        current = self._repository.load_page_snapshots(connection, plan.page_ids)
        expected_by_id = {snapshot.page_id: snapshot for snapshot in plan.snapshots}
        current_by_id = {snapshot.page_id: snapshot for snapshot in current}
        stale = tuple(
            page_id
            for page_id in plan.page_ids
            if current_by_id.get(page_id) != expected_by_id.get(page_id)
        )
        if stale:
            raise BatchConflictError(
                f"批量计划已过期，{len(stale)} 个页面发生变化或已不存在。", stale
            )

    def _execute_status(
        self, connection: sqlite3.Connection, plan: BatchOperationPlan
    ) -> tuple[int, tuple[int, ...]]:
        if plan.target_status is None:
            raise BatchValidationError("批量状态计划缺少目标状态。")
        timestamp = _utc_now()
        reviewed_at = timestamp if plan.target_status is PageStatus.REVIEWED else None
        changed_count = 0
        for page_chunk in _chunks(
            plan.eligible_page_ids, MAX_SQL_PARAMETERS - 3
        ):
            changed_count += self._repository._write_status_chunk(
                connection,
                page_chunk,
                plan.target_status,
                reviewed_at,
                timestamp,
            )
        if changed_count != len(plan.eligible_page_ids):
            raise BatchExecutionError(
                "页面状态写入数量与预检不一致，事务已取消。"
            )
        return changed_count, plan.eligible_page_ids

    def _execute_relations(
        self, connection: sqlite3.Connection, plan: BatchOperationPlan
    ) -> tuple[int, int, tuple[int, ...]]:
        spec = _RELATION_SPECS[plan.operation]
        found_targets = self._repository.load_target_ids(
            connection, plan.target_ids, spec
        )
        if found_targets != set(plan.target_ids):
            raise BatchConflictError(
                f"批量计划已过期，目标{spec.target_label}已发生变化。"
            )
        current_existing = self._repository.load_existing_relations(
            connection, plan.page_ids, plan.target_ids, spec
        )
        expected_existing = set(plan.existing_relations)
        if current_existing != expected_existing:
            changed_pairs = current_existing.symmetric_difference(expected_existing)
            stale_pages = tuple(
                page_id
                for page_id in plan.page_ids
                if any(item.page_id == page_id for item in changed_pairs)
            )
            raise BatchConflictError(
                "批量计划已过期，页面直接分类关系已发生变化。", stale_pages
            )

        changed_count = 0
        timestamp = _utc_now()
        reserved_parameters = 1 if plan.operation in _ADD_OPERATIONS else 0
        for page_chunk, target_chunk in _relation_chunks(
            plan.page_ids,
            plan.target_ids,
            reserved_parameters=reserved_parameters,
        ):
            changed_count += self._repository._write_relation_chunk(
                connection,
                plan.operation,
                spec,
                page_chunk,
                target_chunk,
                timestamp,
            )
        if changed_count != plan.eligible_relation_count:
            raise BatchExecutionError(
                "页面分类写入数量与预检不一致，事务已取消。"
            )
        return changed_count, plan.unchanged_relation_count, plan.eligible_page_ids


class _BatchRepository:
    """Parameterized schema-v4 repository with an explicit write transaction."""

    WRITE_BEGIN_SQL: Final[str] = "BEGIN IMMEDIATE"

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = Path(database_path)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Keep validation and every SQL chunk inside one immediate transaction."""

        connection = self._open_connection()
        try:
            connection.execute(self.WRITE_BEGIN_SQL)
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def read_page_snapshots(
        self, page_ids: Sequence[int]
    ) -> tuple[PageBatchSnapshot, ...]:
        with self._read_connection() as connection:
            return self.load_page_snapshots(connection, page_ids)

    def read_relation_preflight(
        self,
        page_ids: Sequence[int],
        target_ids: Sequence[int],
        spec: _RelationSpec,
    ) -> tuple[tuple[PageBatchSnapshot, ...], set[int], set[PageRelation]]:
        with self._read_connection() as connection:
            snapshots = self.load_page_snapshots(connection, page_ids)
            found_targets = self.load_target_ids(connection, target_ids, spec)
            existing = self.load_existing_relations(
                connection, page_ids, target_ids, spec
            )
        return snapshots, found_targets, existing

    def load_page_snapshots(
        self, connection: sqlite3.Connection, page_ids: Sequence[int]
    ) -> tuple[PageBatchSnapshot, ...]:
        found: dict[int, PageBatchSnapshot] = {}
        for page_chunk in _chunks(page_ids, MAX_SQL_PARAMETERS):
            placeholders = _placeholders(len(page_chunk))
            rows = connection.execute(
                f"""SELECT id, review_status, updated_at FROM pages
                WHERE id IN ({placeholders})""",
                page_chunk,
            ).fetchall()
            for row in rows:
                snapshot = PageBatchSnapshot(
                    page_id=int(row["id"]),
                    status=PageStatus(str(row["review_status"])),
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                )
                found[snapshot.page_id] = snapshot
        return tuple(found[page_id] for page_id in page_ids if page_id in found)

    def load_target_ids(
        self,
        connection: sqlite3.Connection,
        target_ids: Sequence[int],
        spec: _RelationSpec,
    ) -> set[int]:
        found: set[int] = set()
        for target_chunk in _chunks(target_ids, MAX_SQL_PARAMETERS):
            placeholders = _placeholders(len(target_chunk))
            rows = connection.execute(
                f"SELECT id FROM {spec.target_table} WHERE id IN ({placeholders})",
                target_chunk,
            ).fetchall()
            found.update(int(row["id"]) for row in rows)
        return found

    def load_existing_relations(
        self,
        connection: sqlite3.Connection,
        page_ids: Sequence[int],
        target_ids: Sequence[int],
        spec: _RelationSpec,
    ) -> set[PageRelation]:
        found: set[PageRelation] = set()
        for page_chunk, target_chunk in _relation_chunks(page_ids, target_ids):
            page_placeholders = _placeholders(len(page_chunk))
            target_placeholders = _placeholders(len(target_chunk))
            rows = connection.execute(
                f"""SELECT {spec.page_column}, {spec.target_column} FROM {spec.table}
                WHERE {spec.page_column} IN ({page_placeholders})
                  AND {spec.target_column} IN ({target_placeholders})""",
                (*page_chunk, *target_chunk),
            ).fetchall()
            found.update(
                PageRelation(int(row[spec.page_column]), int(row[spec.target_column]))
                for row in rows
            )
        return found

    def _write_status_chunk(
        self,
        connection: sqlite3.Connection,
        page_ids: Sequence[int],
        target_status: PageStatus,
        reviewed_at: str | None,
        updated_at: str,
    ) -> int:
        placeholders = _placeholders(len(page_ids))
        cursor = connection.execute(
            f"""UPDATE pages SET review_status = ?, reviewed_at = ?, updated_at = ?
            WHERE id IN ({placeholders})""",
            (target_status.value, reviewed_at, updated_at, *page_ids),
        )
        return cursor.rowcount

    def _write_relation_chunk(
        self,
        connection: sqlite3.Connection,
        operation: BatchOperationType,
        spec: _RelationSpec,
        page_ids: Sequence[int],
        target_ids: Sequence[int],
        timestamp: str,
    ) -> int:
        page_placeholders = _placeholders(len(page_ids))
        target_placeholders = _placeholders(len(target_ids))
        if operation in _ADD_OPERATIONS:
            if spec is _TAG_SPEC:
                cursor = connection.execute(
                    f"""INSERT OR IGNORE INTO page_tags(page_id, tag_id, created_at)
                    SELECT p.id, t.id, ? FROM pages AS p CROSS JOIN tags AS t
                    WHERE p.id IN ({page_placeholders})
                      AND t.id IN ({target_placeholders})""",
                    (timestamp, *page_ids, *target_ids),
                )
            else:
                cursor = connection.execute(
                    f"""INSERT OR IGNORE INTO project_pages(project_id, page_id, created_at)
                    SELECT pr.id, p.id, ? FROM pages AS p CROSS JOIN projects AS pr
                    WHERE p.id IN ({page_placeholders})
                      AND pr.id IN ({target_placeholders})""",
                    (timestamp, *page_ids, *target_ids),
                )
        else:
            cursor = connection.execute(
                f"""DELETE FROM {spec.table}
                WHERE {spec.page_column} IN ({page_placeholders})
                  AND {spec.target_column} IN ({target_placeholders})""",
                (*page_ids, *target_ids),
            )
        return cursor.rowcount


def _normalize_ids(
    values: Sequence[int], label: str
) -> tuple[int, tuple[int, ...], str | None]:
    requested = tuple(values)
    normalized: list[int] = []
    seen: set[int] = set()
    for value in requested:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return (
                len(requested),
                tuple(normalized),
                f"{label} ID 必须全部是正整数：{value!r}。",
            )
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return len(requested), tuple(normalized), None


def _normalize_status_target(
    value: PageStatus | str,
) -> tuple[PageStatus | None, str | None]:
    try:
        normalized = value if isinstance(value, PageStatus) else PageStatus(value)
    except ValueError:
        return None, f"非法的页面批量目标状态：{value!s}。"
    if normalized not in _ALLOWED_STATUS_SOURCES:
        return normalized, f"页面批量目标状态不允许为 {normalized.value}。"
    return normalized, None


def _protected_status_reason(current: PageStatus, target: PageStatus) -> str:
    if current is PageStatus.DRAFT:
        return "草稿页面包含优先级更高的人工编辑，不能批量覆盖。"
    if current is PageStatus.FAILED:
        return "失败页面需要单独处理，不能批量覆盖。"
    return f"不允许直接从 {current.value} 批量变更为 {target.value}。"


def _blocked_plan_reason(plan: BatchOperationPlan) -> str:
    reasons: list[str] = []
    if plan.missing_page_ids:
        reasons.append(f"{len(plan.missing_page_ids)} 个页面不存在")
    if plan.missing_target_ids:
        reasons.append(f"{len(plan.missing_target_ids)} 个分类目标不存在")
    if plan.protected_pages:
        reasons.append(f"{len(plan.protected_pages)} 个页面受保护")
    return "批量计划不可执行：" + "，".join(reasons or ("请求无效",)) + "。"


def _failed_result(
    plan: BatchOperationPlan,
    code: BatchFailureCode,
    reason: str,
    *,
    stale_page_ids: Sequence[int] = (),
) -> BatchOperationResult:
    return BatchOperationResult(
        requested_count=plan.requested_count,
        requested_relation_count=plan.requested_relation_count,
        changed_count=0,
        unchanged_count=0,
        affected_page_ids=(),
        operation=plan.operation,
        target_ids=plan.target_ids,
        target_status=plan.target_status,
        committed=False,
        failure_code=code,
        failure_reason=reason,
        stale_page_ids=tuple(stale_page_ids),
    )


def _chunks(values: Sequence[int], size: int) -> Iterator[tuple[int, ...]]:
    if size < 1:
        raise ValueError("SQL 分块大小必须大于零")
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _relation_chunks(
    page_ids: Sequence[int],
    target_ids: Sequence[int],
    *,
    reserved_parameters: int = 0,
) -> Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    target_capacity = min(
        _RELATION_TARGET_CHUNK_SIZE,
        MAX_SQL_PARAMETERS - reserved_parameters - 1,
    )
    for target_chunk in _chunks(target_ids, target_capacity):
        page_capacity = MAX_SQL_PARAMETERS - reserved_parameters - len(target_chunk)
        for page_chunk in _chunks(page_ids, page_capacity):
            yield page_chunk, target_chunk


def _placeholders(count: int) -> str:
    if count < 1:
        raise ValueError("SQL 参数列表不能为空")
    return ", ".join("?" for _ in range(count))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
