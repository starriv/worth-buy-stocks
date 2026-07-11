#!/usr/bin/env python3
"""Offline session/read-through tests for market_data_cache and fetching facade."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetching as F  # noqa: E402
import market_data_cache as C  # noqa: E402
from market_data_store import CacheBusyError, MarketDataStore  # noqa: E402


def _bar(day: str, close: float) -> dict:
    return {
        "t": f"{day}T04:00:00Z",
        "o": close - 1,
        "h": close + 1,
        "l": close - 2,
        "c": close,
        "v": 1000,
        "vw": close - 0.2,
        "n": 10,
    }


SESSIONS = [
    {"session_date": "2026-07-06", "open_at": "09:30", "close_at": "16:00"},
    {"session_date": "2026-07-07", "open_at": "09:30", "close_at": "16:00"},
    {"session_date": "2026-07-08", "open_at": "09:30", "close_at": "16:00"},
    {"session_date": "2026-07-09", "open_at": "09:30", "close_at": "16:00"},
    {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "13:00"},
]

DATASET = {
    "AAPL": [
        _bar("2026-07-06", 100), _bar("2026-07-07", 101),
        _bar("2026-07-08", 102), _bar("2026-07-09", 103),
        _bar("2026-07-10", 104),
    ],
    "SPY": [
        _bar("2026-07-06", 500), _bar("2026-07-07", 501),
        _bar("2026-07-08", 502), _bar("2026-07-09", 503),
        _bar("2026-07-10", 504),
    ],
}


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class Harness:
    def __init__(
        self, db: Path, now: dt.datetime, *,
        actual_feed: str | None = None, dataset: dict | None = None,
    ):
        self.store = MarketDataStore(db, now_fn=lambda: now)
        self.calendar_calls: list[tuple[str, str]] = []
        self.remote_calls: list[tuple[tuple[str, ...], str, str, str]] = []
        self.actual_feed = actual_feed
        self.dataset = DATASET if dataset is None else dataset

    def calendar(self, start, end, timeout):
        self.calendar_calls.append((start, end))
        return list(SESSIONS)

    def remote(self, symbols, start, end, feed, adjustment, limit, timeout):
        self.remote_calls.append((tuple(symbols), start, end, feed))
        bars = {
            symbol: [dict(bar) for bar in self.dataset.get(symbol, []) if start <= bar["t"][:10] <= end]
            for symbol in symbols
        }
        bars = {symbol: rows for symbol, rows in bars.items() if rows}
        actual = self.actual_feed or feed
        note = "sip 已回退 iex" if actual != feed else None
        return bars, actual, note

    def cache(self):
        return C.MarketDataCache(
            self.store,
            remote_bars_fetcher=self.remote,
            calendar_fetcher=self.calendar,
        )


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "market.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()


class TestSessionClassification(unittest.TestCase):
    def test_pre_open_regular_open_early_close_grace_and_finalized(self):
        pre = C.classify_session(_utc("2026-07-10T12:00:00Z"), SESSIONS)
        opened = C.classify_session(_utc("2026-07-10T14:00:00Z"), SESSIONS)
        grace = C.classify_session(_utc("2026-07-10T17:05:00Z"), SESSIONS)
        closed = C.classify_session(_utc("2026-07-10T17:16:00Z"), SESSIONS)
        self.assertEqual((pre.phase, pre.latest_completed_session), ("pre_market", "2026-07-09"))
        self.assertEqual((opened.phase, opened.latest_completed_session), ("open", "2026-07-09"))
        self.assertEqual((grace.phase, grace.latest_completed_session), ("post_close_grace", "2026-07-09"))
        self.assertEqual((closed.phase, closed.latest_completed_session), ("closed", "2026-07-10"))

    def test_weekend_and_weekday_holiday_use_previous_session(self):
        weekend = C.classify_session(_utc("2026-07-11T15:00:00Z"), SESSIONS)
        holiday_sessions = [row for row in SESSIONS if row["session_date"] != "2026-07-08"]
        holiday = C.classify_session(_utc("2026-07-08T15:00:00Z"), holiday_sessions)
        self.assertEqual((weekend.phase, weekend.latest_completed_session), ("non_trading", "2026-07-10"))
        self.assertEqual((holiday.phase, holiday.latest_completed_session), ("non_trading", "2026-07-07"))


class TestReadThrough(PolicyCase):
    def test_cold_then_warm_pre_market_uses_database_only(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        cache = h.cache()
        cold, used_feed, note = cache.load_bars(
            ["AAPL", "SPY"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(used_feed, "iex")
        self.assertEqual(cold["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("pre_market", note)
        self.assertEqual(len(h.calendar_calls), 1)
        self.assertEqual(len(h.remote_calls), 1)

        warm, _, warm_note = cache.load_bars(
            ["AAPL", "SPY"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(warm, cold)
        self.assertEqual(len(h.calendar_calls), 1)
        self.assertEqual(len(h.remote_calls), 1)
        self.assertIn("远端补充 0 根", warm_note)

    def test_open_session_never_enters_completed_bar_result(self):
        h = Harness(self.db, _utc("2026-07-10T14:00:00Z"))
        result, _, note = h.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertEqual(h.remote_calls[0][2], "2026-07-09")
        self.assertIn("open", note)

    def test_early_close_grace_waits_then_finalizes_once_with_overlap(self):
        pre = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        pre.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )

        grace = Harness(self.db, _utc("2026-07-10T17:05:00Z"))
        during, _, _ = grace.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(during["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertEqual(grace.remote_calls, [])

        after = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        finalized, _, note = after.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(finalized["AAPL"][-1]["t"][:10], "2026-07-10")
        self.assertEqual(after.remote_calls, [(('AAPL',), "2026-07-06", "2026-07-10", "iex")])
        self.assertIn("closed", note)
        after.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(len(after.remote_calls), 1)

    def test_weekend_warm_cache_does_not_call_remote(self):
        friday = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        friday.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-11",
            "iex", "split", 10000, 30,
        )
        weekend = Harness(self.db, _utc("2026-07-11T15:00:00Z"))
        result, _, note = weekend.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-11",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(weekend.remote_calls, [])
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-10")
        self.assertIn("non_trading", note)

    def test_refresh_replaces_covered_range(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        cache = h.cache()
        cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        DATASET["AAPL"][1]["c"] = 222
        try:
            refreshed, _, _ = cache.load_bars(
                ["AAPL"], "2026-07-06", "2026-07-10",
                "iex", "split", 10000, 30, mode="refresh",
            )
        finally:
            DATASET["AAPL"][1]["c"] = 101
        self.assertEqual(len(h.remote_calls), 2)
        self.assertEqual(refreshed["AAPL"][1]["c"], 222)

    def test_overlap_revision_triggers_full_symbol_refresh(self):
        pre = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        pre.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        pre.store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-08", 999)]}, feed="iex", adjustment="split"
        )
        after = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        result, _, note = after.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(len(after.remote_calls), 2)
        self.assertEqual(result["AAPL"][2]["c"], 102)
        self.assertIn("已刷新全部缓存历史", note)

    def test_overlap_revision_heals_cached_history_before_requested_range(self):
        pre = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        pre.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        # Simulate a stale adjustment baseline: both a row inside the next
        # request (triggers detection) and one before it (must be healed too).
        pre.store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-06", 999), _bar("2026-07-08", 999)]},
            feed="iex", adjustment="split",
        )
        after = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        result, _, note = after.cache().load_bars(
            ["AAPL"], "2026-07-08", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(after.remote_calls[-1][1:3], ("2026-07-06", "2026-07-10"))
        self.assertEqual(result["AAPL"][0]["c"], 102)
        healed = after.store.get_daily_bars(
            ["AAPL"], start_date="2026-07-06", end_date="2026-07-06",
            feed="iex", adjustment="split",
        )
        self.assertEqual(healed["AAPL"][0]["c"], 100)
        self.assertIn("已刷新全部缓存历史", note)

    def test_refresh_mode_heals_cached_history_before_requested_range(self):
        pre = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        pre.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        pre.store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-06", 999), _bar("2026-07-09", 999)]},
            feed="iex", adjustment="split",
        )
        after = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        after.cache().load_bars(
            ["AAPL"], "2026-07-08", "2026-07-10",
            "iex", "split", 10000, 30, mode="refresh",
        )
        healed = after.store.get_daily_bars(
            ["AAPL"], start_date="2026-07-06", end_date="2026-07-06",
            feed="iex", adjustment="split",
        )
        self.assertEqual(healed["AAPL"][0]["c"], 100)

    def test_sip_fallback_is_stored_only_under_actual_iex_feed(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"), actual_feed="iex")
        result, used_feed, note = h.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "sip", "split", 10000, 30,
        )
        self.assertEqual(used_feed, "iex")
        self.assertIn("回退", note)
        self.assertIn("AAPL", result)
        self.assertEqual(
            h.store.get_daily_bars(
                ["AAPL"], start_date="2026-07-06", end_date="2026-07-09",
                feed="sip", adjustment="split",
            ),
            {},
        )
        self.assertIn("AAPL", h.store.get_daily_bars(
            ["AAPL"], start_date="2026-07-06", end_date="2026-07-09",
            feed="iex", adjustment="split",
        ))

    def test_write_failure_returns_fetched_data_without_false_coverage(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        cache = h.cache()
        with patch.object(h.store, "commit_daily_sync", side_effect=CacheBusyError("locked")):
            result, _, note = cache.load_bars(
                ["AAPL"], "2026-07-06", "2026-07-10",
                "iex", "split", 10000, 30,
            )
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("数据库写入不可用", note)
        self.assertNotEqual(
            h.store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-06", end_date="2026-07-09",
            ),
            [],
        )

    def test_tail_session_bar_not_published_keeps_gap_until_available(self):
        dataset = {
            "AAPL": [
                _bar("2026-07-06", 100), _bar("2026-07-07", 101),
                _bar("2026-07-08", 102), _bar("2026-07-09", 103),
            ]
        }
        h = Harness(self.db, _utc("2026-07-10T17:16:00Z"), dataset=dataset)
        cache = h.cache()
        first, _, note = cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(first["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("尚未发布", note)
        self.assertEqual(
            h.store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-06", end_date="2026-07-10",
            ),
            [("2026-07-10", "2026-07-10")],
        )

        dataset["AAPL"].append(_bar("2026-07-10", 104))
        second, _, _ = cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(second["AAPL"][-1]["t"][:10], "2026-07-10")
        self.assertEqual(
            h.store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-06", end_date="2026-07-10",
            ),
            [],
        )
        remote_calls = len(h.remote_calls)
        cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(len(h.remote_calls), remote_calls)

    def test_feed_fallback_is_learned_and_reused_without_refetch(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"), actual_feed="iex")
        cache = h.cache()
        first, first_feed, _ = cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "sip", "split", 10000, 30,
        )
        self.assertEqual(first_feed, "iex")
        self.assertEqual(h.store.get_feed_fallback("sip"), "iex")
        self.assertEqual(len(h.remote_calls), 1)

        second, second_feed, note = cache.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "sip", "split", 10000, 30,
        )
        self.assertEqual(len(h.remote_calls), 1)
        self.assertEqual(second_feed, "iex")
        self.assertEqual(second, first)
        self.assertIn("此前受限", note)

    def test_warm_cache_survives_remote_outage_with_degraded_note(self):
        pre = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        pre.cache().load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        after = Harness(self.db, _utc("2026-07-10T17:16:00Z"))
        down = C.MarketDataCache(
            after.store,
            remote_bars_fetcher=lambda *args: (_ for _ in ()).throw(RuntimeError("bars down")),
            calendar_fetcher=after.calendar,
        )
        result, _, note = down.load_bars(
            ["AAPL"], "2026-07-06", "2026-07-10",
            "iex", "split", 10000, 30,
        )
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("远端拉取失败", note)
        self.assertEqual(
            after.store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-06", end_date="2026-07-10",
            ),
            [("2026-07-10", "2026-07-10")],
        )

    def test_remote_failure_on_cold_cache_is_reported_without_false_coverage(self):
        h = Harness(self.db, _utc("2026-07-10T12:00:00Z"))
        cache = C.MarketDataCache(
            h.store,
            remote_bars_fetcher=lambda *args: (_ for _ in ()).throw(RuntimeError("bars down")),
            calendar_fetcher=h.calendar,
        )
        with self.assertRaisesRegex(RuntimeError, "bars down"):
            cache.load_bars(
                ["AAPL"], "2026-07-06", "2026-07-10",
                "iex", "split", 10000, 30,
            )
        self.assertNotEqual(
            h.store.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-06", end_date="2026-07-09",
            ),
            [],
        )


def _snapshot_context(day: str, close: float = 104.0) -> dict:
    return {
        "status": "ok",
        "as_of": f"{day}T15:00:00Z",
        "feed": "iex",
        "symbols": {
            "AAPL": {
                "symbol": "AAPL",
                "daily_bar": {
                    "date": day, "open": close - 1, "high": close + 1,
                    "low": close - 2, "close": close, "volume": 5000,
                    "vwap": close - 0.2,
                },
            }
        },
    }


class TestSnapshotReadThrough(PolicyCase):
    def _cache(self, now, remote):
        store = MarketDataStore(self.db, now_fn=lambda: now)
        cache = C.MarketDataCache(
            store,
            remote_bars_fetcher=lambda *args: ({}, "iex", None),
            calendar_fetcher=lambda *args: list(SESSIONS),
            remote_snapshots_fetcher=remote,
        )
        return store, cache

    def test_open_refreshes_remote_and_atomically_writes_snapshot_and_live_bar(self):
        calls = []

        def remote(symbols, feed, timeout):
            calls.append((tuple(symbols), feed))
            return _snapshot_context("2026-07-10")

        store, cache = self._cache(_utc("2026-07-10T14:00:00Z"), remote)
        store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-09", 103)]}, feed="iex", adjustment="split"
        )
        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(calls, [(('AAPL',), "iex")])
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["date"], "2026-07-10")
        self.assertEqual(
            store.get_live_daily_bars(["AAPL"], feed="iex")["AAPL"]["t"][:10],
            "2026-07-10",
        )
        final = store.get_latest_daily_bars(
            ["AAPL"], feed="iex", adjustment="split"
        )
        self.assertEqual(final["AAPL"]["t"][:10], "2026-07-09")

    def test_pre_market_reads_cached_snapshot_and_synthesizes_missing_final_bar(self):
        def forbidden(*args):
            raise AssertionError("pre-market snapshot transport must not run")

        store, cache = self._cache(_utc("2026-07-10T12:00:00Z"), forbidden)
        store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-09", 103)], "SPY": [_bar("2026-07-09", 503)]},
            feed="iex", adjustment="split",
        )
        spy = _snapshot_context("2026-07-09", 503)
        spy["symbols"] = {"SPY": {**spy["symbols"]["AAPL"], "symbol": "SPY"}}
        store.upsert_snapshot_context(spy, include_live=False)
        context = cache.load_snapshots(["AAPL", "SPY"], "iex", 30)
        self.assertEqual(list(context["symbols"]), ["AAPL", "SPY"])
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["date"], "2026-07-09")
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["volume"], 1000)
        self.assertEqual(context["symbols"]["SPY"]["daily_bar"]["close"], 503)
        self.assertIn("pre_market", context["cache_note"])

    def test_pre_market_partial_cache_fetches_only_missing_symbol_without_live_row(self):
        calls = []

        def remote(symbols, feed, timeout):
            calls.append(list(symbols))
            context = _snapshot_context("2026-07-09", 50)
            context["symbols"] = {
                "NEW": {**context["symbols"]["AAPL"], "symbol": "NEW"}
            }
            return context

        store, cache = self._cache(_utc("2026-07-10T12:00:00Z"), remote)
        store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-09", 103)]}, feed="iex", adjustment="split"
        )
        context = cache.load_snapshots(["AAPL", "NEW"], "iex", 30)
        self.assertEqual(calls, [["NEW"]])
        self.assertEqual(list(context["symbols"]), ["AAPL", "NEW"])
        self.assertIn("远端补充 1 只", context["cache_note"])
        self.assertEqual(store.get_live_daily_bars(["NEW"], feed="iex"), {})
        self.assertEqual(
            store.get_latest_snapshots(["NEW"], feed="iex")["status"], "ok"
        )

    def test_weekend_uses_database_without_snapshot_transport(self):
        calls = []
        store, cache = self._cache(
            _utc("2026-07-11T15:00:00Z"),
            lambda *args: calls.append(args) or _snapshot_context("2026-07-10"),
        )
        store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-10", 104)]}, feed="iex", adjustment="split"
        )
        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(calls, [])
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["date"], "2026-07-10")
        self.assertIn("non_trading", context["cache_note"])

    def test_refresh_outside_market_updates_latest_without_creating_live_bar(self):
        store, cache = self._cache(
            _utc("2026-07-10T12:00:00Z"),
            lambda *args: _snapshot_context("2026-07-09"),
        )
        context = cache.load_snapshots(["AAPL"], "iex", 30, mode="refresh")
        self.assertEqual(context["status"], "ok")
        self.assertEqual(store.get_latest_snapshots(["AAPL"], feed="iex")["status"], "ok")
        self.assertEqual(store.get_live_daily_bars(["AAPL"], feed="iex"), {})

    def test_open_remote_failure_uses_cached_snapshot_with_stale_flag(self):
        store, cache = self._cache(
            _utc("2026-07-10T14:00:00Z"),
            lambda *args: {"status": "unavailable", "reason": "snapshot timeout"},
        )
        store.upsert_snapshot_context(_snapshot_context("2026-07-09"), include_live=False)
        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertTrue(context["stale"])
        self.assertIn("snapshot timeout", context["cache_note"])

    def test_closed_session_replaces_intraday_snapshot_via_remote(self):
        calls = []

        def remote(symbols, feed, timeout):
            calls.append(list(symbols))
            context = _snapshot_context("2026-07-10", 106.0)
            context["as_of"] = "2026-07-10T18:00:00Z"
            return context

        store, cache = self._cache(_utc("2026-07-10T18:30:00Z"), remote)
        intraday = _snapshot_context("2026-07-10", 104.0)  # 11:00 ET capture
        store.upsert_snapshot_context(intraday, include_live=False)

        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(calls, [["AAPL"]])
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["close"], 106.0)
        self.assertNotIn("stale", context)

        again = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(calls, [["AAPL"]])
        self.assertEqual(again["symbols"]["AAPL"]["daily_bar"]["close"], 106.0)

    def test_closed_session_upgrades_intraday_snapshot_from_final_bar(self):
        def forbidden(*args):
            raise AssertionError("final-bar upgrade must stay network-free")

        store, cache = self._cache(_utc("2026-07-10T18:30:00Z"), forbidden)
        store.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-10", 104)]}, feed="iex", adjustment="split"
        )
        store.upsert_snapshot_context(
            _snapshot_context("2026-07-10", 101.5), include_live=False
        )
        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["close"], 104)
        self.assertNotIn("stale", context)

    def test_closed_session_stale_snapshot_degrades_with_flag_when_remote_down(self):
        store, cache = self._cache(
            _utc("2026-07-10T18:30:00Z"),
            lambda *args: {"status": "unavailable", "reason": "snapshot down"},
        )
        store.upsert_snapshot_context(
            _snapshot_context("2026-07-10", 104.0), include_live=False
        )
        context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertTrue(context["stale"])
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["close"], 104.0)
        self.assertIn("盘中快照", context["cache_note"])

    def test_snapshot_write_failure_returns_remote_context(self):
        store, cache = self._cache(
            _utc("2026-07-10T14:00:00Z"),
            lambda *args: _snapshot_context("2026-07-10"),
        )
        with patch.object(
            store, "upsert_snapshot_context", side_effect=CacheBusyError("locked")
        ):
            context = cache.load_snapshots(["AAPL"], "iex", 30)
        self.assertEqual(context["status"], "ok")
        self.assertIn("写入不可用", context["cache_note"])
        self.assertEqual(store.get_latest_snapshots(["AAPL"], feed="iex")["status"], "unavailable")


class TestFetchingFacade(PolicyCase):
    def test_calendar_transport_normalizes_cli_shape(self):
        raw = {"calendar": [{
            "date": "2026-07-10",
            "core_start": "2026-07-10T09:30:00-04:00",
            "core_end": "2026-07-10T13:00:00-04:00",
        }]}
        with patch("fetching._run_json", return_value=raw) as run:
            sessions = F._fetch_market_calendar("2026-01-01", "2026-12-31", 30)
        self.assertEqual(sessions, [{
            "session_date": "2026-07-10",
            "open_at": "2026-07-10T09:30:00-04:00",
            "close_at": "2026-07-10T13:00:00-04:00",
        }])
        self.assertIn("XNYS", run.call_args.args[0])

    def test_empty_multi_year_calendar_is_not_accepted_as_coverage(self):
        store = MarketDataStore(self.db, now_fn=lambda: _utc("2026-07-10T12:00:00Z"))
        cache = C.MarketDataCache(
            store,
            remote_bars_fetcher=lambda *args: ({}, "iex", None),
            calendar_fetcher=lambda *args: [],
        )
        with self.assertRaisesRegex(C.CachePolicyError, "returned no sessions"):
            cache.ensure_calendar(30)
        self.assertNotEqual(
            store.missing_ranges(
                C.CALENDAR_SYMBOL, feed=C.CALENDAR_FEED,
                adjustment=C.CALENDAR_ADJUSTMENT,
                start_date="2025-01-01", end_date="2027-12-31",
            ),
            [],
        )

    def test_cache_off_skips_calendar_and_drops_unclosed_today(self):
        remote = {"AAPL": [_bar("2026-07-09", 100), _bar("2026-07-10", 101)]}
        with patch("fetching._fetch_bars_remote", return_value=(remote, "iex", None)) as bars, \
             patch("fetching._fetch_market_calendar") as calendar:
            result, _, note = F.fetch_bars(
                ["AAPL"], "2026-07-06", "2026-07-10", "iex", "split", 10000, 30,
                cache_mode="off", now_fn=lambda: _utc("2026-07-10T14:00:00Z"),
            )
        calendar.assert_not_called()
        bars.assert_called_once()
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("已剔除", note)

    def test_calendar_failure_falls_back_to_remote(self):
        remote = {"AAPL": [_bar("2026-07-09", 100), _bar("2026-07-10", 101)]}
        with patch("fetching._fetch_market_calendar", side_effect=RuntimeError("calendar down")), \
             patch("fetching._fetch_bars_remote", return_value=(remote, "iex", None)) as bars:
            result, _, note = F.fetch_bars(
                ["AAPL"], "2026-07-06", "2026-07-10", "iex", "split", 10000, 30,
                cache_db=self.db, now_fn=lambda: _utc("2026-07-10T14:00:00Z"),
            )
        bars.assert_called_once()
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("已回退远端", note)

    def test_database_initialization_failure_falls_back_to_remote(self):
        remote = {"AAPL": [_bar("2026-07-09", 100), _bar("2026-07-10", 101)]}
        with patch("fetching.MarketDataStore", side_effect=OSError("read-only filesystem")), \
             patch("fetching._fetch_bars_remote", return_value=(remote, "iex", None)) as bars, \
             patch("fetching._fetch_market_calendar") as calendar:
            result, used_feed, note = F.fetch_bars(
                ["AAPL"], "2026-07-06", "2026-07-10", "iex", "split", 10000, 30,
                cache_db=self.db, now_fn=lambda: _utc("2026-07-10T14:00:00Z"),
            )
        bars.assert_called_once()
        calendar.assert_not_called()
        self.assertEqual(used_feed, "iex")
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("read-only filesystem", note)

    def test_snapshot_database_initialization_failure_falls_back_to_remote(self):
        remote = _snapshot_context("2026-07-10")
        with patch("fetching.MarketDataStore", side_effect=OSError("read-only filesystem")), \
             patch("fetching._fetch_snapshots_remote", return_value=remote) as snapshots, \
             patch("fetching._fetch_market_calendar") as calendar:
            context = F.fetch_snapshots(
                ["AAPL"], feed="iex", timeout=30, cache_db=self.db,
                now_fn=lambda: _utc("2026-07-10T14:00:00Z"),
            )
        snapshots.assert_called_once()
        calendar.assert_not_called()
        self.assertEqual(context["status"], "ok")
        self.assertIn("read-only filesystem", context["cache_note"])

    def test_incomplete_v1_schema_falls_back_to_remote_without_using_bad_rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.execute("CREATE TABLE unrelated(value TEXT)")
        remote = {"AAPL": [_bar("2026-07-09", 100), _bar("2026-07-10", 101)]}
        with patch("fetching._fetch_bars_remote", return_value=(remote, "iex", None)) as bars, \
             patch("fetching._fetch_market_calendar") as calendar:
            result, _, note = F.fetch_bars(
                ["AAPL"], "2026-07-06", "2026-07-10", "iex", "split", 10000, 30,
                cache_db=self.db, now_fn=lambda: _utc("2026-07-10T14:00:00Z"),
            )
        bars.assert_called_once()
        calendar.assert_not_called()
        self.assertEqual(result["AAPL"][-1]["t"][:10], "2026-07-09")
        self.assertIn("已回退远端", note)


if __name__ == "__main__":
    unittest.main()
