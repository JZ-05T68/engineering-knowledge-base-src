"""Pure coordinate-mapping helpers for the streamlit-image-coordinates compat experiment.

All functions are deterministic and free of Streamlit / database dependencies so
they can be unit-tested in isolation and later reused by the v0.3.0 service layer.
"""

from __future__ import annotations

MIN_RECT_SIZE = 1  # minimum width/height in original pixels


def make_component_key(
    document_id: int,
    page_id: int,
    mode: str = "region",
    anchor_version: int = 0,
) -> str:
    """Build a stable, page-scoped component key.

    The key encodes the owning document, page, interaction mode and an anchor
    version so that:
    - switching pages always yields a different key (no cross-page state bleed);
    - re-framing a region can bump ``anchor_version`` to force a clean component.
    """
    if not mode or not mode.replace("_", "").isalnum():
        raise ValueError(f"非法 key 模式: {mode!r}")
    return f"sic_{mode}_doc{document_id}_pg{page_id}_v{anchor_version}"


def _to_float(value: object, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} 缺失")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是数字: {value!r}") from exc


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

    Rules (frozen by the v0.3.0 design):
    - corners are re-ordered (any drag direction works);
    - scaling uses the *actual* displayed size reported by the component, which
      may differ from the requested width (browser/DPR rounding);
    - coordinates are rounded to the nearest integer (round-half-away-from-zero);
    - results are clamped into the original image bounds (out-of-image drags are
      cropped, never passed through);
    - a rectangle that ends up smaller than MIN_RECT_SIZE in original pixels is
      rejected with ValueError.
    """
    if display_width <= 0 or display_height <= 0:
        raise ValueError("显示尺寸无效")
    if orig_width <= 0 or orig_height <= 0:
        raise ValueError("原始图像尺寸无效")

    left, top, right, bottom = normalize_display_rect(x1, y1, x2, y2)

    scale_x = orig_width / display_width
    scale_y = orig_height / display_height

    def conv(value: float, scale: float, limit: int) -> int:
        pixel = _round_half_away(value * scale)
        return max(0, min(pixel, limit))

    x0 = conv(left, scale_x, orig_width)
    x1o = conv(right, scale_x, orig_width)
    y0 = conv(top, scale_y, orig_height)
    y1o = conv(bottom, scale_y, orig_height)

    if x1o - x0 < MIN_RECT_SIZE or y1o - y0 < MIN_RECT_SIZE:
        raise ValueError("换算后矩形无效（面积为零），已拒绝")

    return {"x0": x0, "y0": y0, "x1": x1o, "y1": y1o}


def _round_half_away(value: float) -> int:
    """Round to nearest integer, halves away from zero (deterministic)."""
    import math

    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))
