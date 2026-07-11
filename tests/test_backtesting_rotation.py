#!/usr/bin/env python3
"""Accounting and selection tests for the rotation portfolio study."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.rotation import (  # noqa: E402
    plan_signal_indices,
    run_rotation_study,
    simulate_rotation,
)


def _weekdays(start: str, count: int) -> list[str]:
    day = dt.date.fromisoformat(start)
    dates = []
    while len(dates) < count:
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    return dates


def _flat_bars(dates: list[str], price: float, drift: float = 0.0):
    return [
        {
            "t": f"{date}T04:00:00Z",
            "o": price + drift * index,
            "h": price + drift * index + 1,
            "l": price + drift * index - 1,
            "c": price + drift * index,
            "v": 1_000_000,
        }
        for index, date in enumerate(dates)
    ]


class SignalGridTests(unittest.TestCase):
    def test_grid_starts_at_warmup_and_skips_last_session(self):
        calendar = _weekdays("2020-01-01", 300)
        indices = plan_signal_indices(calendar, warmup=260)
        self.assertEqual(indices[0], 260)
        self.assertTrue(all(index < 299 for index in indices))
        self.assertEqual(indices[1] - indices[0], 21)


class RotationSimulationTests(unittest.TestCase):
    def _setup(self, *, risk_on=True):
        dates = _weekdays("2026-01-05", 10)
        bars = {
            "AAA": _flat_bars(dates, 100.0),
            "BBB": _flat_bars(dates, 50.0),
            "CCC": _flat_bars(dates, 20.0),
        }
        scores = {dates[0]: {"AAA": 90.0, "BBB": 80.0, "CCC": 70.0}}
        regimes = {date: risk_on for date in dates}
        return dates, bars, scores, regimes

    def test_top_n_equal_weight_with_entry_costs(self):
        dates, bars, scores, regimes = self._setup()
        result = simulate_rotation(
            bars, dates, [0], scores,
            top_n=2, cost_bps=10, regimes=regimes,
            universe=("AAA", "BBB", "CCC"),
            window_start=dates[0], window_end=dates[-1],
        )
        rebalance = result["rebalances"][0]
        self.assertEqual(rebalance["members"], ["AAA", "BBB"])
        self.assertEqual(rebalance["execution_date"], dates[1])
        # Full equity deployed at 10 bps: equity = 1 - 0.001.
        self.assertAlmostEqual(result["curve"][1]["equity"], 0.999, places=6)
        self.assertAlmostEqual(result["metrics"]["total_cost_equity"], 0.001, places=6)

    def test_risk_off_signal_keeps_portfolio_in_cash(self):
        dates, bars, scores, regimes = self._setup(risk_on=False)
        result = simulate_rotation(
            bars, dates, [0], scores,
            top_n=2, cost_bps=10, regimes=regimes,
            universe=("AAA", "BBB", "CCC"),
            window_start=dates[0], window_end=dates[-1],
        )
        self.assertEqual(result["rebalances"][0]["member_count"], 0)
        self.assertEqual(result["metrics"]["invested_session_pct"], 0.0)
        self.assertEqual(result["curve"][-1]["equity"], 1.0)

    def test_rebalance_swaps_dropped_member_and_charges_both_sides(self):
        dates, bars, scores, regimes = self._setup()
        scores[dates[2]] = {"AAA": 90.0, "CCC": 85.0, "BBB": 10.0}
        result = simulate_rotation(
            bars, dates, [0, 2], scores,
            top_n=2, cost_bps=10, regimes=regimes,
            universe=("AAA", "BBB", "CCC"),
            window_start=dates[0], window_end=dates[-1],
        )
        second = result["rebalances"][1]
        self.assertEqual(second["members"], ["AAA", "CCC"])
        self.assertGreater(second["traded_notional_equity"], 0)
        # Flat prices: costs are the only drag, exactly two one-way legs on
        # the BBB->CCC swap of half the book plus the initial deployment.
        self.assertLess(result["curve"][-1]["equity"], 0.999)

    def test_bankrupt_holding_is_written_off_to_zero(self):
        dates, bars, scores, regimes = self._setup()
        bars["SIVB"] = _flat_bars(dates[:3], 40.0)
        scores[dates[0]] = {"SIVB": 95.0, "AAA": 90.0}
        result = simulate_rotation(
            bars, dates, [0], scores,
            top_n=2, cost_bps=0, regimes=regimes,
            universe=("AAA", "SIVB"),
            window_start=dates[0], window_end=dates[-1],
        )
        # Half the book dies with SIVB, the AAA half survives at flat price.
        self.assertAlmostEqual(result["curve"][-1]["equity"], 0.5, places=6)

    def test_stale_non_bankrupt_symbol_marks_at_last_close(self):
        dates, bars, scores, regimes = self._setup()
        bars["BBB"] = _flat_bars(dates[:4], 50.0)
        result = simulate_rotation(
            bars, dates, [0], scores,
            top_n=2, cost_bps=0, regimes=regimes,
            universe=("AAA", "BBB", "CCC"),
            window_start=dates[0], window_end=dates[-1],
        )
        self.assertAlmostEqual(result["curve"][-1]["equity"], 1.0, places=6)


class RotationStudyTests(unittest.TestCase):
    def test_study_selects_winner_and_applies_gates(self):
        calendar = _weekdays("2020-07-27", 1560)
        bars = {
            "SPY": _flat_bars(calendar, 300.0, drift=0.2),
            "QQQ": _flat_bars(calendar, 300.0, drift=0.3),
            "AAPL": _flat_bars(calendar, 100.0, drift=0.5),
            "MSFT": _flat_bars(calendar, 100.0, drift=0.1),
        }

        class FakePanel:
            def __init__(self):
                self.bars = bars
                self.calendar = calendar

        def scores(date: str):
            return {"AAPL": 90.0, "MSFT": 60.0}

        report = run_rotation_study(panel=FakePanel(), scores_provider=scores)
        self.assertEqual(report["report_type"], "rotation_study_research")
        self.assertIn(report["selection"]["winner"], report["configs"])
        self.assertIn(report["selection"]["decision"], ("promote", "reject"))
        for result in report["configs"].values():
            for window in result["windows"].values():
                self.assertIsNotNone(window["metrics"]["total_return_pct"])
                self.assertIn("QQQ", window["benchmarks"])
        self.assertTrue(report["semantic_hash"])
        winner = report["configs"][report["selection"]["winner"]]
        self.assertTrue(winner["windows"]["holdout"]["rebalances"])


if __name__ == "__main__":
    unittest.main()
