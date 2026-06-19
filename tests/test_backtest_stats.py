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


if __name__ == "__main__":
    unittest.main()
