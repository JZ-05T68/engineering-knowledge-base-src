"""Tests for the pure page OCR eligibility policy in ``src.ocr_policy``."""

from __future__ import annotations

import pytest

from src.ocr_policy import has_reusable_ocr_text, is_page_eligible_for_ocr

DEFAULT_MINIMUM = 20


def eligible(extracted_text, ocr_text=None, image_available=True, minimum=DEFAULT_MINIMUM):
    """Call the policy with the project default threshold unless overridden."""

    return is_page_eligible_for_ocr(
        extracted_text=extracted_text,
        ocr_text=ocr_text,
        image_available=image_available,
        minimum_text_length=minimum,
    )


def test_none_extracted_text_is_eligible() -> None:
    assert eligible(None) is True


def test_empty_extracted_text_is_eligible() -> None:
    assert eligible("") is True


def test_whitespace_only_extracted_text_is_eligible() -> None:
    assert eligible("   \n\t \r\n  ") is True


def test_punctuation_only_extracted_text_is_eligible() -> None:
    assert eligible("--。。，、··__") is True


def test_mixed_cjk_latin_below_threshold_is_eligible() -> None:
    assert eligible("芯片 ab", minimum=20) is True


def test_one_below_threshold_is_eligible() -> None:
    assert eligible("a" * 19, minimum=20) is True


def test_exactly_at_threshold_is_not_eligible() -> None:
    assert eligible("a" * 20, minimum=20) is False


def test_one_above_threshold_is_not_eligible() -> None:
    assert eligible("a" * 21, minimum=20) is False


def test_unavailable_image_blocks_eligibility() -> None:
    assert eligible("", image_available=False) is False
    assert eligible(None, image_available=False) is False


def test_existing_effective_ocr_text_blocks_eligibility() -> None:
    assert eligible("", ocr_text="识别出的文字") is False


def test_whitespace_only_ocr_text_does_not_block() -> None:
    assert eligible("", ocr_text="  \n\t ") is True


def test_punctuation_only_ocr_text_does_not_block() -> None:
    assert eligible("", ocr_text="---。。。") is True


def test_cjk_characters_count_toward_threshold() -> None:
    assert eligible("芯样本", minimum=3) is False
    assert eligible("芯样", minimum=3) is True


def test_digits_and_latin_letters_count_toward_threshold() -> None:
    assert eligible("abc123", minimum=6) is False
    assert eligible("abc123", minimum=7) is True


def test_inputs_are_not_modified() -> None:
    extracted = "  短文本  "
    ocr = "  "
    eligible(extracted, ocr_text=ocr)
    assert extracted == "  短文本  "
    assert ocr == "  "


def test_repeated_calls_are_deterministic() -> None:
    first = eligible("abc", minimum=20)
    second = eligible("abc", minimum=20)
    assert first is second is True


def test_zero_threshold_never_offers_ocr() -> None:
    # Mirrors needs_review semantics: effective_length < 0 is impossible.
    assert eligible("", minimum=0) is False


def test_negative_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="最少有效文本长度"):
        eligible("", minimum=-1)


def test_has_reusable_ocr_text_semantics() -> None:
    assert has_reusable_ocr_text(None) is False
    assert has_reusable_ocr_text("") is False
    assert has_reusable_ocr_text("   \n") is False
    assert has_reusable_ocr_text("--。") is False
    assert has_reusable_ocr_text("结果1") is True
