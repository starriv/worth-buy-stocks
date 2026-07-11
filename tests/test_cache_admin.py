#!/usr/bin/env python3
"""Offline tests for cache_admin read-only inspection and guarded mutations."""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


def _asset(asset_id: str, symbol: str, *, exchange="NASDAQ", tradable=True) -> dict:
    return {
        "id": asset_id, "class": "us_equity", "exchange": exchange,
        "symbol": symbol, "name": symbol, "status": "active",
        "tradable": tradable, "fractionable": True,
        "marginable": True, "shortable": True, "easy_to_borrow": True,
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
        self.assertEqual(status["schema_version"], 3)
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


class TestWarmMarketCli(AdminCase):
    @staticmethod
    def result(stop_reason="complete"):
        return {
            "universe": 2, "covered": 2, "pending": 0,
            "completed": 2, "failed": 0, "pages": 1,
            "effective_rpm": 1, "retries": 0, "elapsed": 1,
            "eta": 0, "stop_reason": stop_reason, "scope_limited": False,
        }

    def test_parser_defaults_and_rejects_unsafe_values(self):
        parser = A.build_parser()
        args = parser.parse_args(["warm-market"])
        self.assertEqual(
            (args.years, args.workers, args.batch_size, args.max_rpm, args.max_runtime),
            (3, 4, 50, 120, 3600),
        )
        for argv in (
            ["warm-market", "--workers", "100"],
            ["warm-market", "--max-rpm", "200"],
            ["warm-market", "--years", "0"],
            ["warm-market", "--batch-size", "-1"],
            ["warm-market", "--max-runtime", "0"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_main_keeps_progress_on_stderr_and_summary_on_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def run(**kwargs):
            kwargs["progress"]({
                "event": "planned", "universe": 2, "pending": 2, "batches": 1
            })
            return self.result("dry_run")

        with patch.object(A, "run_market_warmup", side_effect=run) as warm, \
             patch.object(A, "fetch_snapshots") as snapshots, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = A.main([
                "--cache-db", str(self.db), "warm-market", "--dry-run"
            ])
        self.assertEqual(code, A.EXIT_OK)
        self.assertEqual(json.loads(stdout.getvalue())["stop_reason"], "dry_run")
        self.assertEqual(json.loads(stderr.getvalue().strip())["event"], "planned")
        self.assertNotIn("planned", stdout.getvalue())
        snapshots.assert_not_called()
        self.assertTrue(warm.call_args.kwargs["dry_run"])
        self.assertEqual(warm.call_args.kwargs["max_rpm"], 120)

    def test_exit_codes_cover_partial_lock_fatal_and_sigint(self):
        args = A.build_parser().parse_args(["warm-market"])
        cases = (
            (self.result("max_runtime"), None, A.EXIT_PARTIAL),
            (None, A.WarmupAlreadyRunningError("locked"), A.EXIT_LOCKED),
            (None, RuntimeError("fatal fixture"), A.EXIT_FATAL),
        )
        for returned, error, expected in cases:
            with self.subTest(expected=expected):
                behavior = {"return_value": returned} if error is None else {"side_effect": error}
                with patch.object(A, "run_market_warmup", **behavior):
                    result, code = A.warm_market_command(args)
                self.assertEqual(code, expected)
                self.assertIn("stop_reason", result)

        def interrupt(**kwargs):
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertTrue(kwargs["stop_event"].is_set())
            return self.result("cancelled")

        import signal
        with patch.object(A, "run_market_warmup", side_effect=interrupt):
            result, code = A.warm_market_command(args)
        self.assertEqual(code, A.EXIT_SIGINT)
        self.assertEqual(result["stop_reason"], "sigint")

    def test_status_reports_three_year_target_for_tracked_symbols_only(self):
        store = MarketDataStore(self.db)
        store.upsert_market_sessions([
            {"session_date": "2026-07-09", "open_at": "09:30", "close_at": "16:00"},
            {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "16:00"},
        ])
        store.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2023-07-10", end_date="2026-07-10",
        )
        store.add_sync_range(
            ["MSFT"], feed="iex", adjustment="split",
            start_date="2026-01-01", end_date="2026-07-10",
        )

        class FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 7, 10, 21, 0, tzinfo=dt.timezone.utc)
                return value if tz is None else value.astimezone(tz)

        with patch.object(A.dt, "datetime", FrozenDatetime):
            target = A.status_summary(self.db)["warm_market_target"]
        self.assertEqual(target["latest_completed_session"], "2026-07-10")
        self.assertEqual(target["tracked_symbols"], 2)
        self.assertEqual(target["covered_symbols"], 1)
        self.assertIn("tracked SQLite", target["scope"])


class TestAssetReconcileCli(AdminCase):
    @staticmethod
    def result(stop_reason="complete"):
        return {
            "universe": 2, "covered": 2, "pending": 0,
            "completed": 2, "failed": 0, "pages": 1,
            "effective_rpm": 1, "retries": 0, "elapsed": 1,
            "eta": 0, "stop_reason": stop_reason,
            "universe_source": "asset_catalog",
        }

    def test_parser_exposes_asset_sync_and_reconcile_defaults(self):
        parser = A.build_parser()
        sync = parser.parse_args(["sync-assets"])
        reconcile = parser.parse_args(["reconcile-market", "--dry-run"])
        self.assertFalse(sync.force)
        self.assertEqual(sync.timeout, 30)
        self.assertEqual(
            (
                reconcile.years, reconcile.workers, reconcile.batch_size,
                reconcile.max_rpm, reconcile.max_runtime,
            ),
            (3, 4, 50, 120, 3600),
        )
        self.assertTrue(reconcile.dry_run)
        self.assertFalse(reconcile.force_assets)

    def test_reconcile_main_threads_catalog_options_and_returns_summary(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def run(**kwargs):
            kwargs["progress"]({
                "event": "planned", "universe": 2, "pending": 2, "batches": 1
            })
            return self.result("dry_run")

        with patch.object(A, "run_market_reconcile", side_effect=run) as reconcile, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = A.main([
                "--cache-db", str(self.db), "reconcile-market",
                "--dry-run", "--force-assets",
            ])
        self.assertEqual(code, A.EXIT_OK)
        self.assertEqual(json.loads(stdout.getvalue())["stop_reason"], "dry_run")
        self.assertIn("planned", stderr.getvalue())
        self.assertTrue(reconcile.call_args.kwargs["dry_run"])
        self.assertTrue(reconcile.call_args.kwargs["force_assets"])

    def test_sync_assets_main_uses_daily_catalog_contract(self):
        summary = {
            "catalog": "alpaca_us_equity_active", "source": "remote",
            "current_count": 10, "eligible_count": 8,
        }
        stdout = io.StringIO()
        with patch.object(A, "ensure_daily_asset_catalog", return_value=summary) as ensure, \
             redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = A.main([
                "--cache-db", str(self.db), "sync-assets", "--force"
            ])
        self.assertEqual(code, A.EXIT_OK)
        self.assertEqual(json.loads(stdout.getvalue())["asset_catalog"], summary)
        self.assertTrue(ensure.call_args.kwargs["force"])

    def test_status_reports_catalog_exclusions_and_reconcile_coverage(self):
        store = MarketDataStore(self.db)
        store.replace_asset_catalog([
            _asset("a", "AAPL"), _asset("b", "MSFT"),
            _asset("c", "OTCM", exchange="OTC"),
        ])
        store.upsert_market_sessions([
            {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "16:00"}
        ])
        store.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2023-07-10", end_date="2026-07-10",
        )

        class FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 7, 10, 21, 0, tzinfo=dt.timezone.utc)
                return value if tz is None else value.astimezone(tz)

        with patch.object(A.dt, "datetime", FrozenDatetime):
            catalog = A.status_summary(self.db)["asset_catalog"]
        self.assertEqual(catalog["current_count"], 3)
        self.assertEqual(catalog["eligible_count"], 2)
        self.assertEqual(catalog["exclusions"]["unsupported_iex_otc"], 1)
        self.assertEqual(catalog["reconcile_target"]["covered_symbols"], 1)
        self.assertEqual(catalog["reconcile_target"]["pending_symbols"], 1)


if __name__ == "__main__":
    unittest.main()
