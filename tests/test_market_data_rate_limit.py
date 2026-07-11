#!/usr/bin/env python3
"""Deterministic tests for page-level pacing, 429 retry, and singleton locks."""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetching  # noqa: E402
from market_data_rate_limit import (  # noqa: E402
    PageRateLimiter,
    RateLimitCancelled,
    RateLimitRetryPolicy,
    WarmupAlreadyRunningError,
    WarmupFileLock,
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


def _result(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        ["alpaca"], returncode, stdout=json.dumps(payload), stderr=stderr
    )


class TestPageRateLimiter(unittest.TestCase):
    def test_even_pacing_has_no_initial_burst(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        first = limiter.acquire()
        second = limiter.acquire()
        self.assertAlmostEqual(first, 0.5)
        self.assertAlmostEqual(second, 1.0)
        self.assertEqual(limiter.snapshot()["peak_60s"], 2)

    def test_thousand_pages_with_eight_workers_stay_inside_window(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)

        def take_slots(count):
            return [limiter.acquire() for _ in range(count)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            timestamps = [
                value
                for future in [pool.submit(take_slots, 125) for _ in range(8)]
                for value in future.result()
            ]
        self.assertEqual(len(timestamps), 1000)
        metrics = limiter.snapshot()
        self.assertEqual(metrics["pages"], 1000)
        self.assertLessEqual(metrics["peak_60s"], 120)

    def test_five_thousand_symbol_multi_page_simulation_with_eight_workers(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        symbols = [f"S{i:04d}" for i in range(5000)]
        batches = [symbols[offset:offset + 50] for offset in range(0, len(symbols), 50)]

        def fetch_three_pages(batch):
            self.assertLessEqual(len(batch), 50)
            return [limiter.acquire() for _page in range(3)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            timestamps = [
                value for values in pool.map(fetch_three_pages, batches) for value in values
            ]
        self.assertEqual(len(timestamps), 300)
        self.assertEqual(limiter.snapshot()["pages"], 300)
        self.assertLessEqual(limiter.snapshot()["peak_60s"], 120)

    def test_global_cooldown_delays_other_worker(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        first = limiter.acquire()
        deadline = limiter.defer(60)
        other_worker = limiter.acquire()
        self.assertGreaterEqual(other_worker, deadline)
        self.assertGreaterEqual(other_worker - first, 60)

    def test_cancellation_is_checked_while_waiting(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(1, clock=clock.monotonic, sleeper=clock.sleep)
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(RateLimitCancelled):
            limiter.acquire(stopped)


class TestFetchPageHook(unittest.TestCase):
    def test_every_page_acquires_a_slot(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        pages = [
            _result({
                "bars": {"AAPL": [{"t": "2026-07-08T04:00:00Z"}]},
                "next_page_token": "next",
            }),
            _result({
                "bars": {"AAPL": [{"t": "2026-07-09T04:00:00Z"}]},
                "next_page_token": None,
            }),
        ]
        with patch.object(fetching.subprocess, "run", side_effect=pages) as run:
            bars = fetching._fetch_feed(
                ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 1, 30,
                request_limiter=limiter,
            )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(bars["AAPL"]), 2)
        self.assertEqual(limiter.snapshot()["pages"], 2)

    def test_429_is_retried_with_new_slot_and_shared_cooldown(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        retry = RateLimitRetryPolicy(
            max_retries=2, base_delay=60, max_delay=60,
            jitter_ratio=0, random_fn=lambda: 0,
        )
        responses = [
            _result({}, returncode=1, stderr="HTTP 429 Too Many Requests; Retry-After: 60"),
            _result({"bars": {"AAPL": []}, "next_page_token": None}),
        ]
        with patch.object(fetching.subprocess, "run", side_effect=responses) as run:
            bars = fetching._fetch_feed(
                ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30,
                request_limiter=limiter, retry_policy=retry,
            )
        self.assertEqual(bars, {})
        self.assertEqual(run.call_count, 2)
        metrics = limiter.snapshot()
        self.assertEqual(metrics["pages"], 2)
        self.assertEqual(metrics["retries"], 1)
        self.assertEqual(metrics["rate_limits"], 1)
        self.assertGreaterEqual(clock.monotonic(), 60.5)

    def test_429_retry_is_bounded_and_preserves_structured_error(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        retry = RateLimitRetryPolicy(
            max_retries=1, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )
        response = _result({}, returncode=1, stderr="429 rate limit")
        with patch.object(fetching.subprocess, "run", return_value=response):
            with self.assertRaises(fetching.AlpacaRateLimitError):
                fetching._fetch_feed(
                    ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30,
                    request_limiter=limiter, retry_policy=retry,
                )
        metrics = limiter.snapshot()
        self.assertEqual(metrics["pages"], 2)
        self.assertEqual(metrics["retries"], 1)
        self.assertEqual(metrics["rate_limits"], 2)

    def test_unhooked_transport_keeps_existing_behavior(self):
        response = _result({"bars": {"AAPL": []}, "next_page_token": None})
        with patch.object(fetching.subprocess, "run", return_value=response) as run:
            self.assertEqual(
                fetching._fetch_feed(
                    ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30
                ),
                {},
            )
        run.assert_called_once()

    def test_timeout_counts_page_and_is_not_misclassified_as_429(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        retry = RateLimitRetryPolicy(
            max_retries=3, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )
        with patch.object(
            fetching.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["alpaca"], 30),
        ) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                fetching._fetch_feed(
                    ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30,
                    request_limiter=limiter, retry_policy=retry,
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(limiter.snapshot()["pages"], 1)
        self.assertEqual(limiter.snapshot()["retries"], 0)

    def test_non_json_page_counts_once_and_fails_without_retry(self):
        clock = VirtualClock()
        limiter = PageRateLimiter(120, clock=clock.monotonic, sleeper=clock.sleep)
        retry = RateLimitRetryPolicy(
            max_retries=3, base_delay=1, max_delay=1,
            jitter_ratio=0, random_fn=lambda: 0,
        )
        response = subprocess.CompletedProcess(
            ["alpaca"], 0, stdout="not-json", stderr=""
        )
        with patch.object(fetching.subprocess, "run", return_value=response) as run:
            with self.assertRaisesRegex(RuntimeError, "无法解析"):
                fetching._fetch_feed(
                    ["AAPL"], "2026-07-01", "2026-07-09", "iex", "split", 10000, 30,
                    request_limiter=limiter, retry_policy=retry,
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(limiter.snapshot()["pages"], 1)
        self.assertEqual(limiter.snapshot()["retries"], 0)


class TestWarmupFileLock(unittest.TestCase):
    def test_same_database_conflicts_and_release_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite3"
            first = WarmupFileLock(db).acquire()
            self.addCleanup(first.release)
            with self.assertRaises(WarmupAlreadyRunningError):
                WarmupFileLock(db).acquire()
            first.release()
            second = WarmupFileLock(db).acquire()
            second.release()

    def test_different_databases_can_lock_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = WarmupFileLock(Path(tmp) / "one.sqlite3").acquire()
            second = WarmupFileLock(Path(tmp) / "two.sqlite3").acquire()
            first.release()
            second.release()


if __name__ == "__main__":
    unittest.main()
