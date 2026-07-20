"""Pure visible-page selection, plan binding, and one-shot action state."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, Final

from src.batch_service import BatchOperationPlan, BatchOperationType

_MAX_CONSUMED_TOKENS: Final[int] = 32


class BatchSelectionError(ValueError):
    """Raised when UI state attempts to exceed its current visible scope."""


class BatchSelectionSource(StrEnum):
    """Page surface that owns the current visible selection."""

    SEARCH = "search"
    REVIEW_QUEUE = "review_queue"


@dataclass(frozen=True, slots=True)
class VisiblePageScope:
    """Stable signature boundary for exactly the pages rendered to the user."""

    source: BatchSelectionSource
    document_id: int | None
    normalized_filters: str
    sort: str
    query: str
    batch_number: int
    visible_page_ids: tuple[int, ...]
    signature: str


@dataclass(frozen=True, slots=True)
class BoundBatchAction:
    """One preflight plan bound to scope, selection, operation, and target."""

    scope_signature: str
    selected_page_ids: tuple[int, ...]
    operation: BatchOperationType
    target_key: tuple[str, ...]
    target_label: str
    plan: BatchOperationPlan
    token: str


@dataclass(frozen=True, slots=True)
class BatchSelectionState:
    """Session-safe state whose consumed tokens survive pending-plan cleanup."""

    scope_signature: str = ""
    selected_page_ids: tuple[int, ...] = ()
    pending_action: BoundBatchAction | None = None
    confirmed_token: str | None = None
    consumed_tokens: tuple[str, ...] = ()
    widget_generation: int = 0


@dataclass(frozen=True, slots=True)
class ScopeReconcileResult:
    """Result of restricting prior state to one current visible scope."""

    state: BatchSelectionState
    scope_changed: bool = False
    cleared_sensitive_state: bool = False
    pruned_hidden_ids: tuple[int, ...] = ()


def build_visible_page_scope(
    *,
    source: BatchSelectionSource | str,
    document_id: int | None,
    filters: object,
    sort: str,
    query: str,
    batch_number: int,
    visible_page_ids: Sequence[int],
) -> VisiblePageScope:
    """Build a deterministic scope whose signature includes ordered visible IDs."""

    normalized_source = BatchSelectionSource(source)
    if document_id is not None and document_id <= 0:
        raise BatchSelectionError("文档 ID 必须是正整数。")
    if batch_number < 1:
        raise BatchSelectionError("可见批次编号必须从 1 开始。")
    normalized_ids = _normalize_ids(visible_page_ids, "可见页面")
    normalized_filters = _canonical_json(filters)
    normalized_query = unicodedata.normalize("NFKC", str(query)).strip()
    normalized_sort = str(sort).strip()
    payload = {
        "source": normalized_source.value,
        "document_id": document_id,
        "filters": json.loads(normalized_filters),
        "sort": normalized_sort,
        "query": normalized_query,
        "batch_number": batch_number,
        "visible_page_ids": normalized_ids,
    }
    signature = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return VisiblePageScope(
        source=normalized_source,
        document_id=document_id,
        normalized_filters=normalized_filters,
        sort=normalized_sort,
        query=normalized_query,
        batch_number=batch_number,
        visible_page_ids=normalized_ids,
        signature=signature,
    )


def reconcile_scope(
    state: BatchSelectionState, scope: VisiblePageScope
) -> ScopeReconcileResult:
    """Clear changed ranges and always remove IDs that are no longer visible."""

    if state.scope_signature != scope.signature:
        had_scope = bool(state.scope_signature)
        had_sensitive_state = bool(
            state.selected_page_ids or state.pending_action or state.confirmed_token
        )
        return ScopeReconcileResult(
            state=BatchSelectionState(
                scope_signature=scope.signature,
                consumed_tokens=state.consumed_tokens,
                widget_generation=state.widget_generation,
            ),
            scope_changed=had_scope,
            cleared_sensitive_state=had_scope and had_sensitive_state,
        )

    visible = set(scope.visible_page_ids)
    pruned = tuple(page_id for page_id in state.selected_page_ids if page_id not in visible)
    kept_set = set(state.selected_page_ids) & visible
    kept = tuple(page_id for page_id in scope.visible_page_ids if page_id in kept_set)
    if not pruned and kept == state.selected_page_ids:
        return ScopeReconcileResult(state=state)
    return ScopeReconcileResult(
        state=replace(
            state,
            selected_page_ids=kept,
            pending_action=None,
            confirmed_token=None,
        ),
        cleared_sensitive_state=bool(state.pending_action or state.confirmed_token),
        pruned_hidden_ids=pruned,
    )


def select_all_visible(
    state: BatchSelectionState, scope: VisiblePageScope
) -> BatchSelectionState:
    """Select exactly the ordered IDs rendered in the current scope."""

    current = reconcile_scope(state, scope).state
    if current.selected_page_ids == scope.visible_page_ids:
        return current
    return replace(
        current,
        selected_page_ids=scope.visible_page_ids,
        pending_action=None,
        confirmed_token=None,
    )


def clear_selection(state: BatchSelectionState) -> BatchSelectionState:
    """Clear selected IDs and every executable plan while retaining token history."""

    return replace(
        state,
        selected_page_ids=(),
        pending_action=None,
        confirmed_token=None,
    )


def invalidate_pending_action(state: BatchSelectionState) -> BatchSelectionState:
    """Discard a plan and confirmation without changing the visible selection."""

    return replace(state, pending_action=None, confirmed_token=None)


def set_page_selected(
    state: BatchSelectionState,
    scope: VisiblePageScope,
    page_id: int,
    *,
    selected: bool,
) -> BatchSelectionState:
    """Update one stable page ID; hidden IDs can never enter the selection."""

    current = reconcile_scope(state, scope).state
    if page_id not in set(scope.visible_page_ids):
        return current
    selected_set = set(current.selected_page_ids)
    if selected:
        selected_set.add(page_id)
    else:
        selected_set.discard(page_id)
    normalized = tuple(
        visible_id for visible_id in scope.visible_page_ids if visible_id in selected_set
    )
    if normalized == current.selected_page_ids:
        return current
    return replace(
        current,
        selected_page_ids=normalized,
        pending_action=None,
        confirmed_token=None,
    )


def bind_batch_plan(
    state: BatchSelectionState,
    scope: VisiblePageScope,
    *,
    operation: BatchOperationType,
    target_key: Sequence[str],
    target_label: str,
    plan: BatchOperationPlan,
    nonce: str | None = None,
) -> BatchSelectionState:
    """Bind a preflight plan to the exact visible selection and a unique token."""

    current = reconcile_scope(state, scope).state
    if not current.selected_page_ids:
        raise BatchSelectionError("没有选中任何当前可见页面。")
    if plan.operation is not operation:
        raise BatchSelectionError("批量计划类型与当前动作不一致。")
    if plan.page_ids != current.selected_page_ids:
        raise BatchSelectionError("批量计划页面与当前选择不一致。")
    normalized_target = tuple(str(value) for value in target_key)
    payload = {
        "scope_signature": scope.signature,
        "selected_page_ids": current.selected_page_ids,
        "operation": operation.value,
        "target_key": normalized_target,
        "plan": asdict(plan),
        "nonce": nonce or uuid.uuid4().hex,
    }
    token = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    action = BoundBatchAction(
        scope_signature=scope.signature,
        selected_page_ids=current.selected_page_ids,
        operation=operation,
        target_key=normalized_target,
        target_label=target_label,
        plan=plan,
        token=token,
    )
    return replace(current, pending_action=action, confirmed_token=None)


def pending_action_matches(
    state: BatchSelectionState,
    scope: VisiblePageScope,
    operation: BatchOperationType,
    target_key: Sequence[str],
) -> bool:
    """Return whether the plan remains bound to every current action input."""

    action = state.pending_action
    return bool(
        action is not None
        and action.scope_signature == scope.signature
        and action.selected_page_ids == state.selected_page_ids
        and action.operation is operation
        and action.target_key == tuple(str(value) for value in target_key)
        and action.token not in state.consumed_tokens
    )


def confirm_pending_action(
    state: BatchSelectionState, token: str, *, confirmed: bool
) -> BatchSelectionState:
    """Record explicit confirmation only for the currently bound token."""

    action = state.pending_action
    if action is None or action.token != token or token in state.consumed_tokens:
        return replace(state, confirmed_token=None)
    return replace(state, confirmed_token=token if confirmed else None)


def consume_pending_action(
    state: BatchSelectionState, token: str
) -> tuple[BatchSelectionState, bool]:
    """Consume one confirmed token before calling the database service."""

    action = state.pending_action
    if (
        action is None
        or action.token != token
        or state.confirmed_token != token
        or token in state.consumed_tokens
    ):
        return state, False
    consumed = (*state.consumed_tokens, token)[-_MAX_CONSUMED_TOKENS:]
    return (
        replace(
            state,
            pending_action=None,
            confirmed_token=None,
            consumed_tokens=consumed,
        ),
        True,
    )


def finish_pending_action(
    state: BatchSelectionState, *, committed: bool
) -> BatchSelectionState:
    """Clear successful selections; preserve visible selection after any failure."""

    return replace(
        state,
        selected_page_ids=() if committed else state.selected_page_ids,
        pending_action=None,
        confirmed_token=None,
        widget_generation=state.widget_generation + (1 if committed else 0),
    )


def has_dirty_selected_page(
    selected_page_ids: Sequence[int], dirty_page_ids: Sequence[int]
) -> bool:
    """Return whether any detectable unsaved editor belongs to the selection."""

    return bool(set(selected_page_ids) & set(dirty_page_ids))


def _normalize_ids(values: Sequence[int], label: str) -> tuple[int, ...]:
    normalized: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BatchSelectionError(f"{label} ID 必须全部是正整数：{value!r}。")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized)


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_ready(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_ready(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"无法生成稳定批量状态摘要：{type(value).__name__}")
