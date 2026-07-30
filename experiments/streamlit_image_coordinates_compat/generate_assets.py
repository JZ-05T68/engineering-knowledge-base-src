"""Generate synthetic test images for the compat experiment.

Images are artificial (grid + tick labels + labelled rectangles), never derived
from real project material. Run with the compat venv:

    python generate_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).parent / "assets"

GRID = 100  # px between grid lines
RECTS = [  # (x0, y0, x1, y1, label, color)
    (100, 100, 300, 250, "R1", "#d62728"),
    (400, 300, 620, 480, "R2", "#1f77b4"),
    (150, 600, 350, 800, "R3", "#2ca02c"),
]


def render(size: tuple[int, int], name: str, rotate: int = 0) -> None:
    """Draw grid/ticks/rects, optionally rotate the finished image."""
    width, height = size
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)

    for x in range(0, width, GRID):
        draw.line([(x, 0), (x, height)], fill="#dddddd")
        draw.text((x + 2, 2), str(x), fill="#888888")
    for y in range(0, height, GRID):
        draw.line([(0, y), (width, y)], fill="#dddddd")
        draw.text((2, y + 2), str(y), fill="#888888")

    for x0, y0, x1, y1, label, color in RECTS:
        if x1 <= width and y1 <= height:
            draw.rectangle([x0, y0, x1, y1], outline=color, width=4)
            draw.text((x0 + 6, y0 + 6), f"{label} ({x0},{y0})-({x1},{y1})", fill=color)

    draw.text((10, height - 24), f"{name} {width}x{height}", fill="black")

    if rotate:
        img = img.rotate(rotate, expand=True)  # final stored image, like a rendered page

    ASSETS.mkdir(exist_ok=True)
    img.save(ASSETS / f"{name}.png")
    print(f"saved {ASSETS / (name + '.png')} {img.size}")


if __name__ == "__main__":
    render((800, 1200), "portrait_test")
    render((1600, 900), "landscape_test")
    render((800, 1200), "rotated_test", rotate=90)  # stored 1200x800, content rotated
