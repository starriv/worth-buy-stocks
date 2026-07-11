#!/usr/bin/env python3
"""Offline tests for whole-market discovery, planning, resume, and bounds."""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import market_warmup as W  # noqa: E402
import fetching  # noqa: E402
from market_data_cache import (  # noqa: E402
    CALENDAR_ADJUSTMENT,
    CALENDAR_FEED,
    CALENDAR_SYMBOL,
)
from market_data_rate_limit import PageRateLimiter, RateLimitRetryPolicy  # noqa: E402
from market_data_store import (  # noqa: E402
    CacheBusyError,
    MarketDataCacheError,
    MarketDataStore,
)


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


def _bar(day: str, close: float = 100.0) -> dict:
    return {
        "t": f"{day}T04:00:00Z", "o": close - 1, "h": close + 1,
        "l": close - 2, "c": close, "v": 1000,
    }


class WarmupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "market.sqlite3"
        self.store = MarketDataStore(
            self.db,
            now_fn=lambda: dt.datetime(2026, 7, 10, 21, 0, tzinfo=dt.timezone.utc),
        )
        self.store.upsert_market_sessions([
            {"session_date": "2026-07-08", "open_at": "09:30", "close_at": "16:00"},
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
    def limiter(clock: VirtualClock) -> PageRateLimiter:
        return PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)

    @staticmethod
    def retry() -> RateLimitRetryPolicy:
        return RateLimitRetryPolicy(
            max_retries=1, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )


class TestUniverseAndDates(WarmupCase):
    def test_discovery_calls_combined_exchanges_once_and_sorts_scanner_filter(self):
        calls = []

        def assets(**kwargs):
            calls.append(kwargs)
            return [
                {"symbol": "SPY", "fractionable": True},
                {"symbol": "BRK.B", "fractionable": True},
                {"symbol": "aapl", "fractionable": True},
                {"symbol": "SPY", "fractionable": True},
                {"symbol": "NOFRAC", "fractionable": False},
            ]

        symbols, limited = W.discover_universe(asset_fetcher=assets, timeout=17)
        self.assertEqual(symbols, ["AAPL", "SPY"])
        self.assertFalse(limited)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["exchanges"], ("NYSE", "NASDAQ"))
        self.assertEqual(calls[0]["timeout"], 17)

    def test_symbols_file_skips_asset_discovery_and_marks_limited(self):
        source = Path(self.tmp.name) / "symbols.txt"
        source.write_text("msft, AAPL\nBRK.B AAPL\n", encoding="utf-8")

        def forbidden(**_kwargs):
            raise AssertionError("asset discovery must be skipped")

        symbols, limited = W.discover_universe(
            symbols_file=source, asset_fetcher=forbidden
        )
        self.assertEqual(symbols, ["AAPL", "MSFT"])
        self.assertTrue(limited)

    def test_three_year_start_handles_leap_day(self):
        self.assertEqual(W.start_date_for_years("2026-07-10", 3), "2023-07-10")
        self.assertEqual(W.start_date_for_years("2024-02-29", 3), "2021-02-28")


class TestPlanning(WarmupCase):
    def test_partial_middle_gap_is_the_only_batch(self):
        self.store.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2023-07-10", end_date="2025-01-01",
        )
        self.store.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2025-01-03", end_date="2026-07-10",
        )
        plan = W.build_warmup_plan(
            self.store, ["AAPL"], latest_session="2026-07-10", years=3
        )
        self.assertEqual(plan.covered, 0)
        self.assertEqual(plan.pending_ranges, 1)
        self.assertEqual(
            plan.batches,
            (W.WarmupBatch("2025-01-02", "2025-01-02", ("AAPL",)),),
        )

    def test_five_thousand_symbols_plan_is_bounded_to_batches_not_bars(self):
        symbols = [f"S{i:04d}" for i in range(5000)]
        plan = W.build_warmup_plan(
            self.store, symbols, latest_session="2026-07-10",
            years=3, batch_size=50,
        )
        self.assertEqual(len(plan.universe), 5000)
        self.assertEqual(plan.pending_symbols, 5000)
        self.assertEqual(len(plan.batches), 100)
        self.assertTrue(all(len(batch.symbols) <= 50 for batch in plan.batches))
        self.assertFalse(hasattr(plan, "bars"))


class TestExecution(WarmupCase):
    def test_dry_run_requests_no_bars_and_writes_no_symbol_coverage(self):
        source = Path(self.tmp.name) / "canary.txt"
        source.write_text("AAPL\nMSFT\n", encoding="utf-8")
        clock = VirtualClock()

        def forbidden_remote(*_args, **_kwargs):
            raise AssertionError("dry-run must not request bars")

        def forbidden_assets(**_kwargs):
            raise AssertionError("symbols-file must skip asset discovery")

        result = W.run_market_warmup(
            store=self.store, symbols_file=source, dry_run=True,
            asset_fetcher=forbidden_assets, calendar_fetcher=self.no_calendar,
            remote_transport=forbidden_remote,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "dry_run")
        self.assertTrue(result["scope_limited"])
        self.assertEqual(result["pages"], 0)
        self.assertEqual(result["estimated_rows"], 2 * 785)
        self.assertTrue(self.store.missing_ranges(
            "AAPL", feed="iex", adjustment="split",
            start_date=result["range"]["start"], end_date=result["range"]["end"],
        ))

    def test_invalid_options_fail_before_asset_discovery(self):
        calls = []

        def assets(**kwargs):
            calls.append(kwargs)
            return []

        with self.assertRaisesRegex(ValueError, "workers"):
            W.run_market_warmup(store=self.store, workers=100, asset_fetcher=assets)
        self.assertEqual(calls, [])

    def test_success_then_same_plan_has_zero_remote_batches(self):
        plan = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=3, batch_size=50,
        )
        clock = VirtualClock()
        calls = []

        def remote(symbols, start, end, feed, adjustment, limit, timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            calls.append((tuple(symbols), start, end))
            return ({symbol: [_bar(end)] for symbol in symbols}, feed, None)

        result = W.execute_warmup_plan(
            self.store, plan, workers=4, max_runtime=60,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "complete")
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(len(calls), 1)

        second = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=3, batch_size=50,
        )
        self.assertEqual(second.covered, 2)
        self.assertEqual(second.batches, ())

    def test_empty_historical_response_can_record_successful_coverage(self):
        plan = W.build_warmup_plan(
            self.store, ["NEWCO"], latest_session="2026-07-09",
            years=1, batch_size=50,
        )
        clock = VirtualClock()

        def remote(_symbols, _start, _end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            return {}, feed, None

        result = W.execute_warmup_plan(
            self.store, plan, workers=1, max_runtime=60,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "complete")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(
            self.store.missing_ranges(
                "NEWCO", feed="iex", adjustment="split",
                start_date=plan.start, end_date=plan.end,
            ),
            [],
        )

    def test_batch_failure_does_not_write_coverage(self):
        plan = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=1, batch_size=50,
        )
        clock = VirtualClock()

        def remote(_symbols, _start, _end, _feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            raise RuntimeError("offline fixture failure")

        result = W.execute_warmup_plan(
            self.store, plan, workers=2, max_runtime=60,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "batch_failures")
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["pending"], 2)
        self.assertTrue(self.store.missing_ranges(
            "AAPL", feed="iex", adjustment="split",
            start_date=plan.start, end_date=plan.end,
        ))

    def test_one_failed_batch_does_not_block_other_batches(self):
        plan = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=1, batch_size=1,
        )
        clock = VirtualClock()

        def remote(symbols, _start, end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            if symbols == ["AAPL"] or tuple(symbols) == ("AAPL",):
                raise RuntimeError("isolated AAPL failure")
            return ({symbols[0]: [_bar(end)]}, feed, None)

        result = W.execute_warmup_plan(
            self.store, plan, workers=2, max_runtime=60,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "batch_failures")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(
            self.store.missing_ranges(
                "MSFT", feed="iex", adjustment="split",
                start_date=plan.start, end_date=plan.end,
            ),
            [],
        )

        resume = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=1, batch_size=1,
        )
        self.assertEqual(
            resume.batches,
            (W.WarmupBatch(plan.start, plan.end, ("AAPL",)),),
        )
        resumed_calls = []

        def recover(symbols, _start, end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            resumed_calls.append(tuple(symbols))
            return ({symbols[0]: [_bar(end)]}, feed, None)

        resumed_clock = VirtualClock()
        resumed = W.execute_warmup_plan(
            self.store, resume, workers=1, max_runtime=60,
            limiter=self.limiter(resumed_clock), retry_policy=self.retry(),
            remote_transport=recover, calendar_fetcher=self.no_calendar,
            clock=resumed_clock.monotonic,
        )
        self.assertEqual(resumed["stop_reason"], "complete")
        self.assertEqual(resumed_calls, [("AAPL",)])

    def test_sqlite_locked_or_read_only_write_keeps_gap_missing(self):
        for error in (
            CacheBusyError("locked fixture"),
            MarketDataCacheError("read-only fixture"),
        ):
            with self.subTest(error=type(error).__name__):
                db = Path(self.tmp.name) / f"{type(error).__name__}.sqlite3"
                store = MarketDataStore(
                    db,
                    now_fn=lambda: dt.datetime(2026, 7, 10, 21, 0, tzinfo=dt.timezone.utc),
                )
                store.upsert_market_sessions([
                    {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "16:00"}
                ])
                store.add_sync_range(
                    [CALENDAR_SYMBOL], feed=CALENDAR_FEED,
                    adjustment=CALENDAR_ADJUSTMENT,
                    start_date="2025-01-01", end_date="2027-12-31",
                )
                plan = W.build_warmup_plan(
                    store, ["AAPL"], latest_session="2026-07-10", years=1
                )
                clock = VirtualClock()

                def remote(symbols, _start, end, feed, _adj, _limit, _timeout, **hooks):
                    hooks["request_limiter"].acquire(hooks["stop_event"])
                    return ({symbols[0]: [_bar(end)]}, feed, None)

                with patch.object(store, "commit_daily_sync", side_effect=error):
                    result = W.execute_warmup_plan(
                        store, plan, workers=1, max_runtime=60,
                        limiter=self.limiter(clock), retry_policy=self.retry(),
                        remote_transport=remote, calendar_fetcher=self.no_calendar,
                        clock=clock.monotonic,
                    )
                self.assertEqual(result["stop_reason"], "batch_failures")
                self.assertEqual(result["pending"], 1)
                self.assertTrue(store.missing_ranges(
                    "AAPL", feed="iex", adjustment="split",
                    start_date=plan.start, end_date=plan.end,
                ))

    def test_exhausted_429_retries_leave_no_false_coverage(self):
        plan = W.build_warmup_plan(
            self.store, ["AAPL"], latest_session="2026-07-10", years=1
        )
        clock = VirtualClock()
        response = subprocess.CompletedProcess(
            ["alpaca"], 1, stdout="", stderr="HTTP 429 rate limit"
        )
        retry = RateLimitRetryPolicy(
            max_retries=1, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )
        with patch.object(fetching.subprocess, "run", return_value=response):
            result = W.execute_warmup_plan(
                self.store, plan, workers=1, max_runtime=60,
                limiter=self.limiter(clock), retry_policy=retry,
                remote_transport=fetching._fetch_bars_remote,
                calendar_fetcher=self.no_calendar, clock=clock.monotonic,
            )
        self.assertEqual(result["stop_reason"], "batch_failures")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["retries"], 1)
        self.assertEqual(result["rate_limits"], 2)
        self.assertTrue(self.store.missing_ranges(
            "AAPL", feed="iex", adjustment="split",
            start_date=plan.start, end_date=plan.end,
        ))

    def test_max_runtime_stops_dispatch_and_preserves_first_commit(self):
        plan = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=1, batch_size=1,
        )
        clock = VirtualClock()
        calls = []

        def remote(symbols, _start, end, feed, _adjustment, _limit, _timeout, **hooks):
            hooks["request_limiter"].acquire(hooks["stop_event"])
            calls.append(tuple(symbols))
            clock.sleep(2)
            return ({symbols[0]: [_bar(end)]}, feed, None)

        result = W.execute_warmup_plan(
            self.store, plan, workers=1, max_runtime=1,
            limiter=self.limiter(clock), retry_policy=self.retry(),
            remote_transport=remote, calendar_fetcher=self.no_calendar,
            clock=clock.monotonic,
        )
        self.assertEqual(result["stop_reason"], "max_runtime")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 1)
        resume = W.build_warmup_plan(
            self.store, ["AAPL", "MSFT"], latest_session="2026-07-10",
            years=1, batch_size=1,
        )
        self.assertEqual(len(resume.batches), 1)
        self.assertEqual(resume.batches[0].symbols, ("MSFT",))


if __name__ == "__main__":
    unittest.main()
