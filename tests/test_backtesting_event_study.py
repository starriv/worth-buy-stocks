#!/usr/bin/env python3
"""Event labeling, non-overlap and validation tests."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.event_study import _validation, build_event_study  # noqa: E402


def _fixture(count: int = 30):
    dates = []
    day = dt.date(2026, 1, 2)
    while len(dates) < count:
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    bars = {}
    for symbol, base, slope in (
        ("AAPL", 100.0, 1.0), ("SPY", 400.0, 0.2), ("QQQ", 350.0, 0.25)
    ):
        bars[symbol] = [
            {
                "t": f"{date}T04:00:00Z", "o": base + slope * index - 0.2,
                "h": base + slope * index + 1.0, "l": base + slope * index - 1.0,
                "c": base + slope * index, "v": 1_000_000,
            }
            for index, date in enumerate(dates)
        ]
    timeline = []
    for index, date in enumerate(dates):
        high = index % 3 == 0
        timeline.append({
            "signal_date": date,
            "status": "ok",
            "core_data_complete": True,
            "raw_composite": 82.0 if high else 65.0,
            "composite": 80.0 if high else 65.0,
            "verdict": "是" if high else "观察",
            "confirmation": {"ok": high},
            "risk_gates": [] if high else ["fixture gate"],
            "entry_timing": {"classification": "trend_continuation" if high else "overextended"},
        })
    replay = {"symbol": "AAPL", "calendar": dates, "timeline": timeline, "exclusions": []}
    return dates, bars, replay


class EventStudyTests(unittest.TestCase):
    def test_returns_mfe_mae_and_groups_are_materialized(self):
        _, bars, replay = _fixture()
        result = build_event_study(replay, bars, horizons=(2,), min_inference_group=2)
        horizon = result["horizons"]["2"]
        event = horizon["events"][0]
        self.assertGreater(event["symbol_return_pct"], event["spy_return_pct"])
        self.assertGreater(event["mfe_pct"], 0)
        self.assertLess(event["mae_pct"], event["mfe_pct"])
        self.assertIn("gte_75", horizon["descriptive"]["score_buckets"])
        self.assertIn("trend_continuation", horizon["descriptive"]["entry_timing"])

    def test_inference_uses_non_overlapping_sample(self):
        _, bars, replay = _fixture()
        result = build_event_study(replay, bars, horizons=(5,))
        horizon = result["horizons"]["5"]
        self.assertEqual(horizon["descriptive_n"], 25)
        self.assertEqual(horizon["inference_n"], 5)
        indexes = [row["calendar_index"] for row in horizon["inference"]["events"]]
        self.assertTrue(all(b - a >= 5 for a, b in zip(indexes, indexes[1:])))

    def test_immature_tail_is_reported(self):
        _, bars, replay = _fixture()
        result = build_event_study(replay, bars, horizons=(4,))
        exclusions = result["horizons"]["4"]["exclusions"]
        self.assertEqual(sum(row["reason"] == "horizon_immature" for row in exclusions), 4)

    def test_missing_target_session_uses_conservative_stale_mark(self):
        dates, bars, replay = _fixture()
        missing_target = dates[2]
        bars["AAPL"] = [
            row for row in bars["AAPL"] if row["t"][:10] != missing_target
        ]
        result = build_event_study(replay, bars, horizons=(2,))
        horizon = result["horizons"]["2"]
        event = next(
            row for row in horizon["events"] if row["signal_date"] == dates[0]
        )
        self.assertEqual(event["degraded"], "stale_target_price")
        self.assertEqual(event["target_date"], missing_target)
        self.assertEqual(event["target_price_date"], dates[1])
        self.assertEqual(horizon["degraded_n"], 1)
        self.assertFalse(any(
            row["signal_date"] == dates[0] for row in horizon["exclusions"]
        ))

    def test_signal_with_no_price_after_it_is_excluded(self):
        dates, bars, replay = _fixture()
        cutoff = dates[10]
        bars["AAPL"] = [row for row in bars["AAPL"] if row["t"][:10] <= cutoff]
        result = build_event_study(replay, bars, horizons=(2,))
        exclusions = result["horizons"]["2"]["exclusions"]
        self.assertIn({
            "signal_date": cutoff,
            "horizon": 2,
            "target_date": dates[12],
            "reason": "no_price_after_signal",
        }, exclusions)
        terminal = next(
            row for row in result["horizons"]["2"]["events"]
            if row["signal_date"] == dates[9]
        )
        self.assertEqual(terminal["degraded"], "terminal_price_before_horizon")
        self.assertEqual(terminal["target_price_date"], cutoff)

    def test_bankrupt_terminal_mark_is_included_in_mae(self):
        dates, bars, replay = _fixture()
        bars["SIVB"] = bars.pop("AAPL")[:2]
        replay["symbol"] = "SIVB"
        result = build_event_study(replay, bars, horizons=(2,))
        event = next(
            row for row in result["horizons"]["2"]["events"]
            if row["signal_date"] == dates[0]
        )
        self.assertEqual(event["degraded"], "bankrupt_terminal")
        self.assertEqual(event["symbol_return_pct"], -100.0)
        self.assertEqual(event["target_close"], 0.0)
        self.assertEqual(event["mae_pct"], -100.0)

    def test_validation_supports_and_contradicts_are_pre_registered(self):
        supports = _validation({
            "disciplined_yes": {"n": 8}, "not_yes": {"n": 9},
            "yes_mean_ci_95": [1.0, 4.0],
            "yes_minus_not_yes_ci_95": [0.5, 3.0],
        }, 8)
        contradicts = _validation({
            "disciplined_yes": {"n": 8}, "not_yes": {"n": 9},
            "yes_mean_ci_95": [-4.0, -1.0],
            "yes_minus_not_yes_ci_95": [-3.0, -0.5],
        }, 8)
        self.assertEqual(supports["status"], "supports")
        self.assertEqual(contradicts["status"], "contradicts")

    def test_validation_is_inconclusive_for_small_or_crossing_ci(self):
        small = _validation({
            "disciplined_yes": {"n": 7}, "not_yes": {"n": 20},
            "yes_mean_ci_95": [1.0, 4.0],
            "yes_minus_not_yes_ci_95": [1.0, 3.0],
        }, 8)
        crossing = _validation({
            "disciplined_yes": {"n": 10}, "not_yes": {"n": 10},
            "yes_mean_ci_95": [-1.0, 4.0],
            "yes_minus_not_yes_ci_95": [-2.0, 3.0],
        }, 8)
        self.assertEqual(small["status"], "inconclusive")
        self.assertEqual(crossing["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
