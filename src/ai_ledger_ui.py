"""Read-only AI call ledger dashboard (v0.5.3 Phase 5).

Renders only local SQLite aggregates and rows. It never calls a provider,
never shows prompts/answers, and offers no modify/delete/replay/clear/export
actions.
"""

from __future__ import annotations

import streamlit as st

from src.ai_ledger_service import AILedgerError, AILedgerService
from src.models import AICallLedgerEntry, AICallLedgerQuery

STATUS_LABELS = {
    "success": "成功",
    "error": "失败",
    "rejected": "预算拒绝",
}
CAPABILITY_LABELS = {
    "completion": "生成调用",
    "embedding": "向量嵌入",
    "rerank": "重排",
}
SORT_LABELS = {
    "created_at_desc": "最近调用优先",
    "created_at_asc": "最早调用优先",
    "latency_desc": "耗时最长优先",
    "total_tokens_desc": "Token 最多优先",
}


def _none_dash(value: int | None) -> str:
    return "—" if value is None else str(value)


def _render_entry(entry: AICallLedgerEntry) -> None:
    with st.container(border=True):
        head = st.columns([2.4, 1.2, 1.2, 1, 1.2, 1.2])
        head[0].caption(f"时间：{entry.created_at}")
        head[1].caption(f"功能：{entry.source_feature}")
        head[2].caption(
            f"类型：{CAPABILITY_LABELS.get(entry.capability, entry.capability)}"
        )
        head[3].caption(f"状态：{STATUS_LABELS.get(entry.status, entry.status)}")
        head[4].caption(f"Provider / Model：qwen / {entry.model}")
        head[5].caption(f"耗时：{_none_dash(entry.latency_ms)} ms")
        second = st.columns(4)
        second[0].caption(
            f"Token：in {_none_dash(entry.prompt_tokens)} / "
            f"out {_none_dash(entry.completion_tokens)} / "
            f"total {_none_dash(entry.total_tokens)}"
        )
        second[1].caption(f"重试：{entry.retry_count}")
        second[2].caption(f"target_refs：{len(entry.target_refs)} 项")
        second[3].caption("真实调用（Mock 离线演示不写入台账）")
        if entry.status != "success" and entry.error_summary:
            st.caption(f"错误摘要（已脱敏）：{entry.error_summary}")
        with st.expander(f"target_refs（{len(entry.target_refs)} 项）", expanded=False):
            if entry.target_refs_parse_error:
                st.caption("目标引用解析失败（历史异常行，未回写数据库）。")
            for stable_id in entry.target_refs:
                if stable_id in entry.unavailable_target_refs:
                    st.caption(f"{stable_id}（目标当前不可用）")
                else:
                    st.caption(stable_id)
            if not entry.target_refs and not entry.target_refs_parse_error:
                st.caption("本次调用没有目标引用。")


def render_ai_ledger_page(service: AILedgerService) -> None:
    """Render the read-only AI call ledger dashboard."""

    st.caption(
        "此页面只展示 AI 调用审计信息，不保存完整提示词、上下文正文或模型回答。"
    )
    try:
        stats = service.stats()
        models = service.distinct_models()
        features = service.distinct_source_features()
    except Exception as exc:
        st.error(f"读取 AI 调用台账失败：{exc}（其他页面不受影响）")
        return

    metrics = st.columns(4)
    metrics[0].metric("总调用", stats.total_calls)
    metrics[1].metric("成功", stats.success_count)
    metrics[2].metric("失败 / 拒绝", stats.error_count + stats.rejected_count)
    metrics[3].metric(
        "Token 合计",
        "—" if stats.total_tokens is None else stats.total_tokens,
    )
    st.caption(
        "Provider：qwen（运行时唯一接入供应商）；Mock 离线演示不写入台账，"
        "费用数据不存在，本页不做任何猜测。"
    )

    filters = st.columns(4)
    source_feature = filters[0].selectbox(
        "功能来源",
        ["全部", *features],
        key="ledger_source_feature",
    )
    capability = filters[1].selectbox(
        "调用类型",
        ["全部", "completion", "embedding", "rerank"],
        key="ledger_capability",
        format_func=lambda value: (
            "全部" if value == "全部" else CAPABILITY_LABELS.get(value, value)
        ),
    )
    status = filters[2].selectbox(
        "状态",
        ["全部", "success", "error", "rejected"],
        key="ledger_status",
        format_func=lambda value: (
            "全部" if value == "全部" else STATUS_LABELS.get(value, value)
        ),
    )
    model = filters[3].selectbox(
        "Model", ["全部", *models], key="ledger_model"
    )

    time_filters = st.columns(3)
    since_iso = time_filters[0].text_input(
        "起始时间（ISO，可空）", key="ledger_since"
    )
    until_iso = time_filters[1].text_input(
        "结束时间（ISO，可空）", key="ledger_until"
    )
    page_size = time_filters[2].selectbox(
        "每页条数", [20, 50, 100], key="ledger_page_size"
    )
    sort = st.selectbox(
        "排序",
        list(SORT_LABELS),
        key="ledger_sort",
        format_func=lambda value: SORT_LABELS[value],
    )

    offset = int(st.session_state.get("ledger_offset", 0))
    query = AICallLedgerQuery(
        source_feature=None if source_feature == "全部" else source_feature,
        capability=None if capability == "全部" else capability,
        status=None if status == "全部" else status,
        provider="qwen",
        model=None if model == "全部" else model,
        since_iso=since_iso.strip() or None,
        until_iso=until_iso.strip() or None,
        sort=sort,
        limit=int(page_size),
        offset=offset,
    )
    try:
        page = service.query(query)
    except AILedgerError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"查询 AI 调用台账失败：{exc}（其他页面不受影响）")
        return

    if not page.entries:
        st.info("当前筛选条件下没有任何 AI 调用记录。")
        return

    total_pages = (page.total + page.limit - 1) // page.limit
    current_page = offset // page.limit + 1
    navigation = st.columns([1, 2, 1])
    if navigation[0].button(
        "上一页", disabled=offset <= 0, key="ledger_prev", use_container_width=True
    ):
        st.session_state["ledger_offset"] = max(offset - page.limit, 0)
        st.rerun()
    navigation[1].caption(
        f"共 {page.total} 条；第 {current_page} / {max(total_pages, 1)} 页"
    )
    if navigation[2].button(
        "下一页",
        disabled=offset + page.limit >= page.total,
        key="ledger_next",
        use_container_width=True,
    ):
        st.session_state["ledger_offset"] = offset + page.limit
        st.rerun()

    for entry in page.entries:
        _render_entry(entry)
