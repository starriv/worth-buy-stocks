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


class TestPlanLevels(unittest.TestCase):
    PLAN = {
        "suggested_entry_price": 250.0,
        "stop_loss_price": 238.0,
        "take_profit_price": 274.0,
        "take_profit_2_price": 286.0,
        "pullback_entry_price": 245.0,
    }

    def test_extracts_all_levels(self):
        levels = C.plan_levels(self.PLAN)
        self.assertEqual([(lv[0], lv[1]) for lv in levels],
                         [("入场", 250.0), ("止损", 238.0), ("止盈1", 274.0), ("止盈2", 286.0)])

    def test_entry_falls_back_to_pullback(self):
        plan = dict(self.PLAN, suggested_entry_price=None)
        labels = [lv[0] for lv in C.plan_levels(plan)]
        self.assertIn("回踩入场", labels)
        self.assertNotIn("入场", labels)

    def test_industry_convention_colors(self):
        # 行业惯例：入场蓝、止损红、止盈绿/亮绿；回踩入场（待触发）黄
        colors = {lv[0]: lv[2] for lv in C.plan_levels(self.PLAN)}
        self.assertEqual(colors["入场"], C.BLUE)
        self.assertEqual(colors["止损"], C.RED)
        self.assertEqual(colors["止盈1"], C.GREEN)
        self.assertEqual(colors["止盈2"], C.BRIGHT_GREEN)
        pullback = C.plan_levels(dict(self.PLAN, suggested_entry_price=None))
        self.assertEqual({lv[0]: lv[2] for lv in pullback}["回踩入场"], C.YELLOW)

    def test_missing_and_invalid_fields_skipped(self):
        self.assertEqual(C.plan_levels({"stop_loss_price": -1}), [])
        self.assertEqual(C.plan_levels({}), [])
        self.assertEqual(C.plan_levels(None), [])


class TestLoadPlan(unittest.TestCase):
    def _write(self, obj):
        import json
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, f.name)
        json.dump(obj, f)
        f.close()
        return f.name

    def test_result_json_shape(self):
        plan = {"stop_loss_price": 238.0}
        path = self._write({"symbols": {"AAPL": {"score": {"trade_plan": plan}}}})
        self.assertEqual(C.load_plan(path, "aapl"), plan)

    def test_single_symbol_without_flag(self):
        plan = {"stop_loss_price": 238.0}
        path = self._write({"symbols": {"AAPL": {"score": {"trade_plan": plan}}}})
        self.assertEqual(C.load_plan(path), plan)

    def test_bare_trade_plan(self):
        plan = {"stop_loss_price": 238.0}
        self.assertEqual(C.load_plan(self._write(plan)), plan)
        self.assertEqual(C.load_plan(self._write({"trade_plan": plan})), plan)

    def test_unavailable_plan_returns_empty(self):
        path = self._write({"status": "unavailable", "reason": "缺价"})
        self.assertEqual(C.load_plan(path), {})

    def test_unknown_symbol_raises(self):
        path = self._write({"symbols": {"AAPL": {}}})
        with self.assertRaises(RuntimeError):
            C.load_plan(path, "MSFT")


class TestRenderLevels(unittest.TestCase):
    BARS = [_bar(d, 100 + d, 104 + d, 98 + d, 102 + d) for d in range(2, 12)]

    def test_labels_and_line_drawn(self):
        levels = [("入场", 108.0, C.COLOR_ENTRY), ("止损", 95.0, C.COLOR_STOP)]
        out = C.render("X", self.BARS, rows=14, color=False, levels=levels)
        self.assertIn("◂ 入场 108.00", out)
        self.assertIn("◂ 止损 95.00", out)
        self.assertIn(C.LEVEL_CHAR, out)

    def test_scale_extends_to_levels(self):
        # 止损 90 低于所有 low（最低 100），底部刻度应扩到 90
        out = C.render("X", self.BARS, rows=14, color=False,
                       levels=[("止损", 90.0, C.COLOR_STOP)])
        self.assertIn("90.00 ┤", out)

    def test_same_row_labels_merge(self):
        # 两条价位线价格几乎相同 -> 同一行，标签合并
        out = C.render("X", self.BARS, rows=14, color=False,
                       levels=[("止盈1", 120.0, C.COLOR_TP1), ("止盈2", 120.01, C.COLOR_TP2)])
        self.assertIn("止盈2 120.01 止盈1 120.00", out)

    def test_level_color_applied(self):
        out = C.render("X", self.BARS, rows=14, color=True,
                       levels=[("止损", 95.0, C.COLOR_STOP)])
        self.assertIn(f"{C.COLOR_STOP}{C.LEVEL_CHAR}{C.RESET}", out)

    def test_no_levels_output_unchanged(self):
        with_none = C.render("X", self.BARS, rows=14, color=False)
        empty = C.render("X", self.BARS, rows=14, color=False, levels=[])
        self.assertEqual(with_none, empty)
        self.assertNotIn(C.LEVEL_CHAR, with_none)


class TestRenderHtml(unittest.TestCase):
    BARS = [_bar(2, 10, 12, 9, 11), _bar(3, 11, 13, 10, 10.5)]  # 一涨一跌

    def test_svg_and_candle_colors(self):
        out = C.render_html("AAPL", self.BARS)
        self.assertIn("<svg", out)
        self.assertIn(C.HTML_UP, out)
        self.assertIn(C.HTML_DOWN, out)
        self.assertIn("AAPL", out)
        self.assertNotIn(C.GREEN, out)  # 不混入 ANSI 码

    def test_levels_labeled_with_mapped_color(self):
        out = C.render_html("X", self.BARS, levels=[("止损", 9.5, C.COLOR_STOP)])
        self.assertIn("止损 9.50", out)
        self.assertIn(C.HTML_LEVEL[C.COLOR_STOP], out)

    def test_tooltip_and_table_present(self):
        out = C.render_html("X", self.BARS)
        self.assertIn("data-tip", out)
        self.assertIn("数据表", out)
        self.assertIn("<td>2026-01-03</td>", out)

    def test_empty_bars_raise(self):
        with self.assertRaises(RuntimeError):
            C.render_html("X", [])


class TestPriceRow(unittest.TestCase):
    def test_bounds_and_clamp(self):
        self.assertEqual(C._price_row(110, 110, 90, 14), 0)
        self.assertEqual(C._price_row(90, 110, 90, 14), 13)
        self.assertEqual(C._price_row(200, 110, 90, 14), 0)
        self.assertEqual(C._price_row(10, 110, 90, 14), 13)

    def test_flat_span_middle(self):
        self.assertEqual(C._price_row(100, 100, 100, 14), 7)


if __name__ == "__main__":
    unittest.main()
