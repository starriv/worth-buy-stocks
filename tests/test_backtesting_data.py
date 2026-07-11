#!/usr/bin/env python3
"""Offline tests for the final-only historical data boundary."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.data import (  # noqa: E402
    BacktestDataError,
    adjustment_drift_flags,
    bars_content_hash,
    load_backtest_data,
    normalize_bars,
)
from market_data_store import MarketDataStore  # noqa: E402


SYMBOLS = ["AAPL", "SPY", "QQQ"]


def _dates(count: int = 360) -> list[str]:
    out = []
    day = dt.date(2024, 1, 2)
    while len(out) < count:
        if day.isoweekday() <= 5:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def _bars(dates: list[str], base: float) -> list[dict]:
    rows = []
    for index, date in enumerate(dates):
        close = base + index * 0.1
        rows.append({
            "t": f"{date}T04:00:00Z",
            "o": close - 0.2,
            "h": close + 0.8,
            "l": close - 0.8,
            "c": close,
            "v": 1_000_000 + index,
        })
    return rows


def _payload() -> tuple[list[str], dict[str, list[dict]]]:
    dates = _dates()
    return dates, {
        "AAPL": _bars(dates, 100.0),
        "SPY": _bars(dates, 400.0),
        "QQQ": _bars(dates, 350.0),
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CacheOnlyDataTests(unittest.TestCase):
    def _database(self, directory: str) -> tuple[Path, list[str], dict[str, list[dict]]]:
        path = Path(directory) / "market.sqlite3"
        dates, bars = _payload()
        store = MarketDataStore(path)
        store.commit_daily_sync(
            bars,
            SYMBOLS,
            feed="iex",
            adjustment="split",
            start_date=dates[0],
            end_date=dates[-1],
        )
        store.upsert_live_daily_bars(
            {"AAPL": {
                "t": "2025-12-31T20:00:00Z", "o": 999, "h": 1001,
                "l": 998, "c": 1000, "v": 10,
            }},
            feed="iex",
        )
        return path, dates, bars

    def test_cache_only_is_read_only_and_excludes_live_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dates, _ = self._database(tmp)
            before = (path.stat().st_mtime_ns, path.stat().st_size, _sha(path))
            bundle = load_backtest_data(
                SYMBOLS,
                dates[300],
                dates[-1],
                mode="cache-only",
                cache_db=str(path),
            )
            after = (path.stat().st_mtime_ns, path.stat().st_size, _sha(path))
        self.assertEqual(before, after)
        self.assertEqual(bundle.mode, "cache-only")
        self.assertEqual(bundle.load_start, dates[10])
        self.assertEqual(len(bundle.bars["AAPL"]), len(dates) - 10)
        self.assertNotIn("2025-12-31", [row["t"][:10] for row in bundle.bars["AAPL"]])

    def test_cache_only_never_calls_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dates, _ = self._database(tmp)
            with mock.patch("backtesting.data.fetch_bars", side_effect=AssertionError("network")):
                bundle = load_backtest_data(
                    SYMBOLS, dates[300], dates[-1], mode="cache-only", cache_db=str(path)
                )
        self.assertTrue(bundle.bars["SPY"])

    def test_cache_only_clamps_weekend_or_future_end_to_cached_completed_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dates, _ = self._database(tmp)
            MarketDataStore(path).upsert_market_sessions([{
                "session_date": dates[-1],
                "open_at": f"{dates[-1]}T09:30:00-05:00",
                "close_at": f"{dates[-1]}T16:00:00-05:00",
            }])
            with mock.patch("backtesting.data.fetch_bars", side_effect=AssertionError("network")):
                bundle = load_backtest_data(
                    SYMBOLS, dates[300], "2030-01-06",
                    mode="cache-only", cache_db=str(path),
                )
        self.assertEqual(bundle.requested_end, "2030-01-06")
        self.assertEqual(bundle.actual_end, dates[-1])
        self.assertEqual(bundle.coverage["AAPL"]["effective_end"], dates[-1])
        self.assertIn("clamped to completed session", bundle.notes[0])

    def test_symbol_halted_inside_warmup_window_still_loads_via_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.sqlite3"
            dates, bars = _payload()
            halted = set(dates[280:285])
            bars["AAPL"] = [row for row in bars["AAPL"] if row["t"][:10] not in halted]
            store = MarketDataStore(path)
            store.commit_daily_sync(
                bars, SYMBOLS,
                feed="iex", adjustment="split",
                start_date=dates[0], end_date=dates[-1],
            )
            bundle = load_backtest_data(
                SYMBOLS, dates[300], dates[-1],
                mode="cache-only", cache_db=str(path),
            )
        self.assertEqual(len(bundle.bars["AAPL"]), len(dates) - 10 - len(halted))

    def test_adjustment_drift_probe_flags_split_sized_jump(self):
        dates, bars = _payload()
        drifted = json.loads(json.dumps(bars))
        # Rows after index 320 keep a stale pre-split baseline: 2:1 gap down.
        for row in drifted["AAPL"][320:]:
            for key in ("o", "h", "l", "c"):
                row[key] = row[key] / 2.0
        clean_flags = adjustment_drift_flags(normalize_bars(bars, SYMBOLS))
        drift_flags = adjustment_drift_flags(normalize_bars(drifted, SYMBOLS))
        self.assertEqual(clean_flags, [])
        self.assertEqual(len(drift_flags), 1)
        self.assertIn("possible_adjustment_drift: AAPL", drift_flags[0])
        self.assertIn(dates[320], drift_flags[0])

    def test_drift_flags_reach_bundle_quality_flags(self):
        dates, bars = _payload()
        drifted = json.loads(json.dumps(bars))
        for row in drifted["AAPL"][320:]:
            for key in ("o", "h", "l", "c"):
                row[key] = row[key] / 2.0
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "drifted.json"
            source.write_text(json.dumps({"bars": drifted}), encoding="utf-8")
            bundle = load_backtest_data(
                SYMBOLS, dates[300], dates[-1],
                mode="input", input_path=str(source),
            )
        self.assertEqual(len(bundle.quality_flags), 1)
        self.assertIn(
            "quality_flags", bundle.provenance(),
        )
        self.assertIn("possible_adjustment_drift", bundle.provenance()["quality_flags"][0])

    def test_missing_sync_coverage_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dates, _ = self._database(tmp)
            store = MarketDataStore(path)
            with store.connection() as conn:
                conn.execute("DELETE FROM sync_ranges WHERE symbol='QQQ'")
            with self.assertRaises(BacktestDataError) as caught:
                load_backtest_data(
                    SYMBOLS, dates[300], dates[-1], mode="cache-only", cache_db=str(path)
                )
        self.assertEqual(caught.exception.code, "cache_coverage_incomplete")
        self.assertIn("QQQ", caught.exception.details["gaps"])
        self.assertIn("cache_admin.py warm", caught.exception.details["suggestion"])

    def test_missing_completed_benchmark_row_is_rejected_even_with_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dates, _ = self._database(tmp)
            missing_session = dates[320]
            store = MarketDataStore(path)
            store.upsert_market_sessions([{
                "session_date": missing_session,
                "open_at": f"{missing_session}T09:30:00-05:00",
                "close_at": f"{missing_session}T16:00:00-05:00",
            }])
            with store.connection() as conn:
                conn.execute(
                    "DELETE FROM daily_bars WHERE symbol='SPY' AND session_date=?",
                    (missing_session,),
                )
            with self.assertRaises(BacktestDataError) as caught:
                load_backtest_data(
                    SYMBOLS, dates[300], dates[-1],
                    mode="cache-only", cache_db=str(path),
                )
        self.assertEqual(caught.exception.code, "benchmark_session_gap")
        self.assertEqual(
            caught.exception.details["missing_sessions"]["SPY"],
            [missing_session],
        )

    def test_old_schema_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
            conn.close()
            before = _sha(path)
            with self.assertRaises(BacktestDataError) as caught:
                load_backtest_data(
                    SYMBOLS, "2025-01-01", "2025-02-01",
                    mode="cache-only", cache_db=str(path),
                )
            after = _sha(path)
        self.assertEqual(caught.exception.code, "cache_schema_incompatible")
        self.assertEqual(before, after)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.sqlite3"
            with self.assertRaises(BacktestDataError) as caught:
                load_backtest_data(
                    SYMBOLS, "2025-01-01", "2025-02-01",
                    mode="cache-only", cache_db=str(path),
                )
            self.assertFalse(path.exists())
        self.assertEqual(caught.exception.code, "cache_missing")


class InputDataTests(unittest.TestCase):
    def test_input_hash_is_order_independent_and_changes_with_price(self):
        dates, bars = _payload()
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            changed = Path(tmp) / "changed.json"
            first.write_text(json.dumps({"bars": bars}), encoding="utf-8")
            second.write_text(
                json.dumps({"bars": {key: list(reversed(bars[key])) for key in reversed(SYMBOLS)}}),
                encoding="utf-8",
            )
            altered = json.loads(json.dumps(bars))
            altered["AAPL"][-1]["c"] += 1
            altered["AAPL"][-1]["h"] += 1
            changed.write_text(json.dumps({"bars": altered}), encoding="utf-8")
            args = (SYMBOLS, dates[300], dates[-1])
            one = load_backtest_data(*args, mode="input", input_path=str(first))
            two = load_backtest_data(*args, mode="input", input_path=str(second))
            three = load_backtest_data(*args, mode="input", input_path=str(changed))
        self.assertEqual(one.data_hash, two.data_hash)
        self.assertNotEqual(one.data_hash, three.data_hash)

    def test_input_hash_excludes_rows_outside_effective_load_window(self):
        dates, bars = _payload()
        extended = json.loads(json.dumps(bars))
        for symbol, base in (("AAPL", 100.0), ("SPY", 400.0), ("QQQ", 350.0)):
            extended[symbol].append(_bars(["2030-01-02"], base)[0])
            extended[symbol][0]["c"] += 50
            extended[symbol][0]["h"] += 50
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            first.write_text(json.dumps({"bars": bars}), encoding="utf-8")
            second.write_text(json.dumps({"bars": extended}), encoding="utf-8")
            args = (SYMBOLS, dates[300], dates[-1])
            one = load_backtest_data(*args, mode="input", input_path=str(first))
            two = load_backtest_data(*args, mode="input", input_path=str(second))
        self.assertEqual(one.data_hash, two.data_hash)
        self.assertEqual(one.coverage, two.coverage)

    def test_conflicting_session_is_rejected(self):
        rows = _bars(["2025-01-02"], 100.0)
        conflicting = {"AAPL": [rows[0], {**rows[0], "c": 101.0, "h": 102.0}]}
        with self.assertRaises(BacktestDataError) as caught:
            normalize_bars(conflicting, ["AAPL"])
        self.assertEqual(caught.exception.code, "bar_conflict")

    def test_non_finite_ohlcv_is_rejected_before_hashing_or_scoring(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                row = _bars(["2025-01-02"], 100.0)[0]
                row["c"] = value
                with self.assertRaises(BacktestDataError) as caught:
                    normalize_bars({"AAPL": [row]}, ["AAPL"])
                self.assertEqual(caught.exception.code, "bar_invalid")

    def test_auto_is_the_only_mode_that_calls_fetch(self):
        dates, bars = _payload()
        with mock.patch(
            "backtesting.data.fetch_bars", return_value=(bars, "iex", "fixture")
        ) as fetch:
            bundle = load_backtest_data(
                SYMBOLS, dates[300], dates[-1], mode="auto", cache_db="/tmp/unused"
            )
        fetch.assert_called_once()
        self.assertEqual(bundle.mode, "auto")
        self.assertIn("fixture", bundle.notes)

    def test_hash_helper_uses_semantic_rows(self):
        _, bars = _payload()
        normalized = normalize_bars(bars, SYMBOLS)
        self.assertEqual(
            bars_content_hash(normalized, feed="iex", adjustment="split"),
            bars_content_hash(normalized, feed="iex", adjustment="split"),
        )


if __name__ == "__main__":
    unittest.main()
