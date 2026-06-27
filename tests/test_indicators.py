#!/usr/bin/env python3
"""indicators.py 的确定性单元测试。

只测纯计算与离线（--input）路径，不触网、不调用 alpaca CLI。
运行：python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import indicators as I  # noqa: E402


def _bars(closes, start=date(2024, 1, 2), vol=1_000_000):
    """把收盘价序列变成 multi-bars 形状的日线，跳过周末，OHLC 围绕收盘。"""
    out = []
    d = start
    for i, c in enumerate(closes):
        while d.isoweekday() > 5:
            d += timedelta(days=1)
        out.append({"t": d.isoformat() + "T04:00:00Z",
                    "o": c, "h": c + 1, "l": c - 1, "c": c, "v": vol + i})
        d += timedelta(days=1)
    return out


class TestPrimitives(unittest.TestCase):
    def test_ma_exact(self):
        self.assertEqual(I.ma(list(range(1, 11)), 10), 5.5)
        self.assertIsNone(I.ma([1, 2, 3], 10))  # 不足返回 None

    def test_pct_return_exact(self):
        self.assertEqual(I.pct_return([100, 110], 1), 10.0)
        self.assertEqual(I.pct_return([100, 50], 1), -50.0)
        self.assertIsNone(I.pct_return([100], 1))   # 历史不足
        self.assertIsNone(I.pct_return([0, 10], 1))  # 基准为 0

    def test_ema_constant(self):
        self.assertEqual(I.ema_series([5, 5, 5, 5], 3), [5, 5, 5, 5])

    def test_recent_cross(self):
        # a 从下方上穿 b -> golden；最后一根上穿
        a = [0, 0, 0, 2]
        b = [1, 1, 1, 1]
        self.assertEqual(I._recent_cross(a, b)["type"], "golden")
        self.assertEqual(I._recent_cross(b, a)["type"], "death")
        self.assertIsNone(I._recent_cross([1, 1, 1], [0, 0, 0]))  # 无交叉

    def test_relative_strength(self):
        sym = {"r1m_21d": 10.0, "r3m_63d": 5.0, "r6m_126d": None}
        bench = {"r1m_21d": 4.0, "r3m_63d": 5.0, "r6m_126d": 2.0}
        rs = I.relative_strength(sym, bench)
        self.assertEqual(rs["r1m_21d"], 6.0)
        self.assertEqual(rs["r3m_63d"], 0.0)
        self.assertIsNone(rs["r6m_126d"])  # 任一为 None -> None


class TestRSI(unittest.TestCase):
    def test_all_gains_is_100(self):
        self.assertEqual(I.rsi(list(range(1, 30)), 14), 100.0)

    def test_all_losses_is_0(self):
        self.assertEqual(I.rsi(list(range(30, 1, -1)), 14), 0.0)

    def test_insufficient_is_none(self):
        self.assertIsNone(I.rsi([1, 2, 3], 14))

    def test_flat_is_neutral(self):
        self.assertEqual(I.rsi([10.0] * 30, 14), 50.0)


class TestKDJ(unittest.TestCase):
    def test_constant_converges_to_50(self):
        k = I.kdj([10] * 30, [10] * 30, [10] * 30)
        self.assertEqual((k["K"], k["D"], k["J"]), (50.0, 50.0, 50.0))
        self.assertFalse(k["bull"])       # K>D 为严格不成立
        self.assertFalse(k["above_50"])

    def test_rising_is_bullish(self):
        c = list(range(1, 40))
        k = I.kdj([x + 1 for x in c], [x - 1 for x in c], c)
        self.assertTrue(k["bull"])
        self.assertTrue(k["above_50"])

    def test_insufficient_is_none(self):
        self.assertIsNone(I.kdj([1] * 5, [1] * 5, [1] * 5))


class TestMACD(unittest.TestCase):
    def test_constant_is_flat(self):
        m = I.macd([10.0] * 100)
        self.assertEqual((m["DIF"], m["DEA"], m["hist"]), (0.0, 0.0, 0.0))
        self.assertFalse(m["above_zero"])
        self.assertFalse(m["bull"])

    def test_rising_is_bull_above_zero(self):
        # 用略加速的上涨序列，避免完美线性导致 DIF/DEA 完全收敛
        m = I.macd([float(x ** 1.02) for x in range(1, 200)])
        self.assertTrue(m["bull"])
        self.assertTrue(m["above_zero"])

    def test_insufficient_is_none(self):
        self.assertIsNone(I.macd([1.0] * 30))


class TestWeekly(unittest.TestCase):
    def test_aggregation(self):
        # 一周 Mon-Fri + 下周 Mon，共 2 个 ISO 周
        bars = _bars([10, 11, 12, 13, 14, 20], start=date(2026, 6, 8))
        weeks = I.to_weekly(bars)
        self.assertEqual(len(weeks), 2)
        self.assertEqual(weeks[0]["c"], 14)             # 首周收盘=末根
        self.assertEqual(weeks[0]["h"], 15)             # 周高=max(h)=14+1
        self.assertEqual(weeks[0]["l"], 9)              # 周低=min(l)=10-1
        self.assertEqual(weeks[1]["c"], 20)

    def test_bearish_alignment(self):
        self.assertTrue(I._weekly_bear([float(x) for x in range(40, 0, -1)]))
        self.assertFalse(I._weekly_bear([float(x) for x in range(1, 41)]))

    def test_flat_market_not_bearish(self):
        # 横盘：四条均线近乎相等，严格 < + 间距门槛应判 False（不误触发否决）
        flat = [100.0 + (0.01 if i % 2 else -0.01) for i in range(40)]
        self.assertFalse(I._weekly_bear(flat))

    def test_shallow_decline_below_margin_not_bearish(self):
        # 极缓下行，MA5→MA30 跨度 < 1%：视为缠绕横盘，不算空头排列
        gentle = [100.0 - i * 0.001 for i in range(40)]
        self.assertFalse(I._weekly_bear(gentle))


class TestAnalyzeEdges(unittest.TestCase):
    def test_ma60_unknown_is_none_not_false(self):
        """P0 回归：历史不足 60 根时，above_MA60/MA60_rising 必须为 None。"""
        a = I.analyze_symbol(_bars(list(range(100, 145))))  # 45 根，上涨
        self.assertIsNone(a["ma"]["MA60"])
        self.assertIsNone(a["ma"]["above_MA60"])
        self.assertIsNone(a["ma"]["MA60_rising"])
        self.assertEqual(a["bars_count"], 45)

    def test_below_min_bars_errors(self):
        a = I.analyze_symbol(_bars([1, 2, 3]))
        self.assertIn("error", a)

    def test_high_lookback_bars_reported(self):
        a = I.analyze_symbol(_bars(list(range(1, 81))))  # 80 根 < 252
        self.assertEqual(a["structure_30d"]["high_lookback_bars"], 80)

    def test_weekly_partial_flag(self):
        # 末根为周三 -> 当前周未收盘
        bars = _bars(list(range(1, 70)), start=date(2026, 6, 8))
        last_wd = date.fromisoformat(bars[-1]["t"][:10]).isoweekday()
        a = I.analyze_symbol(bars)
        self.assertEqual(a["weekly"]["last_week_partial"], last_wd < 5)


class TestBuildResultInput(unittest.TestCase):
    def test_symbols_with_benchmarks_appends_when_available(self):
        bars = {"AAPL": [], "SPY": [], "QQQ": []}
        self.assertEqual(I._symbols_with_benchmarks(["aapl"], bars), ["AAPL", "SPY", "QQQ"])

    def test_symbols_with_benchmarks_offline_does_not_invent_missing_data(self):
        bars = {"AAPL": []}
        self.assertEqual(I._symbols_with_benchmarks(["aapl"], bars), ["AAPL"])

    def test_end_to_end_offline(self):
        closes = [float(x) for x in range(50, 250)]
        payload = {"bars": {"AAPL": _bars(closes), "SPY": _bars(closes), "QQQ": _bars(closes)}}
        bars = {s: sorted(b, key=lambda x: x["t"]) for s, b in payload["bars"].items()}
        res = I.build_result(["AAPL", "SPY", "QQQ"], bars, "iex", "split")
        aapl = res["symbols"]["AAPL"]
        self.assertNotIn("error", aapl)
        # 同一序列 -> 相对强度为 0
        self.assertEqual(aapl["relative_strength_pct"]["SPY"]["r1m_21d"], 0.0)

    def test_missing_symbol_errors(self):
        res = I.build_result(["MISSING"], {}, "iex", "split")
        self.assertIn("error", res["symbols"]["MISSING"])

    def test_load_llm_context_file(self):
        payload = {"symbols": {"aapl": {"data_trust": "suspect"}}}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            ctx = I._load_llm_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(ctx, {"AAPL": {"data_trust": "suspect"}})

    def test_load_account_context_file(self):
        payload = {
            "account": {"equity": "100000", "cash": "20000", "long_market_value": "75000"},
            "positions": [{"symbol": "aapl", "qty": "5", "market_value": "1000"}],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            ctx = I._load_account_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["account"]["cash_pct"], 20.0)
        self.assertEqual(ctx["positions"]["AAPL"]["market_value"], 1000.0)

    def test_load_finnhub_context_file(self):
        payload = {
            "symbols": {
                "aapl": {
                    "quote": {"current_price": 123.45},
                    "news": [{"headline": "news"}],
                    "data_flags": [],
                }
            }
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            ctx = I._load_finnhub_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["symbols"]["AAPL"]["quote"]["current_price"], 123.45)

    def test_finnhub_auto_without_key_does_not_fetch(self):
        args = SimpleNamespace(
            finnhub_context_file=None,
            finnhub_context="auto",
            input=None,
            finnhub_news_days=30,
            finnhub_earnings_days=14,
            finnhub_timeout=15,
        )
        missing_env = os.path.join(tempfile.gettempdir(), "wbs_missing_test.env")
        with patch.dict(os.environ, {"WORTH_BUY_STOCKS_ENV_FILE": missing_env}, clear=True), \
                patch("indicators.fetch_finnhub_context") as fetch:
            self.assertIsNone(I._finnhub_context_from_args(args, ["AAPL"]))
        fetch.assert_not_called()

    def test_build_result_threads_account_context(self):
        closes = [100.0 * (1.004 ** i) for i in range(260)]
        flat = [400.0 * (1.0005 ** i) for i in range(260)]
        bars = {"WIN": _bars(closes), "SPY": _bars(flat), "QQQ": _bars(flat)}
        account_context = I.normalize_account_context({
            "account": {"equity": "100000", "cash": "10000", "long_market_value": "90000"},
            "positions": [{"symbol": "WIN", "qty": "1000", "market_value": "50000"}],
        })
        res = I.build_result(
            ["WIN", "SPY", "QQQ"], bars, "iex", "split", account_context=account_context
        )
        self.assertEqual(res["account_context"]["status"], "ok")
        overlay = res["symbols"]["WIN"]["score"]["account_overlay"]
        self.assertEqual(overlay["holding_status"], "held")
        self.assertGreater(overlay["current_position_pct"], 0)

    def test_build_result_threads_finnhub_context(self):
        closes = [100.0 * (1.004 ** i) for i in range(260)]
        flat = [400.0 * (1.0005 ** i) for i in range(260)]
        bars = {"WIN": _bars(closes), "SPY": _bars(flat), "QQQ": _bars(flat)}
        finnhub_context = I.normalize_finnhub_context({
            "symbols": {
                "WIN": {
                    "status": "ok",
                    "quote": {"current_price": 123.45},
                    "profile": {"name": "Winner Inc"},
                    "data_flags": ["quote stale"],
                }
            }
        })
        res = I.build_result(
            ["WIN", "SPY", "QQQ"], bars, "iex", "split", finnhub_context=finnhub_context
        )
        self.assertEqual(res["supplemental"]["finnhub"]["status"], "ok")
        self.assertIn("WIN: quote stale", res["supplemental"]["finnhub"]["data_flags"])
        self.assertEqual(
            res["symbols"]["WIN"]["supplemental"]["finnhub"]["quote"]["current_price"],
            123.45,
        )


if __name__ == "__main__":
    unittest.main()
