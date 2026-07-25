"""Pure eligibility policy deciding whether a page may receive local OCR.

This module only answers the eligibility question. It never calls an OCR
engine, never touches the filesystem or the database, never reads or writes
page status fields, and never mutates its inputs. Manual-review state
(``review_status``/``processing_status``) and user Markdown are deliberately
excluded from the decision: OCR text supplements the original material and
must not be coupled to the human-authored content workflow.
"""

from __future__ import annotations

from src.pdf_service import PdfService

__all__ = [
    "has_reusable_ocr_text",
    "is_page_eligible_for_ocr",
]


def has_reusable_ocr_text(ocr_text: str | None) -> bool:
    """Whether existing OCR text already counts as usable page content.

    Whitespace-only or punctuation-only OCR text carries no effective
    characters and is treated as absent, so it does not block eligibility.
    """

    if not ocr_text:
        return False
    return PdfService.effective_text_length(ocr_text) > 0


def is_page_eligible_for_ocr(
    *,
    extracted_text: str | None,
    ocr_text: str | None,
    image_available: bool,
    minimum_text_length: int,
) -> bool:
    """Decide whether one page may be offered local OCR.

    A page is eligible only when all of the following hold:

    - the rendered page image is available (OCR reads the PNG, not the PDF);
    - no reusable OCR text exists yet (no implicit re-run by default);
    - the extracted PDF text layer stays below ``minimum_text_length``
      effective characters, the exact same counting rule and threshold
      semantics as the import-time ``needs_review`` decision.

    ``extracted_text`` values of ``None``, empty, whitespace-only, or
    punctuation-only all count as zero effective characters. The decision is
    deterministic and performs no I/O.
    """

    if minimum_text_length < 0:
        raise ValueError("最少有效文本长度不能小于 0。")
    if not image_available:
        return False
    if has_reusable_ocr_text(ocr_text):
        return False
    effective_length = PdfService.effective_text_length(extracted_text or "")
    return effective_length < minimum_text_length
