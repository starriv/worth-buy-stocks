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
        spread, top, bot = B.quintile_spread(pairs)
        self.assertGreater(spread, 0)
        # 10 个样本 -> 五分位各 2 个：top=(8+9)/2，bot=(0+1)/2
        self.assertEqual((top, bot), (8.5, 0.5))

    def test_quintile_too_few_is_none(self):
        self.assertIsNone(B.quintile_spread([(1, 1.0)] * 5))


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
