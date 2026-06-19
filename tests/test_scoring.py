#!/usr/bin/env python3
"""确定性打分引擎的单元测试（scoring.py）。

用合成的 analyze_symbol 结果驱动，覆盖：强趋势高分、风险否决封顶、
缺失因子重归一、反波动率仓位。不触网。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import scoring as S  # noqa: E402


def _strong():
    """一个各因子都强的标的（应高分、verdict=是、无否决）。"""
    return {
        "ma": {"MA20": 110, "MA60": 105, "MA200": 100,
               "above_MA60": True, "MA60_rising": True,
               "above_MA200": True, "MA200_rising": True},
        "weekly": {"bearish_alignment": False},
        "momentum": {"m12_1_pct": 40.0, "ann_return_6m_pct": 35.0, "risk_adj_6m": 1.5},
        "relative_strength_pct": {"SPY": {"r3m_63d": 12.0, "r6m_126d": 15.0},
                                  "QQQ": {"r3m_63d": 8.0, "r6m_126d": 10.0}},
        "trend_quality": {"efficiency_30": 0.7,
                          "regression_3m": {"ann_slope_pct": 60.0, "r2": 0.9},
                          "adx": {"ADX": 35.0, "plus_DI": 30.0, "minus_DI": 12.0,
                                  "trend_strong": True, "bull_trend": True}},
        "risk": {"max_drawdown_6m_pct": -5.0},
        "macd": {"bull": True, "above_zero": True, "recent_cross": {"type": "golden"}},
        "rsi": {"RSI14": 62},
        "kdj": {"bull": True, "above_50": True},
        "volume": {
            "ratio_vs_ma20": 1.3,
            "avg_ratio_5d": 1.1,
            "up_down_vol_ratio": 1.3,
            "vol_trend_10d": "rising",
            "obv": {"divergence": None},
        },
        "volatility": {"ann_vol_3m_pct": 18.0},
    }


class TestStrong(unittest.TestCase):
    def test_high_score_buy(self):
        r = S.score(_strong())
        self.assertGreaterEqual(r["composite"], 75)
        self.assertEqual(r["verdict"], "是")
        self.assertEqual(r["risk_gates"], [])

    def test_position_sizing_present(self):
        r = S.score(_strong())
        self.assertIsNotNone(r["suggested_position_pct"])


class TestGates(unittest.TestCase):
    def test_below_falling_ma200_caps_55(self):
        a = _strong()
        a["ma"]["above_MA200"] = False
        a["ma"]["MA200_rising"] = False
        r = S.score(a)
        self.assertLessEqual(r["composite"], 55)
        self.assertTrue(any("200 日线" in g for g in r["risk_gates"]))
        self.assertNotEqual(r["verdict"], "是")

    def test_weekly_bearish_caps_50(self):
        a = _strong()
        a["weekly"]["bearish_alignment"] = True
        r = S.score(a)
        self.assertLessEqual(r["composite"], 50)

    def test_below_ma60_caps_65(self):
        a = _strong()
        a["ma"]["above_MA60"] = False
        r = S.score(a)
        self.assertLessEqual(r["composite"], 65)


class TestMarketRegimeGate(unittest.TestCase):
    def test_market_risk_off_caps_buy_to_watch(self):
        # 个股极强（本应「是」），但大盘 risk-off → 封顶 65 → 最高「观察」
        a = _strong()
        base = S.score(a)
        self.assertEqual(base["verdict"], "是")
        gated = S.score(a, market_risk_off=True)
        self.assertLessEqual(gated["composite"], 65)
        self.assertEqual(gated["verdict"], "观察")
        self.assertTrue(any("大盘 risk-off" in g for g in gated["risk_gates"]))

    def test_market_risk_on_no_gate(self):
        a = _strong()
        self.assertEqual(S.score(a, market_risk_off=None)["composite"],
                         S.score(a)["composite"])
        self.assertEqual(S.score(a, market_risk_off=False)["composite"],
                         S.score(a)["composite"])

    def test_build_result_threads_spy_regime(self):
        # build_result 应从 SPY 的 above_MA200 推导市场 regime 并传给每只票打分。
        import analysis as A

        def bars(path):  # path: 收盘价序列 → 最简 OHLCV bar 列表
            from datetime import date, timedelta
            d0 = date(2024, 1, 1)
            out = []
            for i, c in enumerate(path):
                d = (d0 + timedelta(days=i)).isoformat()
                out.append({"t": f"{d}T05:00:00Z", "o": c, "h": c * 1.01,
                            "l": c * 0.99, "c": c, "v": 1_000_000})
            return out

        n = 260
        up = [100.0 * (1.004 ** i) for i in range(n)]       # 强势个股
        spy_down = [400.0 * (0.999 ** i) for i in range(n)]  # SPY 长期下行 → 跌破 MA200
        data = {"WIN": bars(up), "SPY": bars(spy_down), "QQQ": bars(spy_down)}
        res = A.build_result(["WIN", "SPY", "QQQ"], data, "iex", "split")
        self.assertTrue(res.get("market_risk_off"))
        win = res["symbols"]["WIN"]["score"]
        self.assertTrue(any("大盘 risk-off" in g for g in win["risk_gates"]))
        self.assertLessEqual(win["composite"], 65)


class TestRenormalize(unittest.TestCase):
    def test_missing_factor_flagged_and_renormalized(self):
        a = _strong()
        del a["momentum"]            # 动量因子整块缺失
        a["ma"]["MA200"] = None      # 触发数据 flag
        a["ma"]["above_MA200"] = None
        a["ma"]["MA200_rising"] = None
        r = S.score(a)
        self.assertIsNone(r["factor_breakdown"]["momentum"]["score_pct"])
        self.assertTrue(any("momentum" in f for f in r["data_flags"]))
        # 仍能给出 composite（按可用权重重归一）
        self.assertIsNotNone(r["composite"])


class TestConfirmationOverlay(unittest.TestCase):
    def test_technical_volume_not_in_alpha_breakdown(self):
        r = S.score(_strong())
        self.assertNotIn("technical", r["factor_breakdown"])
        self.assertNotIn("volume_exec", r["factor_breakdown"])
        self.assertIn("technical_pct", r["confirmation"])

    def test_weak_technical_caps_buy_to_watch(self):
        a = _strong()
        # 技术全面转弱 -> confirmation 不通过 -> 即便高分也封顶为观察
        a["macd"] = {"bull": False, "above_zero": False,
                     "recent_cross": {"type": "death"}}
        a["rsi"] = {"RSI14": 40}
        a["kdj"] = {"bull": False, "above_50": False}
        r = S.score(a)
        self.assertGreaterEqual(r["composite"], 75)
        self.assertFalse(r["confirmation"]["ok"])
        self.assertEqual(r["verdict"], "观察")


class TestPositionSizing(unittest.TestCase):
    def test_higher_vol_smaller_position(self):
        lo, hi = _strong(), _strong()
        lo["volatility"]["ann_vol_3m_pct"] = 15.0
        hi["volatility"]["ann_vol_3m_pct"] = 45.0
        self.assertGreater(S.score(lo)["suggested_position_pct"],
                           S.score(hi)["suggested_position_pct"])


class TestADXScoring(unittest.TestCase):
    """ADX 在 trend_quality → confirmation.trend_quality_pct 中的作用。"""

    def test_strong_adx_contributes(self):
        """高 ADX + bull trend → trend_quality_pct 高。"""
        a = _strong()
        a["trend_quality"]["adx"] = {"ADX": 42.0, "plus_DI": 32.0, "minus_DI": 8.0,
                                      "trend_strong": True, "bull_trend": True}
        r = S.score(a)
        tq = r["confirmation"].get("trend_quality_pct")
        self.assertIsNotNone(tq)
        self.assertGreaterEqual(tq, 70)

    def test_weak_adx_penalizes(self):
        """ADX < 20 + 非 bull → trend_quality_pct 低。"""
        a = _strong()
        a["trend_quality"]["adx"] = {"ADX": 14.0, "plus_DI": 15.0, "minus_DI": 22.0,
                                      "trend_strong": False, "bull_trend": False}
        r = S.score(a)
        tq = r["confirmation"].get("trend_quality_pct")
        self.assertIsNotNone(tq)
        self.assertLess(tq, 70)

    def test_adx_missing_is_backward_compatible(self):
        """无 ADX 字段时退化为旧逻辑（trend_quality_pct 仍可计算）。"""
        a = _strong()
        del a["trend_quality"]["adx"]
        r = S.score(a)
        tq = r["confirmation"].get("trend_quality_pct")
        self.assertIsNotNone(tq)


class TestVolumeOBV(unittest.TestCase):
    """Phase 2+3: OBV 背离与量价趋势在 volume_exec 中的作用。"""

    def test_obv_bearish_divergence_caps_volume_score(self):
        a = _strong()
        a["volume"]["obv"] = {"divergence": "bearish"}
        r = S.score(a)
        self.assertLessEqual(r["confirmation"]["volume_pct"], 20)

    def test_obv_missing_is_backward_compatible(self):
        a = _strong()
        del a["volume"]["obv"]
        r = S.score(a)
        self.assertIsNotNone(r["confirmation"]["volume_pct"])
        self.assertGreaterEqual(r["confirmation"]["volume_pct"], 0)

    def test_volume_trend_falling_adds_penalty(self):
        a = _strong()
        a["volume"]["vol_trend_10d"] = "falling"
        r = S.score(a)
        # 基础分 1.0 减去趋势惩罚 → < 1.0
        self.assertLess(r["confirmation"]["volume_pct"], 100)

    def test_volume_trend_missing_is_backward_compatible(self):
        a = _strong()
        del a["volume"]["avg_ratio_5d"]
        del a["volume"]["up_down_vol_ratio"]
        del a["volume"]["vol_trend_10d"]
        r = S.score(a)
        self.assertIsNotNone(r["confirmation"]["volume_pct"])
        # 无趋势字段时退化为原逻辑：ratio_vs_ma20=1.3 → 1.0 * 100
        self.assertGreaterEqual(r["confirmation"]["volume_pct"], 60)

    def test_avg_ratio_low_adds_penalty(self):
        a = _strong()
        a["volume"]["avg_ratio_5d"] = 0.3  # 近 5 日严重缩量
        r = S.score(a)
        self.assertLess(r["confirmation"]["volume_pct"], 90)

    def test_up_down_ratio_bearish_adds_penalty(self):
        a = _strong()
        a["volume"]["up_down_vol_ratio"] = 0.5  # 跌日放量 > 涨日放量
        r = S.score(a)
        self.assertLess(r["confirmation"]["volume_pct"], 90)


class TestEfficiencyFactor(unittest.TestCase):
    def test_efficiency_in_alpha_breakdown(self):
        r = S.score(_strong())
        self.assertIn("efficiency", r["factor_breakdown"])
        # _strong 的 efficiency_30=0.7 → score_pct≈70
        self.assertEqual(r["factor_breakdown"]["efficiency"]["score_pct"], 70)
        self.assertEqual(r["weights"]["efficiency"], 10)

    def test_missing_efficiency_renormalizes(self):
        a = _strong()
        del a["trend_quality"]["efficiency_30"]
        r = S.score(a)
        self.assertIsNone(r["factor_breakdown"]["efficiency"]["score_pct"])
        self.assertTrue(any("efficiency" in f for f in r["data_flags"]))
        # 仍能给出 composite（按 momentum+rel 可用权重重归一）
        self.assertIsNotNone(r["composite"])
        self.assertEqual(r["verdict"], "是")

    def test_low_efficiency_lowers_composite(self):
        hi, lo = _strong(), _strong()
        lo["trend_quality"]["efficiency_30"] = 0.1
        self.assertGreater(S.score(hi)["composite"], S.score(lo)["composite"])


class TestLiquidityFlag(unittest.TestCase):
    def test_low_dollar_volume_flagged(self):
        a = _strong()
        a["volume"]["dollar_vol_ma20"] = 1_000_000  # $1M < $5M 门槛
        r = S.score(a)
        self.assertTrue(any("流动性门槛" in f for f in r["data_flags"]))

    def test_high_dollar_volume_not_flagged(self):
        a = _strong()
        a["volume"]["dollar_vol_ma20"] = 500_000_000  # $500M
        r = S.score(a)
        self.assertFalse(any("流动性门槛" in f for f in r["data_flags"]))

    def test_missing_dollar_volume_no_flag(self):
        # 字段缺失（旧数据）→ 不报流动性 flag，向后兼容
        r = S.score(_strong())
        self.assertFalse(any("流动性门槛" in f for f in r["data_flags"]))


class TestRelStrength(unittest.TestCase):
    def test_single_weak_reading_not_collapsed(self):
        """一个视界对一个基准弱，不应把整体压到地板（旧版全局 min 的缺陷）。"""
        a = _strong()
        # SPY 强，QQQ 的 3m 很弱：旧版 min=-20 会塌；新版每视界取较弱+跨视界均值
        a["relative_strength_pct"] = {
            "SPY": {"r3m_63d": 20.0, "r6m_126d": 25.0},
            "QQQ": {"r3m_63d": -20.0, "r6m_126d": 18.0},
        }
        rs_pct = S.score(a)["factor_breakdown"]["rel_strength"]["score_pct"]
        # r3m 取 min(20,-20)=-20，r6m 取 min(25,18)=18，均值=-1 → logistic≈48-50
        self.assertGreater(rs_pct, 40)

    def test_missing_rel_strength_is_none(self):
        a = _strong()
        a["relative_strength_pct"] = {}
        self.assertIsNone(S.score(a)["factor_breakdown"]["rel_strength"]["score_pct"])


class TestEdge(unittest.TestCase):
    def test_error_dict_returns_none(self):
        self.assertIsNone(S.score({"error": "no data"}))


if __name__ == "__main__":
    unittest.main()
