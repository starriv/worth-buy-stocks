#!/usr/bin/env python3
"""Ranking, regime and diff tests for the rotation list tool."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from rotation_list import (  # noqa: E402
    build_rotation_list,
    evaluate_records,
    merge_snapshot,
    rebalance_status,
)


def _weekdays(start: str, count: int) -> list[str]:
    day = dt.date.fromisoformat(start)
    dates = []
    while len(dates) < count:
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    return dates


class FakePanel:
    def __init__(self, *, uptrend: bool, stale: tuple[str, ...] = ()):
        self.calendar = _weekdays("2024-01-01", 260)
        drift = 0.5 if uptrend else -0.5
        base = 500.0 if uptrend else 700.0
        self.bars = {"SPY": [
            {"t": f"{date}T04:00:00Z", "o": base + drift * index,
             "h": base + drift * index + 1, "l": base + drift * index - 1,
             "c": base + drift * index, "v": 1}
            for index, date in enumerate(self.calendar)
        ]}
        self.scores = {
            "NVDA": 92.0, "META": 88.0, "AAPL": 75.0, "MSFT": 60.0,
        }
        self.stale = set(stale)

    def close(self, symbol: str, date: str):
        if symbol == "SPY" or (symbol in self.scores and symbol not in self.stale):
            return 100.0
        return None

    def scores_at(self, date: str):
        return {
            symbol: {"score": {"composite": value}}
            for symbol, value in self.scores.items()
        }


class RotationListTests(unittest.TestCase):
    def test_risk_on_ranks_top_n_equal_weight(self):
        result = build_rotation_list(FakePanel(uptrend=True), top_n=2)
        self.assertEqual(result["regime"], "risk_on")
        self.assertEqual(
            [row["symbol"] for row in result["target"]], ["NVDA", "META"]
        )
        self.assertEqual(result["target"][0]["weight_pct"], 50.0)
        self.assertNotIn("diff", result)

    def test_risk_off_goes_all_cash(self):
        result = build_rotation_list(FakePanel(uptrend=False), top_n=2)
        self.assertEqual(result["regime"], "risk_off")
        self.assertEqual(result["action"], "all_cash")
        self.assertEqual(result["target"], [])

    def test_holdings_diff_lists_buys_sells_keeps(self):
        result = build_rotation_list(
            FakePanel(uptrend=True), top_n=2,
            holdings={"META": 50.0, "MSFT": 50.0},
        )
        self.assertEqual(result["diff"]["buy"], ["NVDA"])
        self.assertEqual(result["diff"]["sell"], ["MSFT"])
        self.assertEqual(result["diff"]["keep"], ["META"])

    def test_stale_symbol_without_signal_day_bar_is_excluded(self):
        result = build_rotation_list(
            FakePanel(uptrend=True, stale=("NVDA",)), top_n=2
        )
        self.assertEqual(
            [row["symbol"] for row in result["target"]], ["META", "AAPL"]
        )

    def test_unknown_signal_date_is_rejected(self):
        with self.assertRaises(ValueError):
            build_rotation_list(
                FakePanel(uptrend=True), top_n=2, signal_date="2019-01-01"
            )


class ForwardTrackingTests(unittest.TestCase):
    def _result(self, signal_date: str):
        return {
            "signal_date": signal_date,
            "strategy": "step63_top20",
            "top_n": 2,
            "regime": "risk_on",
            "target": [
                {"symbol": "NVDA", "composite": 92.0, "weight_pct": 50.0},
                {"symbol": "META", "composite": 88.0, "weight_pct": 50.0},
            ],
        }

    def test_merge_snapshot_dedupes_by_signal_date(self):
        records = merge_snapshot([], self._result("2026-01-05"))
        records = merge_snapshot(records, self._result("2026-01-05"))
        records = merge_snapshot(records, self._result("2026-04-06"))
        self.assertEqual(
            [row["signal_date"] for row in records], ["2026-01-05", "2026-04-06"]
        )
        self.assertEqual(len(records[0]["members"]), 2)

    def test_rebalance_status_counts_sessions_and_flags_due(self):
        calendar = _weekdays("2026-01-05", 130)
        records = [{"signal_date": calendar[0]}]
        early = rebalance_status(records, calendar, calendar[30])
        self.assertEqual(early["sessions_elapsed"], 30)
        self.assertFalse(early["due"])
        late = rebalance_status(records, calendar, calendar[70])
        self.assertTrue(late["due"])
        self.assertTrue(rebalance_status([], calendar, calendar[10])["due"])

    def test_evaluate_settles_matured_records_against_benchmarks(self):
        calendar = _weekdays("2026-01-05", 130)

        class EvalPanel:
            def __init__(self):
                self.calendar = calendar
                self.returns = {"NVDA": 20.0, "META": 10.0, "QQQ": 5.0, "SPY": 4.0}

            def fwd_return_pct(self, symbol, time_index, horizon):
                if time_index + horizon >= len(self.calendar):
                    return None
                return self.returns.get(symbol)

        records = [
            {"signal_date": calendar[0], "regime": "risk_on",
             "members": [{"symbol": "NVDA"}, {"symbol": "META"}]},
            {"signal_date": calendar[100], "regime": "risk_on",
             "members": [{"symbol": "NVDA"}]},
        ]
        report = evaluate_records(records, EvalPanel())
        self.assertEqual(len(report["matured"]), 1)
        self.assertEqual(report["pending"], 1)
        row = report["matured"][0]
        self.assertEqual(row["portfolio_return_pct"], 15.0)
        self.assertEqual(row["excess_vs_qqq_pct"], 10.0)
        self.assertEqual(report["mean_excess_vs_qqq_pct"], 10.0)

    def test_evaluate_risk_off_record_settles_as_cash(self):
        calendar = _weekdays("2026-01-05", 130)

        class EvalPanel:
            def __init__(self):
                self.calendar = calendar

            def fwd_return_pct(self, symbol, time_index, horizon):
                return -8.0 if symbol in ("QQQ", "SPY") else None

        records = [{"signal_date": calendar[0], "regime": "risk_off", "members": []}]
        report = evaluate_records(records, EvalPanel())
        row = report["matured"][0]
        self.assertEqual(row["portfolio_return_pct"], 0.0)
        self.assertEqual(row["excess_vs_qqq_pct"], 8.0)


if __name__ == "__main__":
    unittest.main()
