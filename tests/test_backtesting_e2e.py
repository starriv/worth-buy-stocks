#!/usr/bin/env python3
"""Deterministic long-history acceptance test for the visual backtest path."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.artifact import build_artifact, load_artifact, write_artifact  # noqa: E402
from backtesting.contracts import BacktestConfig, WARMUP_SESSIONS  # noqa: E402
from backtesting.data import load_backtest_data  # noqa: E402
from backtesting.visualization import write_html  # noqa: E402


SYMBOLS = ("AAPL", "SPY", "QQQ")
WARMUP = WARMUP_SESSIONS
EVALUATION_BARS = 1_300


def _sessions(count: int) -> list[str]:
    values: list[str] = []
    day = dt.date(2019, 1, 2)
    while len(values) < count:
        if day.isoweekday() <= 5:
            values.append(day.isoformat())
        day += dt.timedelta(days=1)
    return values


def _bars(dates: list[str], *, base: float, beta: float, phase: float) -> list[dict]:
    """Create trend, range, reversal, price-gap and zero-volume regimes."""

    rows: list[dict] = []
    close = base
    for index, date in enumerate(dates):
        regime = (index // 260) % 5
        drift = (0.16, -0.11, 0.02, 0.24, -0.04)[regime] * beta
        oscillation = math.sin(index / 11.0 + phase) * 0.55
        gap = 8.0 * beta if index == WARMUP + 420 else 0.0
        close = max(5.0, close + drift + oscillation * 0.12 + gap)
        opening = close - drift * 0.4 + math.sin(index / 7.0 + phase) * 0.18
        span = 0.8 + abs(math.sin(index / 13.0)) * 0.7
        rows.append({
            "t": f"{date}T04:00:00Z",
            "o": round(opening, 6),
            "h": round(max(opening, close) + span, 6),
            "l": round(min(opening, close) - span, 6),
            "c": round(close, 6),
            # A zero-volume completed session models a halt without inventing a bar gap.
            "v": 0 if index == WARMUP + 780 else 1_000_000 + (index % 29) * 41_000,
        })
    return rows


def _assert_finite(value, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite(item, f"{path}[{index}]")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise AssertionError(f"{path} is not finite: {value}")


class LongHistoryEndToEndTests(unittest.TestCase):
    def test_1300_bar_input_to_canonical_json_and_self_contained_html(self):
        dates = _sessions(WARMUP + EVALUATION_BARS)
        payload = {"bars": {
            "AAPL": _bars(dates, base=95.0, beta=1.15, phase=0.2),
            "SPY": _bars(dates, base=280.0, beta=0.65, phase=1.0),
            "QQQ": _bars(dates, base=175.0, beta=0.82, phase=1.7),
        }}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long-history.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            started = time.perf_counter()
            bundle = load_backtest_data(
                SYMBOLS,
                dates[WARMUP],
                dates[-1],
                mode="input",
                input_path=str(source),
                warmup_sessions=WARMUP,
            )
            config = BacktestConfig(
                symbol="AAPL",
                start=dates[WARMUP],
                end=dates[-1],
                data_source="input",
                warmup_sessions=WARMUP,
                # Exercise the complete pipeline while keeping routine CI fast.
                signal_step=13,
            )
            artifact = build_artifact(config, bundle)
            json_path = write_artifact(Path(tmp) / "acceptance.json", artifact)
            html_path = write_html(Path(tmp) / "acceptance.html", artifact)
            elapsed = time.perf_counter() - started
            loaded = load_artifact(json_path)
            document = html_path.read_text(encoding="utf-8")

        self.assertEqual(len(artifact["bars"]), EVALUATION_BARS)
        self.assertGreaterEqual(len(artifact["timeline"]), 95)
        self.assertEqual(len(artifact["simulations"]), 7)
        self.assertEqual(artifact["validation"]["status"], "invalid_run")
        self.assertIn("signal_step", artifact["validation"]["invalid_reasons"][0])
        self.assertEqual(loaded["semantic_hash"], artifact["semantic_hash"])
        self.assertIn(artifact["semantic_hash"], document)
        self.assertIn('<script id="artifact-data" type="application/json">', document)
        self.assertNotIn("fetch(", document)
        self.assertLess(elapsed, 45.0)
        _assert_finite(artifact)


if __name__ == "__main__":
    unittest.main()
