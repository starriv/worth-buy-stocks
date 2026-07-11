#!/usr/bin/env python3
"""Ranking, regime and diff tests for the rotation list tool."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from rotation_list import build_rotation_list  # noqa: E402


def _weekdays(start: str, count: int) -> list[str]:
    day = dt.date.fromisoformat(start)
    dates = []
    while len(dates) < count:
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    return dates


class FakePanel:
    def __init__(self, *, uptrend: bool):
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

    def test_unknown_signal_date_is_rejected(self):
        with self.assertRaises(ValueError):
            build_rotation_list(
                FakePanel(uptrend=True), top_n=2, signal_date="2019-01-01"
            )


if __name__ == "__main__":
    unittest.main()
