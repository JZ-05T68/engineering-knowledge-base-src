"""Shared vertical Streamlit UI for safe current-visible page batch operations."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import streamlit as st

from src.batch_selection import (
    BatchSelectionState,
    VisiblePageScope,
    bind_batch_plan,
    clear_selection,
    confirm_pending_action,
    consume_pending_action,
    finish_pending_action,
    has_dirty_selected_page,
    invalidate_pending_action,
    pending_action_matches,
    reconcile_scope,
    select_all_visible,
    set_page_selected,
)
from src.batch_service import (
    BatchFailureCode,
    BatchOperationPlan,
    BatchOperationResult,
    BatchOperationType,
    PageBatchService,
)
from src.models import PageStatus, Project, Tag

LOGGER = logging.getLogger(__name__)
_STATE_KEY: Final[str] = "visible_page_batch_state"
_FLASH_KEY: Final[str] = "visible_page_batch_flash"
_STATUS_LABELS: Final[dict[PageStatus, str]] = {
    PageStatus.REVIEWED: "标记为已复核",
    PageStatus.SKIPPED: "暂时跳过",
    PageStatus.PENDING: "重新打开复核",
}
_OPERATION_LABELS: Final[dict[BatchOperationType, str]] = {
    BatchOperationType.SET_PAGE_STATUS: "批量设置状态",
    BatchOperationType.ADD_PAGE_TAGS: "添加页面直接标签",
    BatchOperationType.REMOVE_PAGE_TAGS: "移除页面直接标签",
    BatchOperationType.ADD_PAGE_PROJECTS: "添加页面直接项目",
    BatchOperationType.REMOVE_PAGE_PROJECTS: "移除页面直接项目",
}


class BatchUiError(ValueError):
    """Raised when a visible batch action lacks a required explicit target."""


@dataclass(frozen=True, slots=True)
class BatchPlanUiSummary:
    """Compact page-oriented plan summary suitable for a narrow layout."""

    requested_pages: int
    changeable_pages: int
    unchanged_pages: int
    protected_pages: int
    draft_pages: int
    failed_pages: int
    missing_or_invalid: int
    changeable_relations: int
    unchanged_relations: int
    executable: bool


@dataclass(frozen=True, slots=True)
class BatchFeedback:
    """One atomic UI message and its Streamlit display level."""

    level: str
    message: str


def create_batch_plan(
    service: PageBatchService,
    page_ids: Sequence[int],
    operation: BatchOperationType,
    *,
    target_status: PageStatus | None = None,
    target_ids: Sequence[int] = (),
) -> BatchOperationPlan:
    """Dispatch one UI preflight to exactly one service planning method."""

    if not page_ids:
        raise BatchUiError("请先选择至少一个当前可见页面。")
    if operation is BatchOperationType.SET_PAGE_STATUS:
        if target_status not in _STATUS_LABELS:
            raise BatchUiError("请选择允许的页面状态目标。")
        return service.plan_status(page_ids, target_status)
    if not target_ids:
        raise BatchUiError("请选择至少一个标签或项目。")
    if operation is BatchOperationType.ADD_PAGE_TAGS:
        return service.plan_add_tags(page_ids, target_ids)
    if operation is BatchOperationType.REMOVE_PAGE_TAGS:
        return service.plan_remove_tags(page_ids, target_ids)
    if operation is BatchOperationType.ADD_PAGE_PROJECTS:
        return service.plan_add_projects(page_ids, target_ids)
    if operation is BatchOperationType.REMOVE_PAGE_PROJECTS:
        return service.plan_remove_projects(page_ids, target_ids)
    raise BatchUiError(f"不支持的批量操作：{operation}")


def summarize_batch_plan(plan: BatchOperationPlan) -> BatchPlanUiSummary:
    """Convert service-level details into exact page and relation counts."""

    statuses = [item.status for item in plan.protected_pages]
    return BatchPlanUiSummary(
        requested_pages=len(plan.page_ids),
        changeable_pages=len(plan.eligible_page_ids),
        unchanged_pages=len(plan.unchanged_page_ids),
        protected_pages=len(plan.protected_pages),
        draft_pages=statuses.count(PageStatus.DRAFT),
        failed_pages=statuses.count(PageStatus.FAILED),
        missing_or_invalid=(
            len(plan.missing_page_ids)
            + len(plan.missing_target_ids)
            + (1 if plan.invalid_reason else 0)
        ),
        changeable_relations=plan.eligible_relation_count,
        unchanged_relations=plan.unchanged_relation_count,
        executable=plan.executable,
    )


def result_feedback(result: BatchOperationResult) -> BatchFeedback:
    """Describe all-or-nothing results without implying partial commits."""

    if not result.committed:
        if result.failure_code is BatchFailureCode.STALE_CONFLICT:
            return BatchFeedback(
                "warning",
                "页面状态已发生变化，本次操作已取消且未修改任何页面，请重新预检。",
            )
        return BatchFeedback(
            "error",
            "批量操作失败，所有修改均已回滚，没有页面被部分修改。",
        )
    if result.changed_count == 0:
        return BatchFeedback(
            "info",
            f"所选 {result.requested_count} 页已经满足目标，没有执行数据库写入。",
        )
    if result.operation is BatchOperationType.SET_PAGE_STATUS:
        return BatchFeedback(
            "success",
            f"批量操作已完成：修改 {result.changed_count} 页，"
            f"{result.unchanged_count} 页原本已满足目标，无失败项。",
        )
    return BatchFeedback(
        "success",
        f"批量操作已完成：修改 {result.changed_count} 条页面直接关系，"
        f"涉及 {len(result.affected_page_ids)} 页，"
        f"{result.unchanged_count} 条关系原本已满足目标，无失败项。",
    )


def clear_inactive_visible_batch_state() -> bool:
    """Clear hidden selection when the current page renders no eligible scope."""

    state = _stored_state()
    had_sensitive_state = bool(
        state.selected_page_ids or state.pending_action or state.confirmed_token
    )
    if not state.scope_signature and not had_sensitive_state:
        return False
    _store_state(
        BatchSelectionState(
            consumed_tokens=state.consumed_tokens,
            widget_generation=state.widget_generation + 1,
        )
    )
    return had_sensitive_state


def render_visible_batch_feedback() -> None:
    """Render one post-execution message even when the result scope is now empty."""

    flash = st.session_state.pop(_FLASH_KEY, None)
    if isinstance(flash, tuple) and len(flash) == 2:
        level, message = flash
        getattr(st, str(level), st.info)(str(message))


def render_visible_page_batch_ui(
    *,
    scope: VisiblePageScope,
    page_labels: Mapping[int, str],
    service: PageBatchService,
    tags: Sequence[Tag],
    projects: Sequence[Project],
    dirty_page_ids: Sequence[int] = (),
    on_committed: Callable[[], None] | None = None,
) -> bool:
    """Render one visible-only selection and explicit preflight/confirm/execute flow."""

    if not scope.visible_page_ids:
        return False
    state = _stored_state()
    transition = reconcile_scope(state, scope)
    state = transition.state
    _store_state(state)

    with st.container(border=True):
        st.markdown("### 当前可见页面批量操作")
        st.caption("只会处理下方当前批次中明确勾选的页面，不会跨分页或扩大范围。")
        if transition.cleared_sensitive_state or transition.pruned_hidden_ids:
            st.info("页面范围已变化，原批量选择已清除。")

        if st.button(
            f"选择当前可见 {len(scope.visible_page_ids)} 项",
            key=f"visible_batch_select_all_{scope.source.value}",
            use_container_width=True,
        ):
            state = select_all_visible(state, scope)
            _store_state(state)
            for page_id in scope.visible_page_ids:
                st.session_state[_checkbox_key(scope, state, page_id)] = True
        if st.button(
            "清除选择",
            key=f"visible_batch_clear_{scope.source.value}",
            disabled=not state.selected_page_ids,
            use_container_width=True,
        ):
            state = clear_selection(state)
            _store_state(state)
            for page_id in scope.visible_page_ids:
                st.session_state[_checkbox_key(scope, state, page_id)] = False

        st.caption(
            f"已选择 {len(state.selected_page_ids)} / 当前可见 "
            f"{len(scope.visible_page_ids)}"
        )
        with st.expander("勾选当前可见页面", expanded=bool(state.selected_page_ids)):
            for page_id in scope.visible_page_ids:
                checkbox_key = _checkbox_key(scope, state, page_id)
                st.checkbox(
                    page_labels.get(page_id, f"页面 {page_id}"),
                    value=page_id in state.selected_page_ids,
                    key=checkbox_key,
                    on_change=_on_page_checkbox_change,
                    args=(scope, page_id, checkbox_key),
                )

        if not tags:
            st.info("当前没有可用标签；请先到“标签管理”创建标签。")
        if not projects:
            st.info("当前没有可用项目；请先到“项目管理”创建项目。")

        operations = [BatchOperationType.SET_PAGE_STATUS]
        if tags:
            operations.extend(
                (BatchOperationType.ADD_PAGE_TAGS, BatchOperationType.REMOVE_PAGE_TAGS)
            )
        if projects:
            operations.extend(
                (
                    BatchOperationType.ADD_PAGE_PROJECTS,
                    BatchOperationType.REMOVE_PAGE_PROJECTS,
                )
            )
        operation_key = f"visible_batch_operation_{scope.source.value}"
        allowed_operation_values = [operation.value for operation in operations]
        if st.session_state.get(operation_key) not in allowed_operation_values:
            st.session_state[operation_key] = BatchOperationType.SET_PAGE_STATUS.value
        operation = BatchOperationType(
            st.selectbox(
                "批量操作类型",
                options=allowed_operation_values,
                format_func=lambda value: _OPERATION_LABELS[BatchOperationType(value)],
                key=operation_key,
            )
        )
        target_status, target_ids, target_key, target_label = _render_target(
            scope, operation, tags, projects
        )
        if operation is not BatchOperationType.SET_PAGE_STATUS:
            st.caption(
                "此操作只修改页面直接分类，不会覆盖其他分类，也不会修改文档级标签或项目继承关系。"
            )

        if state.pending_action is not None and not pending_action_matches(
            state, scope, operation, target_key
        ):
            old_token = state.pending_action.token
            state = invalidate_pending_action(state)
            _store_state(state)
            st.session_state.pop(_confirm_key(old_token), None)

        dirty_selected = has_dirty_selected_page(
            state.selected_page_ids, dirty_page_ids
        )
        if dirty_selected:
            if state.pending_action is not None:
                state = invalidate_pending_action(state)
                _store_state(state)
            st.warning(
                "当前选中的页面存在未保存编辑，请先保存或从批量选择中移除该页面。"
            )
        target_missing = operation is not BatchOperationType.SET_PAGE_STATUS and not target_ids
        if st.button(
            "预检批量操作",
            key=f"visible_batch_preflight_{scope.source.value}",
            type="primary",
            disabled=(not state.selected_page_ids or target_missing or dirty_selected),
            use_container_width=True,
        ):
            try:
                plan = create_batch_plan(
                    service,
                    state.selected_page_ids,
                    operation,
                    target_status=target_status,
                    target_ids=target_ids,
                )
                state = bind_batch_plan(
                    state,
                    scope,
                    operation=operation,
                    target_key=target_key,
                    target_label=target_label,
                    plan=plan,
                )
            except Exception as exc:
                LOGGER.exception("生成页面批量计划失败：source=%s", scope.source.value)
                st.error(f"批量预检失败：{exc}。本次未修改任何页面。")
            else:
                _store_state(state)

        action = state.pending_action
        if action is None:
            return False
        _render_plan(action.plan, action.target_label)
        if not action.plan.executable:
            st.warning(_blocked_plan_message(action.plan))
            return False

        summary = summarize_batch_plan(action.plan)
        st.caption(
            f"范围仅限当前所选 {len(action.selected_page_ids)} 页；"
            f"将实际影响 {summary.changeable_pages} 页，"
            f"{summary.unchanged_pages} 页无需变化。"
        )
        confirm_key = _confirm_key(action.token)
        confirmed = st.checkbox(
            f"我确认对当前选择的 {len(action.selected_page_ids)} 个页面执行此操作",
            key=confirm_key,
        )
        state = confirm_pending_action(state, action.token, confirmed=confirmed)
        _store_state(state)
        if st.button(
            "执行批量操作",
            key=f"visible_batch_execute_{action.token}",
            type="primary",
            disabled=not confirmed or dirty_selected,
            use_container_width=True,
        ):
            consumed_state, accepted = consume_pending_action(state, action.token)
            if not accepted:
                st.warning("该批量操作已执行或确认已失效，请重新预检。")
                return False
            _store_state(consumed_state)
            result = service.execute(action.plan)
            finished_state = finish_pending_action(
                consumed_state, committed=result.committed
            )
            _store_state(finished_state)
            feedback = result_feedback(result)
            st.session_state[_FLASH_KEY] = (feedback.level, feedback.message)
            if result.committed and on_committed is not None:
                try:
                    on_committed()
                except Exception:
                    LOGGER.exception("批量提交后刷新当前页面数据失败")
                    st.session_state[_FLASH_KEY] = (
                        "warning",
                        feedback.message + " 页面显示刷新失败，请手动重新加载当前页面。",
                    )
            return True
    return False


def _render_target(
    scope: VisiblePageScope,
    operation: BatchOperationType,
    tags: Sequence[Tag],
    projects: Sequence[Project],
) -> tuple[PageStatus | None, tuple[int, ...], tuple[str, ...], str]:
    if operation is BatchOperationType.SET_PAGE_STATUS:
        widget_key = f"visible_batch_status_{scope.source.value}"
        allowed = [status.value for status in _STATUS_LABELS]
        if st.session_state.get(widget_key) not in allowed:
            st.session_state[widget_key] = PageStatus.REVIEWED.value
        target = PageStatus(
            st.selectbox(
                "目标状态",
                options=allowed,
                format_func=lambda value: _STATUS_LABELS[PageStatus(value)],
                key=widget_key,
            )
        )
        return target, (), (target.value,), _STATUS_LABELS[target]

    is_tag = operation in {
        BatchOperationType.ADD_PAGE_TAGS,
        BatchOperationType.REMOVE_PAGE_TAGS,
    }
    entities: Sequence[Tag | Project] = tags if is_tag else projects
    entity_names = {entity.id: entity.name for entity in entities}
    kind = "标签" if is_tag else "项目"
    widget_key = f"visible_batch_{'tags' if is_tag else 'projects'}_{scope.source.value}"
    current = st.session_state.get(widget_key, [])
    if not isinstance(current, list):
        current = []
    st.session_state[widget_key] = [
        int(value) for value in current if int(value) in entity_names
    ]
    selected = tuple(
        int(value)
        for value in st.multiselect(
            f"选择页面直接{kind}",
            options=list(entity_names),
            format_func=lambda value: entity_names[value],
            key=widget_key,
        )
    )
    target_label = "、".join(entity_names[value] for value in selected) or f"未选择{kind}"
    return None, selected, tuple(str(value) for value in selected), target_label


def _render_plan(plan: BatchOperationPlan, target_label: str) -> None:
    summary = summarize_batch_plan(plan)
    st.markdown("#### 批量计划")
    st.write(f"操作目标：{target_label}")
    st.write(f"请求页面：{summary.requested_pages} 页")
    st.write(f"可以变更：{summary.changeable_pages} 页")
    st.write(f"无需变更：{summary.unchanged_pages} 页")
    st.write(f"受保护：{summary.protected_pages} 页")
    st.write(f"缺失或非法：{summary.missing_or_invalid} 项")
    if plan.operation is not BatchOperationType.SET_PAGE_STATUS:
        st.write(
            f"直接关系变化：{summary.changeable_relations} 条；"
            f"无需变化：{summary.unchanged_relations} 条"
        )
    st.write("允许执行：是" if summary.executable else "允许执行：否")


def _blocked_plan_message(plan: BatchOperationPlan) -> str:
    summary = summarize_batch_plan(plan)
    reasons: list[str] = []
    if summary.draft_pages:
        reasons.append(f"{summary.draft_pages} 页处于草稿状态")
    if summary.failed_pages:
        reasons.append(f"{summary.failed_pages} 页处于失败状态")
    if summary.protected_pages > summary.draft_pages + summary.failed_pages:
        reasons.append(
            f"{summary.protected_pages - summary.draft_pages - summary.failed_pages} 页状态受保护"
        )
    if summary.missing_or_invalid:
        reasons.append(f"{summary.missing_or_invalid} 个对象缺失或非法")
    return "本次未修改任何页面：" + "，".join(reasons or ["计划不可执行"]) + "。"


def _stored_state() -> BatchSelectionState:
    value = st.session_state.get(_STATE_KEY)
    return value if isinstance(value, BatchSelectionState) else BatchSelectionState()


def _store_state(state: BatchSelectionState) -> None:
    st.session_state[_STATE_KEY] = state


def _checkbox_key(
    scope: VisiblePageScope, state: BatchSelectionState, page_id: int
) -> str:
    return (
        f"visible_batch_page_{scope.source.value}_{scope.signature}_"
        f"{state.widget_generation}_{page_id}"
    )


def _confirm_key(token: str) -> str:
    return f"visible_batch_confirm_{token}"


def _on_page_checkbox_change(
    scope: VisiblePageScope, page_id: int, checkbox_key: str
) -> None:
    state = _stored_state()
    _store_state(
        set_page_selected(
            state,
            scope,
            page_id,
            selected=bool(st.session_state.get(checkbox_key, False)),
        )
    )
