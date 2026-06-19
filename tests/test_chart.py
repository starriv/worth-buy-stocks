#!/usr/bin/env python3
"""chart.py 的离线单元测试：渲染形状、颜色开关、边界，不触网。

运行：python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import chart as C  # noqa: E402


def _bar(t, o, h, l, c):
    return {"t": f"2026-01-{t:02d}T04:00:00Z", "o": o, "h": h, "l": l, "c": c, "v": 1}


class TestRender(unittest.TestCase):
    def test_line_count_and_header(self):
        bars = [_bar(2, 10, 12, 9, 11), _bar(3, 11, 13, 10, 12)]
        out = C.render("AAPL", bars, count=30, rows=14, color=False)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("AAPL 日K"))
        # 1 头 + rows 行图 + 1 底轴
        self.assertEqual(len(lines), 1 + 14 + 1)

    def test_color_toggle(self):
        bars = [_bar(2, 10, 12, 9, 11)]
        self.assertIn(C.GREEN, C.render("X", bars, color=True))
        self.assertNotIn(C.GREEN, C.render("X", bars, color=False))

    def test_down_candle_is_red(self):
        bars = [_bar(2, 11, 11.5, 8, 9)]  # 收 < 开 -> 跌
        self.assertIn(C.RED, C.render("X", bars, color=True))

    def test_flat_price_no_crash(self):
        bars = [_bar(2, 10, 10, 10, 10)]
        out = C.render("X", bars, color=False)
        self.assertIn("X 日K", out)


class TestFillSubrows(unittest.TestCase):
    def test_flat_span_returns_middle(self):
        self.assertEqual(C._fill_subrows(5, 5, 5, 5, 28), {14})

    def test_full_range_fills_all(self):
        self.assertEqual(C._fill_subrows(0, 10, 10, 0, 28), set(range(28)))


if __name__ == "__main__":
    unittest.main()
