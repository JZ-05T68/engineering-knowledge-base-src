"""Unit tests for coordinate_mapping pure functions (stdlib unittest, no DB/files)."""

from __future__ import annotations

import unittest

from coordinate_mapping import (
    MIN_RECT_SIZE,
    display_to_original,
    make_component_key,
    normalize_display_rect,
)


class KeyTests(unittest.TestCase):
    def test_stable_same_inputs(self) -> None:
        a = make_component_key(7, 42)
        b = make_component_key(7, 42)
        self.assertEqual(a, b)

    def test_different_pages_differ(self) -> None:
        self.assertNotEqual(make_component_key(7, 42), make_component_key(7, 43))

    def test_different_documents_differ(self) -> None:
        self.assertNotEqual(make_component_key(7, 42), make_component_key(8, 42))

    def test_mode_and_version_in_key(self) -> None:
        key = make_component_key(1, 2, mode="region", anchor_version=3)
        self.assertIn("region", key)
        self.assertIn("v3", key)
        self.assertNotEqual(
            make_component_key(1, 2, anchor_version=0),
            make_component_key(1, 2, anchor_version=1),
        )

    def test_bad_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_component_key(1, 2, mode="bad mode!")


class NormalizeTests(unittest.TestCase):
    def test_forward_drag(self) -> None:
        self.assertEqual(normalize_display_rect(10, 20, 30, 40), (10.0, 20.0, 30.0, 40.0))

    def test_reverse_drag_sorted(self) -> None:
        self.assertEqual(normalize_display_rect(30, 40, 10, 20), (10.0, 20.0, 30.0, 40.0))

    def test_zero_area_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_display_rect(10, 10, 10, 10)
        with self.assertRaises(ValueError):
            normalize_display_rect(10, 20, 10, 40)

    def test_tiny_area_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_display_rect(10, 10, 10 + MIN_RECT_SIZE - 0.5, 50)

    def test_missing_fields_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_display_rect(None, 10, 20, 30)
        with self.assertRaises(ValueError):
            normalize_display_rect("abc", 10, 20, 30)


class MappingTests(unittest.TestCase):
    def test_no_scaling(self) -> None:
        rect = display_to_original(100, 100, 300, 250, 800, 1200, 800, 1200)
        self.assertEqual(rect, {"x0": 100, "y0": 100, "x1": 300, "y1": 250})

    def test_integer_downscale(self) -> None:
        # displayed at half size -> doubles back
        rect = display_to_original(50, 50, 150, 125, 400, 600, 800, 1200)
        self.assertEqual(rect, {"x0": 100, "y0": 100, "x1": 300, "y1": 250})

    def test_non_integer_scale_rounding(self) -> None:
        # 800 -> 300 display: scale 2.666...
        rect = display_to_original(30, 30, 90, 90, 300, 450, 800, 1200)
        self.assertEqual(rect["x0"], round(30 * 800 / 300))
        self.assertEqual(rect["x1"], round(90 * 800 / 300))
        self.assertEqual(rect["y1"], round(90 * 1200 / 450))

    def test_reverse_drag(self) -> None:
        rect = display_to_original(300, 250, 100, 100, 800, 1200, 800, 1200)
        self.assertEqual(rect, {"x0": 100, "y0": 100, "x1": 300, "y1": 250})

    def test_out_of_bounds_clamped(self) -> None:
        rect = display_to_original(-50, -50, 900, 1300, 800, 1200, 800, 1200)
        self.assertEqual(rect, {"x0": 0, "y0": 0, "x1": 800, "y1": 1200})

    def test_zero_area_rejected(self) -> None:
        with self.assertRaises(ValueError):
            display_to_original(10, 10, 10, 10, 800, 1200, 800, 1200)

    def test_collapsed_after_scaling_rejected(self) -> None:
        # 1 display px at large downscale collapses to < MIN_RECT_SIZE original px
        with self.assertRaises(ValueError):
            display_to_original(100, 100, 100.2, 100.2, 100, 100, 800, 1200)

    def test_invalid_sizes_rejected(self) -> None:
        with self.assertRaises(ValueError):
            display_to_original(0, 0, 10, 10, 0, 600, 800, 1200)
        with self.assertRaises(ValueError):
            display_to_original(0, 0, 10, 10, 400, 600, 0, 1200)

    def test_landscape_image(self) -> None:
        rect = display_to_original(100, 50, 400, 200, 1600, 900, 1600, 900)
        self.assertEqual(rect, {"x0": 100, "y0": 50, "x1": 400, "y1": 200})


if __name__ == "__main__":
    unittest.main()
