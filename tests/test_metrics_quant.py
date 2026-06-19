#!/usr/bin/env python3
"""量化因子原语的确定性单元测试（metrics.py 新增部分）。"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import metrics as M  # noqa: E402


class TestVolReturn(unittest.TestCase):
    def test_constant_vol_zero(self):
        self.assertEqual(M.annualized_vol([10.0] * 70, 63), 0.0)

    def test_insufficient_is_none(self):
        self.assertIsNone(M.annualized_vol([1, 2, 3], 63))

    def test_annualized_return_doubling(self):
        # 半年（126 交易日）翻倍 -> 年化约 +300%
        closes = [100.0] + [200.0] * 126
        r = M.annualized_return(closes, 126)
        self.assertAlmostEqual(r, 300.0, delta=1.0)


class TestDrawdown(unittest.TestCase):
    def test_monotonic_up_zero(self):
        self.assertEqual(M.max_drawdown(list(range(1, 50)), 40), 0.0)

    def test_dip_is_negative(self):
        closes = [100, 110, 120, 90, 95]  # 峰 120 -> 谷 90 = -25%
        self.assertAlmostEqual(M.max_drawdown(closes, 5), -25.0, delta=0.01)


class TestEfficiency(unittest.TestCase):
    def test_monotonic_is_one(self):
        self.assertEqual(M.efficiency_ratio(list(range(1, 40)), 30), 1.0)

    def test_zigzag_is_low(self):
        zig = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11] * 4
        er = M.efficiency_ratio(zig, 30)
        self.assertLess(er, 0.2)


class TestRegression(unittest.TestCase):
    def test_exp_growth_high_r2_positive_slope(self):
        closes = [100 * math.exp(0.002 * i) for i in range(70)]
        reg = M.trend_regression(closes, 63)
        self.assertGreater(reg["r2"], 0.99)
        self.assertGreater(reg["ann_slope_pct"], 0)

    def test_flat_is_none_or_zero_slope(self):
        # 完全水平 -> syy=0 -> None
        self.assertIsNone(M.trend_regression([5.0] * 70, 63))


class TestMomentum121(unittest.TestCase):
    def test_known_value(self):
        # 长度 260：base=closes[-253], recent=closes[-21]
        closes = [float(i) for i in range(1, 261)]
        base, recent = closes[-253], closes[-21]
        expect = round((recent / base - 1) * 100, 2)
        self.assertEqual(M.momentum_12_1(closes), expect)

    def test_short_history_fallback(self):
        # < 253 根：退化为 base=closes[0]
        closes = [float(i) for i in range(1, 60)]
        self.assertEqual(M.momentum_12_1(closes),
                         round((closes[-21] / closes[0] - 1) * 100, 2))


class TestATR(unittest.TestCase):
    def test_constant_range(self):
        # 每根 H-L=2，且无跳空 -> ATR=2
        highs = [11] * 30
        lows = [9] * 30
        closes = [10] * 30
        self.assertAlmostEqual(M.atr(highs, lows, closes, 14), 2.0, delta=0.01)


class TestEMASeed(unittest.TestCase):
    """Phase 1: EMA seed="sma" 行为验证。"""

    def test_sma_seed_first_value(self):
        values = [3.0, 5.0, 7.0, 9.0, 11.0]
        out = M.ema_series(values, 3, seed="sma")
        # 前 3 根 = SMA(3,5,7) = 5.0
        self.assertAlmostEqual(out[0], 5.0)
        self.assertAlmostEqual(out[1], 5.0)
        self.assertAlmostEqual(out[2], 5.0)
        # 第 4 根 = 9*0.5 + 5*0.5 = 7.0 (k=2/(3+1)=0.5)
        self.assertAlmostEqual(out[3], 7.0)

    def test_sma_and_first_converge(self):
        """充分长序列后两种 seed 收敛到相近值。"""
        values = [float(x) for x in range(1, 200)]
        out_sma = M.ema_series(values, 26, seed="sma")
        out_first = M.ema_series(values, 26, seed="first")
        self.assertAlmostEqual(out_sma[-1], out_first[-1], delta=1e-5)

    def test_default_is_first(self):
        values = [1.0, 2.0, 3.0]
        default = M.ema_series(values, 2)
        explicit = M.ema_series(values, 2, seed="first")
        self.assertEqual(default, explicit)

    def test_macd_works_with_40_bars(self):
        """SMA seed 后 40 根即可返回有效 MACD（原来需 ~78）。"""
        m = M.macd([float(x) for x in range(1, 41)])
        self.assertIsNotNone(m)
        self.assertIn("DIF", m)

    def test_macd_insufficient_still_none(self):
        """不足 40 根仍返回 None。"""
        self.assertIsNone(M.macd([float(x) for x in range(1, 39)]))


class TestOBV(unittest.TestCase):
    """Phase 2: OBV 计算与背离检测。"""

    def test_rising_price_rising_vol_no_divergence(self):
        closes = [float(x) for x in range(1, 60)]
        vols = [float(x * 1000) for x in range(1, 60)]
        o = M.obv(closes, vols, 30)
        self.assertIsNotNone(o)
        self.assertIsNone(o["divergence"])  # 价量同涨，无背离

    def test_bearish_divergence(self):
        """价格近 30 日整体上涨但 OBV 趋势向下 → bearish divergence。"""
        n = 100
        # 前 70 根：稳定上涨+放量（OBV 累积高）
        closes = [float(x) for x in range(1, 71)]
        vols = [1000.0] * 70
        # 后 30 根：价格高位震荡微涨，但跌日量远大于涨日量 → OBV 趋势下行
        base = 70.0
        for i in range(15):
            closes.append(base + i * 0.1)   # 涨
            closes.append(base + i * 0.05)  # 跌
            vols.append(10.0)   # 涨日量小
            vols.append(500.0)  # 跌日量大 → OBV 净减
        o = M.obv(closes, vols, 30)
        self.assertEqual(o["divergence"], "bearish")

    def test_bullish_divergence(self):
        """价格近 30 日整体下跌但 OBV 趋势向上 → bullish divergence。"""
        n = 100
        # 前 70 根：稳定下跌+缩量（OBV 累积低）
        closes = [float(x) for x in range(70, 0, -1)]
        vols = [10.0] * 70
        # 后 30 根：价格低位震荡微跌，但涨日量远大于跌日量 → OBV 趋势上行
        base = 1.0
        for i in range(15):
            closes.append(base - i * 0.1)   # 跌
            closes.append(base - i * 0.05)  # 涨
            vols.append(10.0)   # 跌日量小
            vols.append(500.0)  # 涨日量大 → OBV 净增
        o = M.obv(closes, vols, 30)
        self.assertEqual(o["divergence"], "bullish")

    def test_insufficient_bars_none(self):
        self.assertIsNone(M.obv([1, 2, 3], [100] * 3, 30))

    def test_flat_price_no_divergence(self):
        closes = [10.0] * 60
        vols = [1000.0] * 60
        o = M.obv(closes, vols, 30)
        self.assertIsNotNone(o)
        self.assertEqual(o["value"], 0.0)  # 所有日平价，OBV 不增不减


class TestVolumeTrend(unittest.TestCase):
    """Phase 3: 量能趋势指标。"""

    def test_ratio_ma_constant(self):
        vols = [1000.0] * 60
        r = M.volume_ratio_ma(vols, 5, 20)
        self.assertAlmostEqual(r, 1.0, delta=0.01)

    def test_ratio_ma_doubling(self):
        """近 5 日量翻倍 → ratio ≈ 2.0。"""
        vols = [500.0] * 45 + [1000.0] * 5
        r = M.volume_ratio_ma(vols, 5, 20)
        self.assertAlmostEqual(r, 2.0, delta=0.01)

    def test_ratio_ma_insufficient(self):
        self.assertIsNone(M.volume_ratio_ma([100] * 20, 5, 20))

    def test_trend_direction_rising(self):
        vols = [float(x) for x in range(1, 30)]
        self.assertEqual(M.volume_trend_direction(vols, 10), "rising")

    def test_trend_direction_falling(self):
        vols = [float(x) for x in range(30, 0, -1)]
        self.assertEqual(M.volume_trend_direction(vols, 10), "falling")

    def test_trend_direction_insufficient(self):
        self.assertIsNone(M.volume_trend_direction([1, 2], 10))

    def test_up_down_ratio_healthy(self):
        """上涨日放量+下跌日缩量 → ratio > 1。"""
        closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10]  # 交替涨跌
        # vols[i] 对应第 i 天的量：i=1 涨日→200, i=2 跌日→100, ...
        vols    = [0, 200, 100, 200, 100, 200, 100, 200, 100, 200, 100]
        r = M.up_day_volume_ratio(closes, vols, 10)
        self.assertGreater(r, 1.0)

    def test_up_down_ratio_bearish(self):
        """下跌日放量 → ratio < 1。"""
        closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10]
        # 涨日量 100, 跌日量 200
        vols    = [0, 100, 200, 100, 200, 100, 200, 100, 200, 100, 200]
        r = M.up_day_volume_ratio(closes, vols, 10)
        self.assertLess(r, 1.0)

    def test_up_down_ratio_insufficient(self):
        self.assertIsNone(M.up_day_volume_ratio([1, 2], [100, 200], 10))

    def test_up_down_ratio_all_up_returns_none(self):
        """全为上涨日 → 无下跌日比较，返回 None。"""
        closes = list(range(1, 20))
        vols = [100.0] * 19
        self.assertIsNone(M.up_day_volume_ratio(closes, vols, 10))


class TestADX(unittest.TestCase):
    """ADX 趋势强度指标。"""

    def test_constant_price_low_adx(self):
        """横盘 → ADX 低、无趋势。"""
        n = 50
        highs = [12.0] * n
        lows = [10.0] * n
        closes = [11.0] * n
        a = M.adx(highs, lows, closes, 14)
        self.assertIsNotNone(a)
        self.assertLess(a["ADX"], 25)
        self.assertFalse(a["trend_strong"])

    def test_uptrend_strong_adx(self):
        """持续上涨 → ADX > 25 且 +DI > -DI。"""
        n = 50
        highs = [float(x + 2) for x in range(1, n + 1)]
        lows = [float(x - 1) for x in range(1, n + 1)]
        closes = [float(x) for x in range(1, n + 1)]
        a = M.adx(highs, lows, closes, 14)
        self.assertIsNotNone(a)
        self.assertGreater(a["ADX"], 25)
        self.assertGreater(a["plus_DI"], a["minus_DI"])
        self.assertTrue(a["bull_trend"])
        self.assertTrue(a["trend_strong"])

    def test_downtrend_strong_adx(self):
        """持续下跌 → ADX > 25 且 -DI > +DI。"""
        n = 50
        highs = [float(x + 1) for x in range(n, 0, -1)]
        lows = [float(x - 2) for x in range(n, 0, -1)]
        closes = [float(x) for x in range(n, 0, -1)]
        a = M.adx(highs, lows, closes, 14)
        self.assertIsNotNone(a)
        self.assertGreater(a["ADX"], 25)
        self.assertGreater(a["minus_DI"], a["plus_DI"])
        self.assertFalse(a["bull_trend"])

    def test_insufficient_bars_none(self):
        self.assertIsNone(M.adx([1]*20, [1]*20, [1]*20, 14))

    def test_zigzag_low_adx(self):
        """锯齿震荡 → ADX < 20。"""
        n = 50
        highs, lows, closes = [], [], []
        for i in range(n // 2):
            highs.extend([12, 12])
            lows.extend([8, 8])
            closes.extend([12, 8])
        a = M.adx(highs, lows, closes, 14)
        self.assertIsNotNone(a)
        self.assertLess(a["ADX"], 20)


if __name__ == "__main__":
    unittest.main()
