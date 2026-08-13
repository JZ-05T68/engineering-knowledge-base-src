"""Canonical geometry helpers for structured image-region notes.

These pure functions were validated in the streamlit-image-coordinates
compatibility experiment (experiments/streamlit_image_coordinates_compat).
The experiment imports them back from this module so there is exactly one
implementation. No Streamlit, database or filesystem dependencies here.
"""

from __future__ import annotations

import math

MIN_RECT_SIZE = 1  # minimum width/height in original pixels


def make_component_key(
    document_id: int,
    page_id: int,
    mode: str = "region",
    anchor_version: int = 0,
) -> str:
    """Build a stable, page-scoped component key.

    The key encodes the owning document, page, interaction mode and an anchor
    version so that switching pages never leaks component state. The component
    itself rescales to the available width (``use_column_width="always"``), so
    no display-width token is embedded in the key.
    """
    if not mode or not mode.replace("_", "").isalnum():
        raise ValueError(f"非法 key 模式: {mode!r}")
    return f"sic_{mode}_doc{document_id}_pg{page_id}_v{anchor_version}"


def normalize_original_rect(
    x0: object,
    y0: object,
    x1: object,
    y1: object,
    image_width: int,
    image_height: int,
) -> dict[str, int]:
    """Normalize caller-supplied original-image pixel coordinates.

    Rules (frozen by the v0.3.0 design and the component experiment):
    - coordinates must be integers (bool excluded);
    - corners are re-ordered so reversed drags work;
    - out-of-range values are clamped into the image bounds;
    - a rectangle smaller than MIN_RECT_SIZE after clamping is rejected;
    - image dimensions must be positive.
    """
    if image_width <= 0 or image_height <= 0:
        raise ValueError("原始图像尺寸无效")

    px0 = _require_int(x0, "x0")
    py0 = _require_int(y0, "y0")
    px1 = _require_int(x1, "x1")
    py1 = _require_int(y1, "y1")

    left, right = sorted((px0, px1))
    top, bottom = sorted((py0, py1))

    left = max(0, min(left, image_width))
    right = max(0, min(right, image_width))
    top = max(0, min(top, image_height))
    bottom = max(0, min(bottom, image_height))

    if right - left < MIN_RECT_SIZE or bottom - top < MIN_RECT_SIZE:
        raise ValueError("图片区域无效：面积为零或过小")

    return {"x0": left, "y0": top, "x1": right, "y1": bottom}


def normalize_display_rect(
    x1: object,
    y1: object,
    x2: object,
    y2: object,
) -> tuple[float, float, float, float]:
    """Validate and order a raw drag rectangle in display pixels.

    Accepts any drag direction. Raises ValueError on missing/non-numeric fields
    or on a zero-area drag (rejected before scaling, never silently accepted).
    """
    fx1 = _to_float(x1, "x1")
    fy1 = _to_float(y1, "y1")
    fx2 = _to_float(x2, "x2")
    fy2 = _to_float(y2, "y2")
    left, right = sorted((fx1, fx2))
    top, bottom = sorted((fy1, fy2))
    if right - left < MIN_RECT_SIZE or bottom - top < MIN_RECT_SIZE:
        raise ValueError("拖拽面积为零或过小，已拒绝")
    return left, top, right, bottom


def display_to_original(
    x1: object,
    y1: object,
    x2: object,
    y2: object,
    display_width: int,
    display_height: int,
    orig_width: int,
    orig_height: int,
) -> dict[str, int]:
    """Convert a display-space drag rectangle to original PNG pixel coordinates.

    Uses the *actual* displayed size reported by the component, rounds
    half-away-from-zero, clamps into the original image bounds and rejects
    rectangles that collapse below MIN_RECT_SIZE original pixels.
    """
    if display_width <= 0 or display_height <= 0:
        raise ValueError("显示尺寸无效")
    if orig_width <= 0 or orig_height <= 0:
        raise ValueError("原始图像尺寸无效")

    left, top, right, bottom = normalize_display_rect(x1, y1, x2, y2)

    scale_x = orig_width / display_width
    scale_y = orig_height / display_height

    def conv(value: float, scale: float, limit: int) -> int:
        pixel = round_half_away(value * scale)
        return max(0, min(pixel, limit))

    x0 = conv(left, scale_x, orig_width)
    x1o = conv(right, scale_x, orig_width)
    y0 = conv(top, scale_y, orig_height)
    y1o = conv(bottom, scale_y, orig_height)

    if x1o - x0 < MIN_RECT_SIZE or y1o - y0 < MIN_RECT_SIZE:
        raise ValueError("换算后矩形无效（面积为零），已拒绝")

    return {"x0": x0, "y0": y0, "x1": x1o, "y1": y1o}


def round_half_away(value: float) -> int:
    """Round to nearest integer, halves away from zero (deterministic)."""
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数: {value!r}")
    return value


def _to_float(value: object, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} 缺失")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是数字: {value!r}") from exc
