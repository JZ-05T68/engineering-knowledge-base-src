"""Validation tests for the shared direct page-number action."""

from __future__ import annotations

import pytest

from src.page_jump_ui import parse_page_jump


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("1", 1),
        ("3353", 3353),
        (" 2874 ", 2874),
        ("0002", 2),
    ],
)
def test_parse_page_jump_accepts_supported_integer_forms(
    raw_value: str, expected: int
) -> None:
    page_number, error = parse_page_jump(raw_value, 3353)

    assert page_number == expected
    assert error is None


@pytest.mark.parametrize(
    "raw_value",
    [
        "0",
        "3354",
        "99999999999999999999999999999999999999999999999999",
        "",
        "   ",
        "不是数字",
        "2.5",
    ],
)
def test_parse_page_jump_rejects_invalid_values_without_a_target(
    raw_value: str,
) -> None:
    page_number, error = parse_page_jump(raw_value, 3353)

    assert page_number is None
    assert error is not None
    assert "1 到 3353" in error
