"""Compatibility shim: the canonical geometry now lives in ``src.note_geometry``.

The experiment keeps importing ``coordinate_mapping`` so its app and tests run
unchanged, while there is exactly one implementation (validated here, moved to
src for the note service).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.note_geometry import (  # noqa: E402,F401
    MIN_RECT_SIZE,
    display_to_original,
    make_component_key,
    normalize_display_rect,
    normalize_original_rect,
)
