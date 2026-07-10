#!/usr/bin/env python3
"""Offline tests for the SQLite market-data repository."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import market_data_store as M  # noqa: E402


FETCHED_AT = "2026-07-10T01:00:00Z"


def _bar(day: str, close: float = 100.0, *, volume: int = 1_000) -> dict:
    return {
        "t": f"{day}T04:00:00Z",
        "o": close - 1,
        "h": close + 2,
        "l": close - 2,
        "c": close,
        "v": volume,
        "vw": close - 0.25,
        "n": 42,
    }


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "nested" / "market.sqlite3"
        self.cache = M.MarketDataStore(
            self.db,
            now_fn=lambda: dt.datetime(2026, 7, 10, 1, 2, 3, tzinfo=dt.timezone.utc),
        )

    def tearDown(self):
        self.tmp.cleanup()


class TestPathAndMigration(CacheCase):
    def test_default_path_is_skill_data_directory_and_env_can_override(self):
        self.assertEqual(M.resolve_db_path(environ={}), M.SKILL_ROOT / "data" / "market-data.sqlite3")
        override = Path(self.tmp.name) / "override.sqlite3"
        self.assertEqual(
            M.resolve_db_path(environ={M.ENV_DB_PATH: str(override)}),
            override.resolve(),
        )
        explicit = Path(self.tmp.name) / "explicit.sqlite3"
        self.assertEqual(
            M.resolve_db_path(explicit, environ={M.ENV_DB_PATH: str(override)}),
            explicit.resolve(),
        )

    def test_initializes_schema_tables_permissions_and_wal_idempotently(self):
        self.assertEqual(self.cache.schema_version(), M.SCHEMA_VERSION)
        self.assertEqual(self.cache.schema_version(), M.SCHEMA_VERSION)
        self.assertEqual(
            set(self.cache.table_names()),
            {
                "daily_bars", "live_daily_bars", "latest_snapshots",
                "sync_ranges", "market_sessions", "feed_fallbacks",
            },
        )
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    def test_schema_contains_no_credential_columns(self):
        self.cache.schema_version()
        forbidden = ("key", "secret", "token", "account_id", "chat_id")
        with sqlite3.connect(self.db) as conn:
            columns = [
                row[1].lower()
                for table in self.cache.table_names()
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
        for column in columns:
            self.assertFalse(any(word in column for word in forbidden), column)

    def test_failed_migration_rolls_back_all_ddl(self):
        bad_schema = (
            "CREATE TABLE migration_probe (value TEXT)",
            "THIS IS NOT VALID SQL",
        )
        broken = M.MarketDataStore(self.db)
        with patch.object(M, "_SCHEMA_V2", bad_schema):
            with self.assertRaises(M.CacheSchemaError):
                broken.schema_version()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("migration_probe", names)

    def test_rejects_newer_schema(self):
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(M.CacheSchemaError):
            self.cache.schema_version()

    _LEGACY_V1_DDL = (
        """
        CREATE TABLE daily_bars (
            symbol TEXT NOT NULL, session_date TEXT NOT NULL, feed TEXT NOT NULL,
            adjustment TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL, volume REAL, vwap REAL,
            trade_count INTEGER, source_timestamp TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, session_date, feed, adjustment)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX idx_daily_bars_date ON daily_bars (session_date, symbol)",
        """
        CREATE TABLE live_daily_bars (
            symbol TEXT NOT NULL, session_date TEXT NOT NULL, feed TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL, vwap REAL, trade_count INTEGER,
            source_timestamp TEXT NOT NULL, captured_at TEXT NOT NULL,
            PRIMARY KEY (symbol, session_date, feed)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE latest_snapshots (
            symbol TEXT NOT NULL, feed TEXT NOT NULL, session_date TEXT NOT NULL,
            captured_at TEXT NOT NULL, normalized_payload_json TEXT NOT NULL,
            PRIMARY KEY (symbol, feed)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX idx_latest_snapshots_session
        ON latest_snapshots (session_date, symbol)
        """,
        """
        CREATE TABLE sync_ranges (
            symbol TEXT NOT NULL, feed TEXT NOT NULL, adjustment TEXT NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (symbol, feed, adjustment, start_date, end_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX idx_sync_ranges_lookup
        ON sync_ranges (symbol, feed, adjustment, start_date, end_date)
        """,
        """
        CREATE TABLE market_sessions (
            session_date TEXT NOT NULL PRIMARY KEY, open_at TEXT NOT NULL,
            close_at TEXT NOT NULL, fetched_at TEXT NOT NULL
        ) WITHOUT ROWID
        """,
    )

    def test_v1_database_migrates_to_v2_preserving_snapshots_and_coverage(self):
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            for statement in self._LEGACY_V1_DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO latest_snapshots VALUES (?, ?, ?, ?, ?)",
                ("AAPL", "iex", "2026-07-09", "2026-07-09T21:30:00Z",
                 '{"symbol":"AAPL","daily_bar":{"date":"2026-07-09","close":103}}'),
            )
            conn.execute(
                "INSERT INTO sync_ranges VALUES (?, ?, ?, ?, ?, ?)",
                ("AAPL", "iex", "split", "2026-07-01", "2026-07-09", FETCHED_AT),
            )
            conn.execute("PRAGMA user_version = 1")

        self.assertEqual(self.cache.schema_version(), 2)
        snapshot = self.cache.get_latest_snapshots(["AAPL"], feed="iex")
        self.assertEqual(snapshot["symbols"]["AAPL"]["daily_bar"]["close"], 103)
        self.assertEqual(snapshot["meta"]["AAPL"]["session_date"], "2026-07-09")
        self.assertEqual(
            self.cache.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-01", end_date="2026-07-09",
            ),
            [],
        )
        with sqlite3.connect(self.db) as conn:
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("idx_sync_ranges_lookup", indexes)
        self.assertIn("idx_latest_snapshots_session", indexes)
        self.assertIn("feed_fallbacks", names)
        self.assertNotIn("latest_snapshots_v1", names)


class TestDailyAndLiveBars(CacheCase):
    def test_daily_round_trip_is_sorted_and_upsert_is_idempotent(self):
        bars = {"aapl": [_bar("2026-07-09", 101), _bar("2026-07-08", 100)]}
        self.assertEqual(
            self.cache.upsert_daily_bars(
                bars, feed="iex", adjustment="split", fetched_at=FETCHED_AT
            ),
            2,
        )
        updated = _bar("2026-07-09", 105)
        self.cache.upsert_daily_bars(
            {"AAPL": [updated]}, feed="iex", adjustment="split", fetched_at=FETCHED_AT
        )
        out = self.cache.get_daily_bars(
            ["aapl"], start_date="2026-07-01", end_date="2026-07-10",
            feed="iex", adjustment="split",
        )
        self.assertEqual([b["t"][:10] for b in out["AAPL"]], ["2026-07-08", "2026-07-09"])
        self.assertEqual(out["AAPL"][-1], updated)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_bars").fetchone()[0], 2)

    def test_feed_and_adjustment_are_distinct_namespaces(self):
        self.cache.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-09", 100)]}, feed="iex", adjustment="split"
        )
        self.cache.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-09", 200)]}, feed="sip", adjustment="split"
        )
        iex = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-09", end_date="2026-07-09",
            feed="iex", adjustment="split",
        )
        sip = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-09", end_date="2026-07-09",
            feed="sip", adjustment="split",
        )
        self.assertEqual(iex["AAPL"][0]["c"], 100)
        self.assertEqual(sip["AAPL"][0]["c"], 200)

    def test_final_bar_never_reads_live_and_finalization_removes_live(self):
        self.cache.upsert_live_daily_bars(
            {"AAPL": _bar("2026-07-10", 110)}, feed="iex", captured_at=FETCHED_AT
        )
        self.assertEqual(
            self.cache.get_daily_bars(
                ["AAPL"], start_date="2026-07-10", end_date="2026-07-10",
                feed="iex", adjustment="split",
            ),
            {},
        )
        self.assertEqual(self.cache.get_live_daily_bars(["AAPL"], feed="iex")["AAPL"]["c"], 110)
        self.cache.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-10", 111)]}, feed="iex", adjustment="split"
        )
        self.assertEqual(self.cache.get_live_daily_bars(["AAPL"], feed="iex"), {})
        final = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-10", end_date="2026-07-10",
            feed="iex", adjustment="split",
        )
        self.assertEqual(final["AAPL"][0]["c"], 111)

    def test_large_symbol_batch_is_chunked(self):
        symbols = [f"S{i:03d}" for i in range(405)]
        self.cache.upsert_daily_bars(
            {symbol: [_bar("2026-07-09", i + 1)] for i, symbol in enumerate(symbols)},
            feed="iex", adjustment="split",
        )
        out = self.cache.get_daily_bars(
            symbols, start_date="2026-07-09", end_date="2026-07-09",
            feed="iex", adjustment="split",
        )
        self.assertEqual(len(out), 405)
        self.assertEqual(out["S404"][0]["c"], 405)

    def test_authoritative_refresh_can_delete_only_requested_namespace(self):
        bars = {"AAPL": [_bar("2026-07-08", 100), _bar("2026-07-09", 101)]}
        self.cache.upsert_daily_bars(bars, feed="iex", adjustment="split")
        self.cache.upsert_daily_bars(bars, feed="sip", adjustment="split")
        self.assertEqual(
            self.cache.delete_daily_bars(
                ["AAPL"], start_date="2026-07-09", end_date="2026-07-09",
                feed="iex", adjustment="split",
            ),
            1,
        )
        iex = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-01", end_date="2026-07-10",
            feed="iex", adjustment="split",
        )
        sip = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-01", end_date="2026-07-10",
            feed="sip", adjustment="split",
        )
        self.assertEqual([b["t"][:10] for b in iex["AAPL"]], ["2026-07-08"])
        self.assertEqual(len(sip["AAPL"]), 2)

    def test_latest_daily_bars_are_final_only_and_respect_namespace_and_cutoff(self):
        self.cache.upsert_daily_bars(
            {"AAPL": [_bar("2026-07-08", 100), _bar("2026-07-09", 101)]},
            feed="iex", adjustment="split",
        )
        self.cache.upsert_live_daily_bars(
            {"AAPL": _bar("2026-07-10", 999)}, feed="iex"
        )
        latest = self.cache.get_latest_daily_bars(
            ["AAPL"], feed="iex", adjustment="split"
        )
        cutoff = self.cache.get_latest_daily_bars(
            ["AAPL"], feed="iex", adjustment="split", on_or_before="2026-07-08"
        )
        self.assertEqual(latest["AAPL"]["c"], 101)
        self.assertEqual(cutoff["AAPL"]["c"], 100)
        self.assertEqual(
            self.cache.get_latest_daily_bars(
                ["AAPL"], feed="sip", adjustment="split"
            ),
            {},
        )


class TestSnapshots(CacheCase):
    def _context(self, close=101.0, as_of="2026-07-10T15:00:00Z"):
        return {
            "status": "ok",
            "as_of": as_of,
            "feed": "iex",
            "symbols": {
                "AAPL": {
                    "symbol": "AAPL",
                    "daily_bar": {
                        "date": "2026-07-10", "open": 99, "high": 102,
                        "low": 98, "close": close, "volume": 5000, "vwap": 100.5,
                    },
                    "latest_trade": {"price": close, "trade_time": as_of},
                }
            },
        }

    def test_snapshot_and_live_bar_are_atomic_latest_upserts(self):
        self.assertEqual(self.cache.upsert_snapshot_context(self._context()), (1, 1))
        newer = self._context(105.0, "2026-07-10T15:01:00Z")
        self.assertEqual(self.cache.upsert_snapshot_context(newer), (1, 1))
        context = self.cache.get_latest_snapshots(["AAPL"], feed="iex")
        self.assertEqual(context["as_of"], "2026-07-10T15:01:00Z")
        self.assertEqual(context["symbols"]["AAPL"]["daily_bar"]["close"], 105)
        live = self.cache.get_live_daily_bars(["AAPL"], feed="iex", session_date="2026-07-10")
        self.assertEqual(live["AAPL"]["c"], 105)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM latest_snapshots").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM live_daily_bars").fetchone()[0], 1)

    def test_unavailable_or_missing_snapshot_is_nonfatal(self):
        self.assertEqual(self.cache.upsert_snapshot_context({"status": "unavailable"}), (0, 0))
        self.assertEqual(self.cache.get_latest_snapshots([], feed="iex")["status"], "unavailable")
        self.assertEqual(self.cache.get_latest_snapshots(["AAPL"], feed="iex")["status"], "unavailable")

    def test_snapshot_rejects_accidental_credential_fields(self):
        self.cache.schema_version()
        context = self._context()
        context["symbols"]["AAPL"]["api_key"] = "must-not-persist"
        with self.assertRaisesRegex(ValueError, "forbidden credential"):
            self.cache.upsert_snapshot_context(context)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM latest_snapshots").fetchone()[0], 0)

    def test_completed_snapshot_can_persist_without_creating_a_live_bar(self):
        self.assertEqual(
            self.cache.upsert_snapshot_context(self._context(), include_live=False),
            (1, 0),
        )
        self.assertEqual(
            self.cache.get_latest_snapshots(["AAPL"], feed="iex")["status"], "ok"
        )
        self.assertEqual(self.cache.get_live_daily_bars(["AAPL"], feed="iex"), {})

    def test_live_session_filter_rejects_a_stale_daily_bar(self):
        self.assertEqual(
            self.cache.upsert_snapshot_context(
                self._context(), live_session_date="2026-07-11"
            ),
            (1, 0),
        )
        self.assertEqual(self.cache.get_live_daily_bars(["AAPL"], feed="iex"), {})


class TestCoverageAndSessions(CacheCase):
    def test_ranges_merge_and_missing_ranges_preserve_real_gaps(self):
        self.cache.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-01", end_date="2026-07-05", synced_at=FETCHED_AT,
        )
        self.cache.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-06", end_date="2026-07-10", synced_at=FETCHED_AT,
        )
        self.cache.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-20", end_date="2026-07-25", synced_at=FETCHED_AT,
        )
        ranges = self.cache.get_sync_ranges("AAPL", feed="iex", adjustment="split")
        self.assertEqual(
            [(r["start_date"], r["end_date"]) for r in ranges],
            [("2026-07-01", "2026-07-10"), ("2026-07-20", "2026-07-25")],
        )
        self.assertEqual(
            self.cache.missing_ranges(
                "AAPL", feed="iex", adjustment="split",
                start_date="2026-07-01", end_date="2026-07-30",
            ),
            [("2026-07-11", "2026-07-19"), ("2026-07-26", "2026-07-30")],
        )

    def test_empty_remote_result_can_still_mark_range_covered(self):
        self.assertEqual(self.cache.commit_daily_sync(
            {}, ["NEWIPO"], feed="iex", adjustment="split",
            start_date="2026-01-01", end_date="2026-06-30",
        ), 0)
        self.assertEqual(
            self.cache.missing_ranges(
                "NEWIPO", feed="iex", adjustment="split",
                start_date="2026-01-01", end_date="2026-06-30",
            ),
            [],
        )
        self.assertEqual(
            self.cache.get_daily_bars(
                ["NEWIPO"], start_date="2026-01-01", end_date="2026-06-30",
                feed="iex", adjustment="split",
            ),
            {},
        )

    def test_commit_daily_sync_can_authoritatively_replace_range(self):
        self.cache.commit_daily_sync(
            {"AAPL": [_bar("2026-07-08", 100), _bar("2026-07-09", 101)]},
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-08", end_date="2026-07-09",
        )
        self.cache.commit_daily_sync(
            {"AAPL": [_bar("2026-07-09", 202)]},
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-08", end_date="2026-07-09", replace=True,
        )
        out = self.cache.get_daily_bars(
            ["AAPL"], start_date="2026-07-08", end_date="2026-07-09",
            feed="iex", adjustment="split",
        )
        self.assertEqual([bar["t"][:10] for bar in out["AAPL"]], ["2026-07-09"])
        self.assertEqual(out["AAPL"][0]["c"], 202)

    def test_missing_ranges_bulk_matches_per_symbol_results(self):
        self.cache.add_sync_range(
            ["AAPL"], feed="iex", adjustment="split",
            start_date="2026-07-01", end_date="2026-07-05", synced_at=FETCHED_AT,
        )
        bulk = self.cache.missing_ranges_bulk(
            ["AAPL", "MSFT"], feed="iex", adjustment="split",
            start_date="2026-07-01", end_date="2026-07-10",
        )
        self.assertEqual(bulk["AAPL"], [("2026-07-06", "2026-07-10")])
        self.assertEqual(bulk["MSFT"], [("2026-07-01", "2026-07-10")])
        for symbol in ("AAPL", "MSFT"):
            self.assertEqual(
                bulk[symbol],
                self.cache.missing_ranges(
                    symbol, feed="iex", adjustment="split",
                    start_date="2026-07-01", end_date="2026-07-10",
                ),
            )

    def test_feed_fallback_round_trip(self):
        self.assertIsNone(self.cache.get_feed_fallback("sip"))
        self.cache.set_feed_fallback("SIP", "iex")
        self.assertEqual(self.cache.get_feed_fallback("sip"), "iex")
        self.cache.set_feed_fallback("sip", "boats")
        self.assertEqual(self.cache.get_feed_fallback("sip"), "boats")
        self.cache.clear_feed_fallback("sip")
        self.assertIsNone(self.cache.get_feed_fallback("sip"))

    def test_market_sessions_upsert_query_and_clock_injection(self):
        self.assertEqual(
            self.cache.upsert_market_sessions([
                {"date": "2026-07-09", "open": "09:30", "close": "16:00"},
                {"date": "2026-07-10", "open": "09:30", "close": "13:00"},
            ]),
            2,
        )
        self.cache.upsert_market_sessions([
            {"session_date": "2026-07-10", "open_at": "09:30", "close_at": "16:00"}
        ])
        self.assertEqual(self.cache.get_market_session("2026-07-10")["close_at"], "16:00")
        self.assertEqual(
            [row["session_date"] for row in self.cache.get_market_sessions("2026-07-01", "2026-07-31")],
            ["2026-07-09", "2026-07-10"],
        )
        self.assertEqual(self.cache.now_utc(), dt.datetime(2026, 7, 10, 1, 2, 3, tzinfo=dt.timezone.utc))


class TestConcurrency(CacheCase):
    def test_parallel_writers_use_independent_connections_and_remain_idempotent(self):
        self.cache.schema_version()

        def write(i):
            symbol = f"T{i:02d}"
            for _ in range(3):
                self.cache.upsert_daily_bars(
                    {symbol: [_bar("2026-07-09", i + 1)]},
                    feed="iex", adjustment="split",
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(32)))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_bars").fetchone()[0], 32)
        self.assertEqual(self.cache.integrity_check(), "ok")

    def test_lock_timeout_becomes_typed_cache_busy_error(self):
        self.cache.schema_version()
        locker = sqlite3.connect(self.db, isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")
            impatient = M.MarketDataStore(self.db, busy_timeout_ms=20)
            with self.assertRaises(M.CacheBusyError):
                impatient.upsert_daily_bars(
                    {"AAPL": [_bar("2026-07-09")]},
                    feed="iex", adjustment="split",
                )
        finally:
            locker.rollback()
            locker.close()


class TestRangePureFunctions(unittest.TestCase):
    def test_merge_and_missing_validation(self):
        self.assertEqual(
            M.merge_date_ranges([
                ("2026-07-03", "2026-07-05"),
                ("2026-07-01", "2026-07-02"),
                ("2026-07-10", "2026-07-12"),
            ]),
            [("2026-07-01", "2026-07-05"), ("2026-07-10", "2026-07-12")],
        )
        self.assertEqual(
            M.missing_date_ranges([], "2026-07-01", "2026-07-02"),
            [("2026-07-01", "2026-07-02")],
        )
        with self.assertRaises(ValueError):
            M.merge_date_ranges([("2026-07-03", "2026-07-01")])


if __name__ == "__main__":
    unittest.main()
