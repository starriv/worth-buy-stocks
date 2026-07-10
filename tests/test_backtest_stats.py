#!/usr/bin/env python3
"""回测统计原语的单元测试（backtest_common：秩相关 / 分桶 / 五分位）。

这些纯函数过去内嵌在回测脚本里、无测试；并列秩与零方差等边界最易出错。
不触网（不调用 load_panel）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backtest_common as B  # noqa: E402
import backtest_factor_ic as BF  # noqa: E402


class TestRanks(unittest.TestCase):
    def test_avg_ranks_no_ties(self):
        self.assertEqual(B.avg_ranks([30, 10, 20]), [3.0, 1.0, 2.0])

    def test_avg_ranks_with_ties(self):
        # 两个并列取平均秩 (1+2)/2 = 1.5
        self.assertEqual(B.avg_ranks([5, 5, 9]), [1.5, 1.5, 3.0])


class TestSpearman(unittest.TestCase):
    def test_perfect_positive(self):
        self.assertAlmostEqual(B.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def test_perfect_negative(self):
        self.assertAlmostEqual(B.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_too_few_or_flat_is_none(self):
        self.assertIsNone(B.spearman([1, 2], [1, 2]))        # n<3
        self.assertIsNone(B.spearman([1, 2, 3], [5, 5, 5]))  # 零方差


class TestMeanT(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(B.mean_t([]))

    def test_mean_and_n(self):
        m, t, n = B.mean_t([0.1, 0.1, 0.1])
        self.assertAlmostEqual(m, 0.1)
        self.assertEqual(n, 3)
        self.assertGreater(t, 1e6)          # 近零方差 -> t 极大（浮点噪声下非精确 inf）


class TestBuckets(unittest.TestCase):
    def test_bucket_mean_range(self):
        pairs = [(50, 1.0), (65, 2.0), (80, 3.0), (90, 5.0)]
        m, n = B.bucket_mean(pairs, 75, 1e9)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(m, 4.0)       # (3+5)/2

    def test_bucket_empty(self):
        self.assertEqual(B.bucket_mean([(50, 1.0)], 75, 1e9), (None, 0))

    def test_quintile_spread(self):
        pairs = [(i, float(i)) for i in range(10)]  # 完全单调
        gross, net, top, bot = B.quintile_spread(pairs)
        self.assertGreater(gross, 0)
        # 10 个样本 -> 五分位各 2 个：top=(8+9)/2，bot=(0+1)/2
        self.assertEqual((top, bot), (8.5, 0.5))
        # 净价差 = 毛 − 4×单边成本（默认 10bp → 0.4%）
        self.assertAlmostEqual(net, gross - 0.4)

    def test_quintile_net_respects_cost_param(self):
        pairs = [(i, float(i)) for i in range(10)]
        gross, net, _, _ = B.quintile_spread(pairs, cost_bps_oneway=25.0)
        self.assertAlmostEqual(gross - net, 1.0)

    def test_quintile_too_few_is_none(self):
        self.assertIsNone(B.quintile_spread([(1, 1.0)] * 5))


class TestBootstrapCI(unittest.TestCase):
    def test_deterministic_with_fixed_seed(self):
        vals = [0.1, -0.05, 0.2, 0.08, -0.02, 0.15]
        self.assertEqual(B.bootstrap_ci(vals), B.bootstrap_ci(vals))

    def test_interval_brackets_sample_mean(self):
        vals = [0.1, -0.05, 0.2, 0.08, -0.02, 0.15, 0.05, 0.12]
        lo, hi = B.bootstrap_ci(vals)
        mean = sum(vals) / len(vals)
        self.assertLess(lo, mean)
        self.assertGreater(hi, mean)

    def test_constant_series_degenerate_interval(self):
        lo, hi = B.bootstrap_ci([0.5, 0.5, 0.5, 0.5])
        self.assertEqual((lo, hi), (0.5, 0.5))

    def test_too_few_is_none(self):
        self.assertIsNone(B.bootstrap_ci([0.1, 0.2]))


class TestSectorDemean(unittest.TestCase):
    def test_removes_sector_mean_keeps_within_sector_order(self):
        triples = [
            (90, 10.0, "tech"), (80, 6.0, "tech"),     # tech 均值 8
            (70, -2.0, "energy"), (60, -6.0, "energy"),  # energy 均值 -4
        ]
        out = dict((s, f) for s, f in B.sector_demean(triples))
        self.assertAlmostEqual(out[90], 2.0)
        self.assertAlmostEqual(out[80], -2.0)
        self.assertAlmostEqual(out[70], 2.0)
        self.assertAlmostEqual(out[60], -2.0)
        # 行业间差异被消除：两行业去均值后的组内均值都为 0
        self.assertAlmostEqual(out[90] + out[80], 0.0)
        self.assertAlmostEqual(out[70] + out[60], 0.0)

    def test_singleton_sector_is_dropped(self):
        triples = [(90, 10.0, "tech"), (80, 6.0, "tech"), (70, 5.0, "lonely")]
        out = B.sector_demean(triples)
        self.assertEqual(len(out), 2)
        self.assertNotIn(70, [s for s, _ in out])


class TestUniverseRules(unittest.TestCase):
    def test_every_universe_symbol_has_sector(self):
        missing = [s for s in B.UNIVERSE if s not in B.SECTOR]
        self.assertEqual(missing, [])

    def test_basket_size_and_sector_coverage(self):
        self.assertGreaterEqual(len(B.ALIVE), 120)
        sectors = {}
        for s in B.ALIVE:
            sectors.setdefault(B.SECTOR[s], []).append(s)
        self.assertGreaterEqual(len(sectors), 10)
        for name, syms in sectors.items():
            self.assertGreaterEqual(len(syms), 7, f"行业 {name} 票数不足")

    def test_bankrupt_is_subset_of_delisted(self):
        self.assertTrue(B.BANKRUPT.issubset(set(B.DELISTED)))


class TestPanelReturns(unittest.TestCase):
    def test_fwd_return_uses_last_available_post_entry_close(self):
        bars = {
            "SPY": [
                {"t": "2026-01-01T00:00:00Z", "c": 400},
                {"t": "2026-01-02T00:00:00Z", "c": 401},
                {"t": "2026-01-05T00:00:00Z", "c": 402},
            ],
            "AAPL": [
                {"t": "2026-01-01T00:00:00Z", "c": 100},
                {"t": "2026-01-02T00:00:00Z", "c": 50},
            ],
        }
        panel = B.Panel(bars, "iex", "split")
        self.assertEqual(panel.fwd_return_pct("AAPL", 0, 2), -50.0)

    @staticmethod
    def _panel_with_halted(sym):
        """sym 在第 3 个交易日后停牌（最后价 60，入场价 100）。"""
        bars = {
            "SPY": [{"t": f"2026-01-0{d}T00:00:00Z", "c": 400 + d} for d in range(1, 7)],
            sym: [
                {"t": "2026-01-01T00:00:00Z", "c": 100},
                {"t": "2026-01-02T00:00:00Z", "c": 80},
                {"t": "2026-01-03T00:00:00Z", "c": 60},
            ],
        }
        return B.Panel(bars, "iex", "split")

    def test_bankrupt_symbol_counts_as_total_loss_after_halt(self):
        panel = self._panel_with_halted("SIVB")  # SIVB ∈ BANKRUPT
        self.assertEqual(panel.fwd_return_pct("SIVB", 0, 5), -100.0)

    def test_merged_symbol_keeps_last_close_terminal_value(self):
        panel = self._panel_with_halted("ATVI")  # 被并购：溢价退出，最后价即终值
        self.assertEqual(panel.fwd_return_pct("ATVI", 0, 5), -40.0)

    def test_bankrupt_symbol_still_trading_uses_real_close(self):
        panel = self._panel_with_halted("SIVB")
        # 停牌前（target 日仍有价）：正常口径，不提前记 -100
        self.assertEqual(panel.fwd_return_pct("SIVB", 0, 2), -40.0)

    def test_bankrupt_symbol_missing_entry_price_is_none(self):
        panel = self._panel_with_halted("SIVB")
        # 入场日在停牌之后：无 c0，返回 None 而非 -100
        self.assertIsNone(panel.fwd_return_pct("SIVB", 4, 1))


class TestFactorICFeatures(unittest.TestCase):
    def test_trend_factor_is_computed_outside_alpha_breakdown(self):
        row = {
            "ma": {
                "MA20": 120, "MA60": 110, "MA200": 100,
                "above_MA60": True, "MA60_rising": True,
                "above_MA200": True,
            },
            "weekly": {"bearish_alignment": False},
            "score": {
                "factor_breakdown": {
                    "momentum": {"score_pct": 80},
                    "rel_strength": {"score_pct": 70},
                    "efficiency": {"score_pct": 60},
                },
                "confirmation": {
                    "technical_pct": 50,
                    "volume_pct": 40,
                    "trend_quality_pct": 55,
                },
                "composite": 75,
            },
        }
        feats = BF._features(row)
        self.assertEqual(feats["trend"], 100)
        self.assertEqual(feats["trend_quality"], 55)


if __name__ == "__main__":
    unittest.main()
