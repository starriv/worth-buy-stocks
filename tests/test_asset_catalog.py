#!/usr/bin/env python3
"""Offline tests for the persistent Alpaca asset catalog."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import asset_catalog as C  # noqa: E402
from market_data_store import (  # noqa: E402
    ASSET_CATALOG,
    CacheBusyError,
    MarketDataStore,
    asset_data_eligibility,
)


def _asset(
    asset_id: str,
    symbol: str,
    *,
    exchange: str = "NASDAQ",
    tradable: bool = True,
    status: str = "active",
    asset_class: str = "us_equity",
) -> dict:
    return {
        "id": asset_id,
        "class": asset_class,
        "exchange": exchange,
        "symbol": symbol,
        "name": f"{symbol} Inc",
        "status": status,
        "tradable": tradable,
        "fractionable": True,
        "marginable": True,
        "shortable": True,
        "easy_to_borrow": False,
    }


class AssetCatalogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "market.sqlite3"
        self.store = MarketDataStore(
            self.db,
            now_fn=lambda: dt.datetime(2026, 7, 10, 1, 2, 3, tzinfo=dt.timezone.utc),
        )


class TestEligibility(unittest.TestCase):
    def test_iex_eligibility_is_wider_than_scanner_but_excludes_unsupported(self):
        self.assertEqual(asset_data_eligibility(_asset("1", "BRK.B")), (True, None))
        self.assertEqual(
            asset_data_eligibility(_asset("2", "OTCM", exchange="OTC")),
            (False, "unsupported_iex_otc"),
        )
        self.assertEqual(
            asset_data_eligibility(_asset("3", "NOPE", tradable=False)),
            (False, "not_tradable"),
        )
        self.assertEqual(
            asset_data_eligibility(_asset("4", "BAD,SYM")),
            (False, "unsafe_symbol_format"),
        )
        self.assertEqual(
            asset_data_eligibility(_asset("5", "FUT", asset_class="crypto")),
            (False, "unsupported_asset_class"),
        )


class TestCatalogStore(AssetCatalogCase):
    def test_atomic_replace_tracks_new_updated_and_disappeared_assets(self):
        first = self.store.replace_asset_catalog(
            [
                _asset("a", "AAPL"),
                _asset("b", "BRK.B", exchange="NYSE"),
                _asset("c", "OTCM", exchange="OTC"),
                _asset("d", "NOPE", tradable=False),
            ],
            synced_at="2026-07-10T01:00:00Z",
        )
        self.assertEqual(first["source_count"], 4)
        self.assertEqual(first["tradable_count"], 3)
        self.assertEqual(first["eligible_count"], 2)
        self.assertEqual(first["new_count"], 4)
        self.assertEqual(
            self.store.get_asset_symbols(), ["AAPL", "BRK.B"]
        )
        self.assertEqual(
            self.store.get_asset_exclusion_counts(),
            {"eligible": 2, "not_tradable": 1, "unsupported_iex_otc": 1},
        )

        second = self.store.replace_asset_catalog(
            [_asset("a", "AAPL2"), _asset("e", "MSFT")],
            synced_at="2026-07-11T01:00:00Z",
        )
        self.assertEqual(second["new_count"], 1)
        self.assertEqual(second["disappeared_count"], 3)
        self.assertEqual(self.store.get_asset_symbols(), ["AAPL2", "MSFT"])
        all_rows = {row["asset_id"]: row for row in self.store.get_assets(current_only=False)}
        self.assertEqual(all_rows["a"]["first_seen_at"], "2026-07-10T01:00:00Z")
        self.assertEqual(all_rows["a"]["last_seen_at"], "2026-07-11T01:00:00Z")
        self.assertEqual(all_rows["b"]["is_current"], 0)
        self.assertEqual(all_rows["b"]["excluded_reason"], "not_seen_in_latest_sync")

    def test_empty_or_invalid_full_response_preserves_previous_state(self):
        self.store.replace_asset_catalog(
            [_asset("a", "AAPL")], synced_at="2026-07-10T01:00:00Z"
        )
        before = self.store.get_asset_catalog_state()
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.store.replace_asset_catalog([])
        with self.assertRaisesRegex(ValueError, "requires id and symbol"):
            self.store.replace_asset_catalog([{"id": "broken"}])
        self.assertEqual(self.store.get_asset_catalog_state(), before)
        self.assertEqual(self.store.get_asset_symbols(), ["AAPL"])

    def test_duplicate_asset_ids_use_last_payload_without_duplicate_rows(self):
        result = self.store.replace_asset_catalog([
            _asset("a", "OLD"), _asset("a", "NEW"),
        ])
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(self.store.get_asset_symbols(), ["NEW"])

    def test_state_write_failure_rolls_back_asset_changes(self):
        self.store.schema_version()
        with self.store.connection() as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_asset_state
                BEFORE INSERT ON asset_catalog_state
                BEGIN SELECT RAISE(ABORT, 'state fixture failure'); END
                """
            )
        with self.assertRaisesRegex(Exception, "state fixture failure"):
            self.store.replace_asset_catalog([_asset("a", "AAPL")])
        self.assertEqual(self.store.get_assets(current_only=False), [])
        self.assertIsNone(self.store.get_asset_catalog_state())

    def test_database_lock_keeps_previous_catalog(self):
        self.store.replace_asset_catalog([_asset("a", "AAPL")])
        locker = sqlite3.connect(self.db, isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")
            impatient = MarketDataStore(
                self.db, busy_timeout_ms=20,
                now_fn=lambda: dt.datetime(2026, 7, 10, 2, tzinfo=dt.timezone.utc),
            )
            with self.assertRaises(CacheBusyError):
                impatient.replace_asset_catalog([_asset("b", "MSFT")])
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(self.store.get_asset_symbols(), ["AAPL"])


class TestDailyCatalog(AssetCatalogCase):
    def test_daily_ensure_uses_cache_and_force_refreshes(self):
        calls = []

        def fetcher(*, timeout):
            calls.append(timeout)
            return [_asset("a", "AAPL"), _asset("b", "OTCM", exchange="OTC")]

        first = C.ensure_daily_asset_catalog(self.store, fetcher=fetcher, timeout=17)
        second = C.ensure_daily_asset_catalog(self.store, fetcher=fetcher, timeout=17)
        forced = C.ensure_daily_asset_catalog(
            self.store, fetcher=fetcher, timeout=17, force=True
        )
        self.assertEqual(calls, [17, 17])
        self.assertEqual(first["source"], "remote")
        self.assertEqual(second["source"], "cache")
        self.assertEqual(forced["source"], "remote")
        self.assertEqual(second["catalog"], ASSET_CATALOG)
        self.assertTrue(self.store.asset_catalog_is_fresh_today())

    def test_remote_failure_keeps_last_successful_catalog(self):
        self.store.replace_asset_catalog([_asset("a", "AAPL")])
        before = self.store.get_asset_catalog_state()

        def failed(*, timeout):
            raise RuntimeError(f"offline {timeout}")

        with self.assertRaisesRegex(RuntimeError, "offline"):
            C.ensure_daily_asset_catalog(self.store, force=True, fetcher=failed)
        self.assertEqual(self.store.get_asset_catalog_state(), before)
        self.assertEqual(self.store.get_asset_symbols(), ["AAPL"])

    def test_previous_utc_day_is_refreshed(self):
        self.store.replace_asset_catalog(
            [_asset("a", "AAPL")], synced_at="2026-07-09T23:59:59Z"
        )
        calls = []

        def fetcher(*, timeout):
            calls.append(timeout)
            return [_asset("a", "AAPL"), _asset("b", "MSFT")]

        result = C.ensure_daily_asset_catalog(self.store, fetcher=fetcher)
        self.assertEqual(calls, [30])
        self.assertEqual(result["source"], "remote")
        self.assertEqual(self.store.get_asset_symbols(), ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
