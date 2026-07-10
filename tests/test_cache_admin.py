#!/usr/bin/env python3
"""Offline tests for cache_admin read-only inspection and guarded mutations."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cache_admin as A  # noqa: E402
from market_data_store import MarketDataStore  # noqa: E402


def _bar(day: str, close: float = 100.0) -> dict:
    return {
        "t": f"{day}T04:00:00Z", "o": close - 1, "h": close + 1,
        "l": close - 2, "c": close, "v": 1000,
    }


def _snapshot(day: str = "2026-07-09") -> dict:
    return {
        "status": "ok", "as_of": f"{day}T15:00:00Z", "feed": "iex",
        "symbols": {
            "AAPL": {
                "symbol": "AAPL",
                "daily_bar": {
                    "date": day, "open": 99, "high": 102,
                    "low": 98, "close": 101, "volume": 5000,
                },
            }
        },
    }


class AdminCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "market.sqlite3"


class TestReadOnlyInspection(AdminCase):
    def test_status_and_integrity_do_not_create_missing_database(self):
        status = A.status_summary(self.db)
        integrity = A.integrity_summary(self.db)
        self.assertFalse(status["exists"])
        self.assertEqual(integrity["integrity"], "missing")
        self.assertFalse(self.db.exists())

    def test_status_reports_rows_coverage_and_integrity_without_secrets(self):
        store = MarketDataStore(self.db)
        store.commit_daily_sync(
            {"AAPL": [_bar("2026-07-09")]}, ["AAPL"],
            feed="iex", adjustment="split",
            start_date="2026-07-01", end_date="2026-07-09",
        )
        store.upsert_snapshot_context(_snapshot())
        status = A.status_summary(self.db)
        self.assertEqual(status["schema_version"], 2)
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["rows"]["daily_bars"], 1)
        self.assertEqual(status["rows"]["latest_snapshots"], 1)
        self.assertEqual(status["final_sessions"]["latest"], "2026-07-09")
        self.assertEqual(status["recent_errors"]["status"], "not_persisted")
        self.assertNotIn("key", str(status).lower())

    def test_corrupt_file_is_reported_without_rewriting_it(self):
        original = b"not-a-sqlite-database"
        self.db.write_bytes(original)
        integrity = A.integrity_summary(self.db)
        status = A.status_summary(self.db)
        self.assertEqual(integrity["integrity"], "unavailable")
        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(self.db.read_bytes(), original)


class TestPurge(AdminCase):
    def _seed(self):
        store = MarketDataStore(self.db)
        bars = {
            "AAPL": [_bar("2026-07-08"), _bar("2026-07-09"), _bar("2026-07-10")],
            "MSFT": [_bar("2026-07-08"), _bar("2026-07-09"), _bar("2026-07-10")],
        }
        store.commit_daily_sync(
            bars, list(bars), feed="iex", adjustment="split",
            start_date="2026-07-08", end_date="2026-07-10",
        )
        store.upsert_snapshot_context(_snapshot())
        return store

    def test_purge_refuses_implicit_global_scope(self):
        store = MarketDataStore(self.db)
        with self.assertRaisesRegex(ValueError, "必须指定"):
            A.purge_cache(store)
        with self.assertRaisesRegex(ValueError, "同时提供"):
            A.purge_cache(store, start_date="2026-07-09")

    def test_scoped_date_purge_splits_coverage_and_preserves_other_symbols(self):
        store = self._seed()
        affected = A.purge_cache(
            store, symbols=["AAPL"], start_date="2026-07-09", end_date="2026-07-09",
            feed="iex", adjustment="split",
        )
        self.assertEqual(affected["daily_bars"], 1)
        self.assertEqual(affected["latest_snapshots"], 1)
        aapl = store.get_daily_bars(
            ["AAPL"], start_date="2026-07-08", end_date="2026-07-10",
            feed="iex", adjustment="split",
        )
        msft = store.get_daily_bars(
            ["MSFT"], start_date="2026-07-08", end_date="2026-07-10",
            feed="iex", adjustment="split",
        )
        self.assertEqual([row["t"][:10] for row in aapl["AAPL"]], ["2026-07-08", "2026-07-10"])
        self.assertEqual(len(msft["MSFT"]), 3)
        self.assertEqual(
            store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-08", end_date="2026-07-10",
            ),
            [("2026-07-09", "2026-07-09")],
        )
        self.assertEqual(
            store.missing_ranges(
                "MSFT", feed="iex", adjustment="split",
                start_date="2026-07-08", end_date="2026-07-10",
            ),
            [],
        )

    def test_all_purge_empties_tables_but_keeps_database(self):
        store = self._seed()
        affected = A.purge_cache(store, purge_all=True)
        self.assertGreater(affected["daily_bars"], 0)
        self.assertTrue(self.db.exists())
        status = A.status_summary(self.db)
        self.assertTrue(all(count == 0 for count in status["rows"].values()))


class TestWarmRefresh(AdminCase):
    def test_warm_threads_namespace_and_database_to_bars_and_snapshots(self):
        args = argparse.Namespace(
            symbols="AAPL,SPY", start="2026-07-01", end="2026-07-09",
            feed="iex", adjustment="split", timeout=30,
            cache_db=str(self.db),
        )
        bars = {"AAPL": [_bar("2026-07-09")], "SPY": [_bar("2026-07-09")]}
        snapshot = {
            "status": "ok", "feed": "iex", "symbols": {"AAPL": {}},
            "cache_note": "db",
        }
        with patch.object(A, "fetch_bars", return_value=(bars, "iex", "warm")) as fetch_bars, \
             patch.object(A, "fetch_snapshots", return_value=snapshot) as fetch_snapshots:
            result = A.warm_or_refresh(args, "auto")
        fetch_bars.assert_called_once_with(
            ["AAPL", "SPY"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30,
            cache_mode="auto", cache_db=str(self.db),
        )
        fetch_snapshots.assert_called_once_with(
            ["AAPL", "SPY"], feed="iex", timeout=30, adjustment="split",
            cache_mode="auto", cache_db=str(self.db),
        )
        self.assertEqual(result["bars"]["rows"], 2)


if __name__ == "__main__":
    unittest.main()
