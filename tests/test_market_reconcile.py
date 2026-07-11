#!/usr/bin/env python3
"""Offline tests for asset-catalog-driven coverage reconciliation."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import market_reconcile as R  # noqa: E402
from market_data_cache import (  # noqa: E402
    CALENDAR_ADJUSTMENT,
    CALENDAR_FEED,
    CALENDAR_SYMBOL,
)
from market_data_rate_limit import PageRateLimiter, RateLimitRetryPolicy  # noqa: E402
from market_data_store import MarketDataStore  # noqa: E402


class VirtualClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.value += max(0.0, float(seconds))


def _asset(asset_id: str, symbol: str, *, exchange="NASDAQ", tradable=True) -> dict:
    return {
        "id": asset_id, "class": "us_equity", "exchange": exchange,
        "symbol": symbol, "name": symbol, "status": "active",
        "tradable": tradable, "fractionable": False,
        "marginable": True, "shortable": False, "easy_to_borrow": False,
    }


def _bar(day: str, close: float = 100.0) -> dict:
    return {
        "t": f"{day}T04:00:00Z", "o": close - 1, "h": close + 1,
        "l": close - 2, "c": close, "v": 1000,
    }


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarketDataStore(
            Path(self.tmp.name) / "market.sqlite3",
            now_fn=lambda: dt.datetime(2026, 7, 10, 21, 0, tzinfo=dt.timezone.utc),
        )
        self.store.upsert_market_sessions([
            {"session_date": "2026-07-09", "open_at": "09:30", "close_at": "16:00"},
            {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "16:00"},
        ])
        self.store.add_sync_range(
            [CALENDAR_SYMBOL], feed=CALENDAR_FEED, adjustment=CALENDAR_ADJUSTMENT,
            start_date="2025-01-01", end_date="2027-12-31",
        )

    @staticmethod
    def no_calendar(*_args, **_kwargs):
        raise AssertionError("cached calendar should avoid remote fetch")

    @staticmethod
    def retry():
        return RateLimitRetryPolicy(
            max_retries=1, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )

    @staticmethod
    def limiter(clock):
        return PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)


class TestReconcile(ReconcileCase):
    def test_full_catalog_capacity_plans_only_iex_eligible_symbols(self):
        assets = []
        for index in range(14_098):
            tradable = index < 13_268
            exchange = "OTC" if tradable and index < 319 else "NASDAQ"
            assets.append(_asset(
                f"id-{index}", f"S{index:05d}",
                exchange=exchange, tradable=tradable,
            ))
        state = self.store.replace_asset_catalog(assets)
        universe = self.store.get_asset_symbols()
        plan = R.build_warmup_plan(
            self.store, universe, latest_session="2026-07-10",
            years=3, batch_size=50,
        )
        self.assertEqual(state["current_count"], 14_098)
        self.assertEqual(state["tradable_count"], 13_268)
        self.assertEqual(state["eligible_count"], 12_949)
        self.assertEqual(len(universe), 12_949)
        self.assertEqual(plan.pending_symbols, 12_949)
        self.assertEqual(len(plan.batches), 259)
        self.assertFalse(hasattr(plan, "bars"))

    def test_dry_run_refreshes_catalog_once_but_requests_no_bars(self):
        calls = []

        def assets(*, timeout):
            calls.append(timeout)
            return [
                _asset("a", "AAPL"),
                _asset("b", "BRK.B", exchange="NYSE"),
                _asset("c", "OTCM", exchange="OTC"),
                _asset("d", "NOPE", tradable=False),
            ]

        def forbidden_remote(*_args, **_kwargs):
            raise AssertionError("dry-run must not request bars")

        clock = VirtualClock()
        result = R.run_market_reconcile(
            store=self.store, dry_run=True, asset_fetcher=assets,
            remote_transport=forbidden_remote, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            clock=clock.monotonic,
        )
        self.assertEqual(calls, [30])
        self.assertEqual(result["stop_reason"], "dry_run")
        self.assertEqual(result["universe"], 2)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["pages"], 0)
        self.assertEqual(result["asset_catalog"]["source"], "remote")
        self.assertEqual(result["asset_catalog"]["current_count"], 4)
        self.assertEqual(result["asset_catalog"]["eligible_count"], 2)
        self.assertEqual(
            result["asset_catalog"]["exclusions"],
            {"eligible": 2, "not_tradable": 1, "unsupported_iex_otc": 1},
        )
        self.assertEqual(result["bar_presence"]["symbols_without_final_bars"], 2)

        def forbidden_assets(*, timeout):
            raise AssertionError("same UTC day must use cached catalog")

        second_clock = VirtualClock()
        second = R.run_market_reconcile(
            store=self.store, dry_run=True, asset_fetcher=forbidden_assets,
            remote_transport=forbidden_remote, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(second_clock), retry_policy=self.retry(),
            clock=second_clock.monotonic,
        )
        self.assertEqual(second["asset_catalog"]["source"], "cache")
        self.assertEqual(second["pages"], 0)

    def test_reconcile_repairs_only_eligible_gaps_then_repeats_with_zero_pages(self):
        self.store.replace_asset_catalog([
            _asset("a", "AAPL"), _asset("b", "BRK.B", exchange="NYSE"),
            _asset("c", "OTCM", exchange="OTC"),
        ])
        calls = []

        def remote(symbols, _start, end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            calls.append(tuple(symbols))
            return ({symbol: [_bar(end)] for symbol in symbols}, feed, None)

        clock = VirtualClock()
        result = R.run_market_reconcile(
            store=self.store, workers=2, batch_size=50,
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "complete")
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(calls, [("AAPL", "BRK.B")])
        self.assertNotIn("OTCM", {symbol for group in calls for symbol in group})

        def forbidden(*_args, **_kwargs):
            raise AssertionError("complete repeat must not request bars")

        repeat_clock = VirtualClock()
        repeated = R.run_market_reconcile(
            store=self.store,
            remote_transport=forbidden, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(repeat_clock), retry_policy=self.retry(),
            clock=repeat_clock.monotonic,
        )
        self.assertEqual(repeated["covered"], 2)
        self.assertEqual(repeated["pending"], 0)
        self.assertEqual(repeated["pages"], 0)
        self.assertEqual(repeated["asset_catalog"]["source"], "cache")
        self.assertEqual(repeated["bar_presence"]["symbols_with_final_bars"], 2)

    def test_force_assets_refreshes_an_already_fresh_catalog(self):
        self.store.replace_asset_catalog([_asset("a", "AAPL")])
        calls = []

        def assets(*, timeout):
            calls.append(timeout)
            return [_asset("a", "AAPL"), _asset("b", "MSFT")]

        clock = VirtualClock()
        result = R.run_market_reconcile(
            store=self.store, dry_run=True, force_assets=True,
            asset_fetcher=assets, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            clock=clock.monotonic,
        )
        self.assertEqual(calls, [30])
        self.assertEqual(result["universe"], 2)
        self.assertEqual(result["asset_catalog"]["source"], "remote")

    def test_symbols_file_canary_is_intersected_with_eligible_catalog(self):
        self.store.replace_asset_catalog([
            _asset("a", "AAPL"), _asset("b", "MSFT"),
            _asset("c", "OTCM", exchange="OTC"),
        ])
        source = Path(self.tmp.name) / "symbols.txt"
        source.write_text("AAPL\n", encoding="utf-8")
        calls = []

        def remote(symbols, _start, end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            calls.append(tuple(symbols))
            return ({symbols[0]: [_bar(end)]}, feed, None)

        clock = VirtualClock()
        result = R.run_market_reconcile(
            store=self.store, symbols_file=source,
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            clock=clock.monotonic,
        )
        self.assertTrue(result["scope_limited"])
        self.assertEqual(result["universe"], 1)
        self.assertEqual(calls, [("AAPL",)])


if __name__ == "__main__":
    unittest.main()
