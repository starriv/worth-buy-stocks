#!/usr/bin/env python3
"""Point-in-time replay and package statistics regression tests."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.data import load_backtest_data  # noqa: E402
from backtesting.replay import replay_scores  # noqa: E402
from backtesting.simulation import run_simulations  # noqa: E402
from backtesting import statistics as stats  # noqa: E402
from pipeline import build_result  # noqa: E402


def _dates(count: int = 330) -> list[str]:
    rows = []
    day = dt.date(2023, 1, 3)
    while len(rows) < count:
        if day.isoweekday() <= 5:
            rows.append(day.isoformat())
        day += dt.timedelta(days=1)
    return rows


def _bars(dates: list[str], base: float, slope: float) -> list[dict]:
    out = []
    for index, date in enumerate(dates):
        wave = ((index % 17) - 8) * 0.03
        close = base + slope * index + wave
        out.append({
            "t": f"{date}T04:00:00Z",
            "o": close - 0.15,
            "h": close + 0.7,
            "l": close - 0.7,
            "c": close,
            "v": 900_000 + index * 500,
        })
    return out


def _payload() -> tuple[list[str], dict[str, list[dict]]]:
    dates = _dates()
    return dates, {
        "AAPL": _bars(dates, 100, 0.18),
        "SPY": _bars(dates, 400, 0.10),
        "QQQ": _bars(dates, 350, 0.12),
    }


class ReplayTests(unittest.TestCase):
    def _bundle(self, directory: str, bars: dict[str, list[dict]], dates: list[str]):
        path = Path(directory) / "bars.json"
        path.write_text(json.dumps({"bars": bars}), encoding="utf-8")
        return load_backtest_data(
            ["AAPL", "SPY", "QQQ"],
            dates[265],
            dates[-1],
            mode="input",
            input_path=str(path),
        )

    def test_replay_matches_direct_production_pipeline_at_last_date(self):
        dates, bars = _payload()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp, bars, dates)
            replay = replay_scores(bundle, "AAPL", signal_step=5)
        last = replay["timeline"][-1]
        cutoff = last["signal_date"]
        sliced = {
            symbol: [bar for bar in rows if bar["t"][:10] <= cutoff]
            for symbol, rows in bundle.bars.items()
        }
        direct = build_result(
            ["AAPL", "SPY", "QQQ"], sliced, "iex", "split"
        )["symbols"]["AAPL"]["score"]
        self.assertEqual(last["raw_composite"], direct["raw_composite"])
        self.assertEqual(last["composite"], direct["composite"])
        self.assertEqual(last["verdict"], direct["verdict"])
        self.assertEqual(last["factor_breakdown"], direct["factor_breakdown"])

    def test_future_mutation_does_not_change_prior_timeline(self):
        dates, bars = _payload()
        cutoff = dates[260]
        changed = json.loads(json.dumps(bars))
        for symbol in ("AAPL", "SPY", "QQQ"):
            for row in changed[symbol]:
                if row["t"][:10] > cutoff:
                    row["o"] *= 1.8
                    row["h"] *= 1.8
                    row["l"] *= 1.8
                    row["c"] *= 1.8
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            original_bundle = self._bundle(one, bars, dates)
            mutated_bundle = self._bundle(two, changed, dates)
            original = replay_scores(original_bundle, "AAPL")
            mutated = replay_scores(mutated_bundle, "AAPL")
        before_original = [row for row in original["timeline"] if row["signal_date"] <= cutoff]
        before_mutated = [row for row in mutated["timeline"] if row["signal_date"] <= cutoff]
        self.assertEqual(before_original, before_mutated)
        original_scenarios, _ = run_simulations(
            original, original_bundle.bars, horizons=(21,), cost_bps=10
        )
        mutated_scenarios, _ = run_simulations(
            mutated, mutated_bundle.bars, horizons=(21,), cost_bps=10
        )
        for name in original_scenarios:
            def decisions(scenario):
                return {
                    "orders": [
                        {
                            key: trade.get(key)
                            for key in ("trade_id", "policy", "horizon", "signal_date", "entry_date")
                        }
                        for trade in scenario["trades"]
                        if trade["signal_date"] <= cutoff
                    ],
                    "skipped": [
                        row for row in scenario["skipped_signals"]
                        if row["signal_date"] <= cutoff
                    ],
                }

            self.assertEqual(
                decisions(original_scenarios[name]),
                decisions(mutated_scenarios[name]),
            )

    def test_missing_symbol_session_is_excluded(self):
        dates, bars = _payload()
        missing_date = dates[280]
        bars["AAPL"] = [row for row in bars["AAPL"] if row["t"][:10] != missing_date]
        with tempfile.TemporaryDirectory() as tmp:
            replay = replay_scores(self._bundle(tmp, bars, dates), "AAPL")
        self.assertIn(
            {"signal_date": missing_date, "reason": "symbol_missing_on_spy_session"},
            replay["exclusions"],
        )


class StatisticsRegressionTests(unittest.TestCase):
    def test_existing_numeric_contract_is_preserved(self):
        pairs = [(index, float(index)) for index in range(10)]
        self.assertEqual(stats.quintile_spread(pairs), (8.0, 7.6, 8.5, 0.5))
        self.assertEqual(stats.avg_ranks([5, 5, 9]), [1.5, 1.5, 3.0])


if __name__ == "__main__":
    unittest.main()
