#!/usr/bin/env python3
"""forward_log.py 纯函数的离线测试(merge/evaluate/bucket,不触网)。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import forward_log as F  # noqa: E402
import scoring as S  # noqa: E402


def _rec(run_date, symbol, verdict="是", last_close=100.0, version="v1.0", **kw):
    return {"run_date": run_date, "model_version": version, "symbol": symbol,
            "composite": 80.0, "verdict": verdict, "market_risk_off": False,
            "last_close": last_close, **kw}


class TestMergeRecords(unittest.TestCase):
    def test_model_version_uses_scoring_single_source(self):
        self.assertEqual(F.MODEL_VERSION, S.MODEL_VERSION)
        self.assertEqual(S.MODEL_VERSION, "v1.2")

    def test_same_day_rerun_overwrites(self):
        old = [_rec("2026-07-09", "AAPL", composite=70.0)]
        new = [_rec("2026-07-09", "AAPL", composite=81.0)]
        merged = F.merge_records(old, new)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["composite"], 81.0)

    def test_history_is_append_only(self):
        old = [_rec("2026-07-01", "AAPL")]
        new = [_rec("2026-07-09", "AAPL")]
        self.assertEqual(len(F.merge_records(old, new)), 2)

    def test_different_version_not_overwritten(self):
        old = [_rec("2026-07-09", "AAPL", version="v1.0")]
        new = [_rec("2026-07-09", "AAPL", version="v1.1")]
        self.assertEqual(len(F.merge_records(old, new)), 2)


class TestEvaluateRecords(unittest.TestCase):
    """5 个交易日的合成日历,horizon=2:d1 记录到期,d4 记录未到期。"""

    def setUp(self):
        self.calendar = [f"2026-07-0{d}" for d in range(1, 6)]
        self.closes = {
            "SPY": {d: 100.0 + i for i, d in enumerate(self.calendar)},  # d1=100,d3=102
            "AAPL": {d: 50.0 * (1 + i) for i, d in enumerate(self.calendar)},
            "HALT": {"2026-07-01": 10.0, "2026-07-02": 8.0},             # d2 后停牌
        }

    def test_matured_and_immature_split(self):
        records = [
            _rec("2026-07-01", "AAPL", last_close=50.0),   # 到期:entry d1,target d3=150
            _rec("2026-07-04", "AAPL", last_close=200.0),  # 未到期:d4+2 越界
        ]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        self.assertEqual(len(res["matured"]), 1)
        self.assertEqual(res["immature_count"], 1)
        m = res["matured"][0]
        self.assertAlmostEqual(m["realized_pct"], 200.0)  # 50→150
        self.assertAlmostEqual(m["spy_pct"], 2.0)         # 100→102
        self.assertAlmostEqual(m["excess_pct"], 198.0)

    def test_halted_symbol_uses_last_available_close(self):
        records = [_rec("2026-07-01", "HALT", last_close=10.0)]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        self.assertEqual(len(res["matured"]), 1)
        self.assertAlmostEqual(res["matured"][0]["realized_pct"], -20.0)  # 10→8

    def test_no_matured_records(self):
        records = [_rec("2026-07-05", "AAPL", last_close=250.0)]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        self.assertEqual(res["matured"], [])
        self.assertEqual(res["immature_count"], 1)

    def test_missing_entry_price_is_invalid(self):
        records = [_rec("2026-07-01", "AAPL", last_close=None)]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        self.assertEqual(res["invalid_count"], 1)

    def test_v11_uses_next_session_close_for_stock_and_spy(self):
        records = [_rec(
            "2026-07-02", "AAPL", last_close=999.0, version="v1.1",
            signal_date="2026-07-01", entry_timing="trend_continuation",
        )]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        m = res["matured"][0]
        self.assertEqual(m["entry_date"], "2026-07-02")
        self.assertEqual(m["entry_price"], 100.0)  # 忽略记录中的 999，使用同日真实收盘
        self.assertEqual(m["evaluation_method"], "next_session_close")
        self.assertAlmostEqual(m["spy_pct"], round((103.0 / 101.0 - 1) * 100, 2))

    def test_v11_missing_next_session_stock_price_is_invalid(self):
        records = [_rec(
            "2026-07-02", "HALT", version="v1.1", signal_date="2026-07-02",
            entry_timing="pullback_reversal",
        )]
        res = F.evaluate_records(records, self.closes, self.calendar, horizon=2)
        self.assertEqual(res["matured"], [])
        self.assertEqual(res["invalid_count"], 1)


class TestBucketStats(unittest.TestCase):
    def test_grouped_by_version_and_verdict(self):
        matured = [
            {**_rec("2026-07-01", "A"), "realized_pct": 10.0, "excess_pct": 4.0},
            {**_rec("2026-07-01", "B"), "realized_pct": 20.0, "excess_pct": 6.0},
            {**_rec("2026-07-01", "C", verdict="否"), "realized_pct": -5.0, "excess_pct": -8.0},
            {**_rec("2026-07-01", "D", version="v1.1"), "realized_pct": 99.0, "excess_pct": 99.0},
        ]
        st = F.bucket_stats(matured)
        self.assertEqual(st["v1.0/是"], {"n": 2, "mean_pct": 15.0, "mean_excess_pct": 5.0})
        self.assertEqual(st["v1.0/否"]["n"], 1)
        self.assertIn("v1.1/是", st)  # 跨版本不混算

    def test_timing_buckets_are_versioned(self):
        matured = [
            {**_rec("2026-07-01", "A", version="v1.1", entry_timing="overextended"),
             "realized_pct": 2.0, "excess_pct": -1.0},
            {**_rec("2026-07-01", "B", version="v1.1", entry_timing="pullback_reversal"),
             "realized_pct": 8.0, "excess_pct": 4.0},
            {**_rec("2026-07-01", "C", version="v1.0"),
             "realized_pct": 3.0, "excess_pct": 0.0},
        ]
        stats = F.timing_bucket_stats(matured)
        self.assertEqual(stats["v1.1/overextended"]["mean_pct"], 2.0)
        self.assertEqual(stats["v1.1/pullback_reversal"]["mean_excess_pct"], 4.0)
        self.assertIn("v1.0/unknown", stats)


class TestJsonlRoundtrip(unittest.TestCase):
    def test_write_and_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "data", "log.jsonl")
            rows = [_rec("2026-07-09", "AAPL"), _rec("2026-07-09", "MSFT")]
            F.write_jsonl(path, rows)
            self.assertEqual(F.load_jsonl(path), rows)

    def test_load_missing_file_is_empty(self):
        self.assertEqual(F.load_jsonl("/nonexistent/x.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
