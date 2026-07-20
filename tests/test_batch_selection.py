"""Tests for visible-only batch selection, plan binding, and action tokens."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.batch_selection import (
    BatchSelectionSource,
    BatchSelectionState,
    bind_batch_plan,
    build_visible_page_scope,
    clear_selection,
    confirm_pending_action,
    consume_pending_action,
    finish_pending_action,
    has_dirty_selected_page,
    pending_action_matches,
    reconcile_scope,
    select_all_visible,
    set_page_selected,
)
from src.batch_service import BatchOperationPlan, BatchOperationType


def _scope(**changes):
    values = {
        "source": BatchSelectionSource.SEARCH,
        "document_id": 7,
        "filters": {"statuses": ["pending"], "tags": [2, 1]},
        "sort": "document_page",
        "query": " 液压泵 ",
        "batch_number": 1,
        "visible_page_ids": [11, 12, 13],
    }
    values.update(changes)
    return build_visible_page_scope(**values)


def _plan(page_ids=(11, 12)) -> BatchOperationPlan:
    return BatchOperationPlan(
        operation=BatchOperationType.SET_PAGE_STATUS,
        requested_count=len(page_ids),
        page_ids=tuple(page_ids),
        eligible_page_ids=tuple(page_ids),
        executable=True,
    )


def _bound_state():
    scope = _scope()
    state = select_all_visible(BatchSelectionState(), scope)
    state = bind_batch_plan(
        state,
        scope,
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_key=("reviewed",),
        target_label="标记为已复核",
        plan=_plan((11, 12, 13)),
        nonce="fixed-nonce",
    )
    return scope, state


def test_scope_signature_is_canonical_for_equivalent_filter_dicts() -> None:
    first = _scope(filters={"tags": [2, 1], "nested": {"b": 2, "a": 1}})
    second = _scope(filters={"nested": {"a": 1, "b": 2}, "tags": [2, 1]})

    assert first.signature == second.signature
    assert first.normalized_filters == second.normalized_filters
    assert first.query == "液压泵"


@pytest.mark.parametrize(
    "changes",
    [
        {"query": "液压阀"},
        {"filters": {"statuses": ["draft"]}},
        {"sort": "updated_desc"},
        {"batch_number": 2},
        {"visible_page_ids": [13, 12, 11]},
        {"visible_page_ids": [11, 12]},
        {"source": BatchSelectionSource.REVIEW_QUEUE},
        {"document_id": 8},
    ],
)
def test_scope_signature_changes_for_every_selection_boundary(changes: dict) -> None:
    assert _scope(**changes).signature != _scope().signature


def test_scope_normalizes_duplicate_visible_page_ids_without_reordering() -> None:
    scope = _scope(visible_page_ids=[12, 11, 12, 13, 11])

    assert scope.visible_page_ids == (12, 11, 13)


def test_scope_change_clears_selection_plan_confirmation_and_token() -> None:
    old_scope, state = _bound_state()
    state = confirm_pending_action(state, state.pending_action.token, confirmed=True)  # type: ignore[union-attr]

    transition = reconcile_scope(state, _scope(batch_number=2))

    assert old_scope.signature != transition.state.scope_signature
    assert transition.scope_changed
    assert transition.cleared_sensitive_state
    assert transition.state.selected_page_ids == ()
    assert transition.state.pending_action is None
    assert transition.state.confirmed_token is None


def test_same_scope_intersects_selection_with_current_visible_ids() -> None:
    scope = _scope()
    state = replace(
        BatchSelectionState(),
        scope_signature=scope.signature,
        selected_page_ids=(11, 13, 999),
    )

    transition = reconcile_scope(state, scope)

    assert not transition.scope_changed
    assert transition.pruned_hidden_ids == (999,)
    assert transition.state.selected_page_ids == (11, 13)


def test_visible_selection_supports_all_clear_single_and_rejects_hidden_ids() -> None:
    scope = _scope()
    state = select_all_visible(BatchSelectionState(), scope)
    assert state.selected_page_ids == (11, 12, 13)

    state = clear_selection(state)
    assert state.selected_page_ids == ()
    state = set_page_selected(state, scope, 12, selected=True)
    state = set_page_selected(state, scope, 12, selected=True)
    state = set_page_selected(state, scope, 999, selected=True)
    assert state.selected_page_ids == (12,)
    state = set_page_selected(state, scope, 12, selected=False)
    assert state.selected_page_ids == ()


def test_selection_change_invalidates_bound_plan_and_confirmation() -> None:
    scope, state = _bound_state()
    token = state.pending_action.token  # type: ignore[union-attr]
    state = confirm_pending_action(state, token, confirmed=True)

    changed = set_page_selected(state, scope, 13, selected=False)

    assert changed.selected_page_ids == (11, 12)
    assert changed.pending_action is None
    assert changed.confirmed_token is None


def test_pending_action_matches_exact_scope_selection_operation_and_target() -> None:
    scope, state = _bound_state()

    assert pending_action_matches(
        state,
        scope,
        BatchOperationType.SET_PAGE_STATUS,
        ("reviewed",),
    )
    assert not pending_action_matches(
        state,
        scope,
        BatchOperationType.SET_PAGE_STATUS,
        ("skipped",),
    )
    assert not pending_action_matches(
        state,
        _scope(batch_number=2),
        BatchOperationType.SET_PAGE_STATUS,
        ("reviewed",),
    )
    assert not pending_action_matches(
        replace(state, selected_page_ids=(11, 12)),
        scope,
        BatchOperationType.SET_PAGE_STATUS,
        ("reviewed",),
    )


def test_action_token_binds_plan_snapshot_and_nonce_deterministically() -> None:
    scope = _scope()
    selected = select_all_visible(BatchSelectionState(), scope)
    first = bind_batch_plan(
        selected,
        scope,
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_key=("reviewed",),
        target_label="标记为已复核",
        plan=_plan((11, 12, 13)),
        nonce="same",
    )
    second = bind_batch_plan(
        selected,
        scope,
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_key=("reviewed",),
        target_label="标记为已复核",
        plan=_plan((11, 12, 13)),
        nonce="same",
    )
    changed_plan = bind_batch_plan(
        selected,
        scope,
        operation=BatchOperationType.SET_PAGE_STATUS,
        target_key=("reviewed",),
        target_label="标记为已复核",
        plan=replace(_plan((11, 12, 13)), unchanged_page_ids=(13,)),
        nonce="same",
    )

    assert first.pending_action.token == second.pending_action.token  # type: ignore[union-attr]
    assert first.pending_action.token != changed_plan.pending_action.token  # type: ignore[union-attr]


def test_action_token_requires_confirmation_and_can_only_be_consumed_once() -> None:
    _, state = _bound_state()
    token = state.pending_action.token  # type: ignore[union-attr]

    unconfirmed, accepted = consume_pending_action(state, token)
    assert not accepted and unconfirmed == state

    confirmed = confirm_pending_action(state, token, confirmed=True)
    consumed, accepted = consume_pending_action(confirmed, token)
    assert accepted
    assert token in consumed.consumed_tokens
    assert consumed.pending_action is None
    repeated, accepted_again = consume_pending_action(consumed, token)
    assert not accepted_again and repeated == consumed


def test_finish_success_clears_selection_while_failure_preserves_it() -> None:
    scope, state = _bound_state()
    token = state.pending_action.token  # type: ignore[union-attr]
    confirmed = confirm_pending_action(state, token, confirmed=True)
    consumed, accepted = consume_pending_action(confirmed, token)
    assert accepted

    succeeded = finish_pending_action(consumed, committed=True)
    failed = finish_pending_action(consumed, committed=False)

    assert succeeded.selected_page_ids == ()
    assert failed.selected_page_ids == scope.visible_page_ids
    assert succeeded.pending_action is None and failed.pending_action is None


def test_dirty_page_guard_only_blocks_when_dirty_page_is_selected() -> None:
    assert has_dirty_selected_page((11, 12), (12,))
    assert not has_dirty_selected_page((11,), (12,))
    assert not has_dirty_selected_page((), (12,))
