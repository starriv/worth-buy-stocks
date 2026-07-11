#!/usr/bin/env python3
"""Cache controls and source-equivalence tests for runtime/research entrypoints."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backtest as BT  # noqa: E402
import backtesting.panel as BP  # noqa: E402
import chart as C  # noqa: E402
import fetching as F  # noqa: E402
import forward_log as FL  # noqa: E402
import indicators as I  # noqa: E402
import scanner as S  # noqa: E402
from pipeline import build_result  # noqa: E402


def _bar(day: str, close: float, volume: int = 1_000_000) -> dict:
    return {
        "t": f"{day}T04:00:00Z",
        "o": close - 0.5,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": volume,
        "vw": close,
        "n": 100,
    }


class TestEntrypointControls(unittest.TestCase):
    def test_chart_self_fetch_threads_cache_controls(self):
        args = SimpleNamespace(
            count=30, start="2026-07-01", end="2026-07-09",
            feed="iex", adjustment="split", limit=10000, timeout=30,
            cache="refresh", cache_db="/tmp/chart-cache.sqlite3",
        )
        with patch("fetching.fetch_bars", return_value=({"AAPL": [_bar("2026-07-09", 100)]}, "iex", None)) as fetch:
            symbol, rows = C._fetch("aapl", args)
        self.assertEqual(symbol, "AAPL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(fetch.call_args.kwargs, {
            "cache_mode": "refresh", "cache_db": "/tmp/chart-cache.sqlite3"
        })

    def test_cross_sectional_panel_threads_cache_controls(self):
        rows = {"SPY": [_bar("2026-07-09", 500)], "QQQ": [_bar("2026-07-09", 600)]}
        with patch.object(BP, "fetch_bars", return_value=(rows, "iex", "cache hit")) as fetch:
            panel = BP.load_panel(
                "iex", "split", 60, history_days=30,
                cache_mode="off", cache_db="/tmp/backtest-cache.sqlite3",
            )
        self.assertEqual(panel.used_feed, "iex")
        self.assertEqual(fetch.call_args.kwargs, {
            "cache_mode": "off", "cache_db": "/tmp/backtest-cache.sqlite3"
        })

    def test_unified_backtest_research_subcommands_thread_cache_controls(self):
        for command, event in (
            ("factor-ic", "backtest_factor_ic"),
            ("score", "backtest_score"),
            ("robustness", "backtest_robustness"),
        ):
            run = Mock()
            with self.subTest(command=command), \
                 patch.object(sys, "argv", [
                     "backtest.py", command, "--cache", "refresh",
                     "--cache-db", "/tmp/research.sqlite3",
                 ]), \
                 patch.dict(BT.RESEARCH_COMMANDS, {command: (event, run)}):
                self.assertEqual(BT.main(), 0)
            run.assert_called_once_with(
                "iex", "split", 60,
                cache_mode="refresh", cache_db="/tmp/research.sqlite3",
            )

    def test_forward_fetch_threads_cache_controls(self):
        with patch.object(FL, "fetch_bars", return_value=({}, "iex", None)) as fetch:
            FL._fetch(
                ["AAPL"], 30, "iex", "split", 120,
                cache_mode="refresh", cache_db="/tmp/forward.sqlite3",
            )
        self.assertEqual(fetch.call_args.kwargs, {
            "cache_mode": "refresh", "cache_db": "/tmp/forward.sqlite3"
        })

    def test_indicators_online_threads_cache_controls(self):
        argv = [
            "indicators.py", "--symbols", "AAPL", "--start", "2026-07-01",
            "--end", "2026-07-09", "--cache", "refresh",
            "--cache-db", "/tmp/indicators.sqlite3", "--account-context", "off",
            "--finnhub-context", "off", "--snapshot", "off", "--ai-opinion", "off",
        ]
        result = {"symbols": {}, "supplemental": {}}
        with patch.object(sys, "argv", argv), \
             patch.object(I, "fetch_bars", return_value=({}, "iex", "cache note")) as fetch, \
             patch.object(I, "build_result", return_value=result), \
             patch.object(I, "validate_payload"), \
             contextlib.redirect_stdout(io.StringIO()):
            I.main()
        self.assertEqual(fetch.call_args.kwargs, {
            "cache_mode": "refresh", "cache_db": "/tmp/indicators.sqlite3"
        })

    def test_indicators_input_bypasses_market_data_and_database_facade(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump({"bars": {"AAPL": [_bar("2026-07-09", 100)]}}, f)
            input_path = f.name
        self.addCleanup(os.unlink, input_path)
        argv = [
            "indicators.py", "--input", input_path, "--cache", "refresh",
            "--cache-db", "/tmp/must-not-open.sqlite3",
            "--account-context", "on", "--finnhub-context", "on",
            "--snapshot", "on", "--ai-opinion", "on",
        ]
        result = {"symbols": {}, "supplemental": {}}
        with patch.object(sys, "argv", argv), \
             patch.object(I, "fetch_bars") as bars, \
             patch.object(I, "fetch_snapshots") as snapshots, \
             patch.object(I, "fetch_account_context") as account, \
             patch.object(I, "fetch_finnhub_context") as finnhub, \
             patch.object(I, "opinions_for_result") as opinions, \
             patch.object(I, "build_result", return_value=result), \
             patch.object(I, "validate_payload"), \
             contextlib.redirect_stdout(io.StringIO()):
            I.main()
        bars.assert_not_called()
        snapshots.assert_not_called()
        account.assert_not_called()
        finnhub.assert_not_called()
        opinions.assert_not_called()


class TestRemoteCacheGolden(unittest.TestCase):
    def test_remote_and_warm_cache_produce_identical_symbol_scores(self):
        start_day = dt.date(2025, 1, 2)
        days: list[dt.date] = []
        day = start_day
        while len(days) < 260:
            if day.weekday() < 5:
                days.append(day)
            day += dt.timedelta(days=1)
        sessions = [
            {"session_date": d.isoformat(), "open_at": "09:30", "close_at": "16:00"}
            for d in days
        ]
        dataset = {
            "AAPL": [_bar(d.isoformat(), 100 * (1.0018 ** i)) for i, d in enumerate(days)],
            "SPY": [_bar(d.isoformat(), 400 * (1.0007 ** i)) for i, d in enumerate(days)],
            "QQQ": [_bar(d.isoformat(), 350 * (1.0009 ** i)) for i, d in enumerate(days)],
        }

        def remote(symbols, start, end, feed, adjustment, limit, timeout):
            return {
                symbol: [bar for bar in dataset[symbol] if start <= bar["t"][:10] <= end]
                for symbol in symbols
            }, feed, None

        now = dt.datetime.combine(
            days[-1] + dt.timedelta(days=2), dt.time(12), tzinfo=dt.timezone.utc
        )
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(F, "_fetch_market_calendar", return_value=sessions), \
             patch.object(F, "_fetch_bars_remote", side_effect=remote) as transport:
            kwargs = {
                "cache_db": Path(tmp) / "golden.sqlite3",
                "now_fn": lambda: now,
            }
            cold, cold_feed, _ = F.fetch_bars(
                ["AAPL", "SPY", "QQQ"], days[0].isoformat(), days[-1].isoformat(),
                "iex", "split", 10000, 30, **kwargs,
            )
            warm, warm_feed, _ = F.fetch_bars(
                ["AAPL", "SPY", "QQQ"], days[0].isoformat(), days[-1].isoformat(),
                "iex", "split", 10000, 30, **kwargs,
            )

        self.assertEqual(transport.call_count, 1)
        self.assertEqual(cold, warm)
        cold_result = build_result(list(dataset), cold, cold_feed, "split")
        warm_result = build_result(list(dataset), warm, warm_feed, "split")
        self.assertEqual(
            {s: row.get("score") for s, row in cold_result["symbols"].items()},
            {s: row.get("score") for s, row in warm_result["symbols"].items()},
        )
        self.assertEqual(
            S.extract_candidates(cold_result),
            S.extract_candidates(warm_result),
        )


if __name__ == "__main__":
    unittest.main()
