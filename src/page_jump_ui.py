"""Shared, ordinary-language page-number jump controls."""

from __future__ import annotations

import re

import streamlit as st

_INTEGER_PATTERN = re.compile(r"[0-9]+")


def parse_page_jump(raw_value: str, total_pages: int) -> tuple[int | None, str | None]:
    """Validate one action-only page number without changing navigation state."""

    value = raw_value.strip()
    if not value:
        return None, f"请输入页码。可以输入 1 到 {total_pages} 之间的整数。"
    if _INTEGER_PATTERN.fullmatch(value) is None:
        return None, f"页码只能输入整数。请输入 1 到 {total_pages} 之间的页码。"
    page_number = int(value)
    if page_number < 1 or page_number > total_pages:
        return None, f"没有这一页。请输入 1 到 {total_pages} 之间的页码。"
    return page_number, None


def render_page_jump(*, total_pages: int, key_prefix: str) -> int | None:
    """Render an inline jump action and return a valid submitted page number."""

    with st.form(f"{key_prefix}_form", border=False):
        label_column, input_column, total_column, button_column = st.columns(
            [1.15, 1.5, 1, 0.8], vertical_alignment="bottom"
        )
        label_column.markdown("跳转到页码")
        raw_value = input_column.text_input(
            "输入页码",
            key=f"{key_prefix}_input",
            placeholder="例如 2874",
            label_visibility="collapsed",
        )
        total_column.markdown(f"共 {total_pages} 页")
        submitted = button_column.form_submit_button(
            "跳转", type="primary", use_container_width=True
        )
    if not submitted:
        return None
    page_number, error = parse_page_jump(raw_value, total_pages)
    if error is not None:
        st.warning(error)
        return None
    return page_number


__all__ = ["parse_page_jump", "render_page_jump"]
