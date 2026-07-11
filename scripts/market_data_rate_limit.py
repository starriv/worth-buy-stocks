#!/usr/bin/env python3
"""Shared Alpaca bars-page pacing, 429 retry, and warm-job locking.

The limiter is deliberately page-oriented: every actual ``multi-bars`` CLI
invocation must acquire a slot, including failed calls and retries.  It uses a
monotonic clock and emits evenly spaced slots with no startup burst.
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

try:  # POSIX is the supported runtime for this skill (macOS/Linux).
    import fcntl
except ImportError:  # pragma: no cover - defensive error for unsupported hosts
    fcntl = None  # type: ignore[assignment]


class RateLimitCancelled(RuntimeError):
    """The warm job was stopped while waiting for a page slot."""


class AlpacaRateLimitError(RuntimeError):
    """A structured Alpaca/CLI 429 response."""

    def __init__(self, detail: str, *, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class WarmupAlreadyRunningError(RuntimeError):
    """Another warm-market process already owns the database lock."""


class PageRateLimiter:
    """Thread-safe, evenly spaced request budget shared by all workers."""

    def __init__(
        self,
        max_rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_rpm, bool) or not 1 <= int(max_rpm) <= 180:
            raise ValueError("max_rpm must be an integer in 1..180")
        self.max_rpm = int(max_rpm)
        self.interval = 60.0 / self.max_rpm
        self._clock = clock
        self._sleeper = sleeper
        self._condition = threading.Condition()
        started = float(self._clock())
        self._started_at = started
        self._next_slot = started + self.interval
        self._cooldown_until = started
        self._request_times: deque[float] = deque()
        self._pages = 0
        self._retries = 0
        self._rate_limits = 0
        self._peak_60s = 0

    def acquire(self, stop_event: threading.Event | None = None) -> float:
        """Wait for and record one page slot; returns its monotonic timestamp."""
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RateLimitCancelled("warm-market stopped while waiting for rate-limit slot")
            with self._condition:
                now = float(self._clock())
                ready_at = max(self._next_slot, self._cooldown_until)
                if now >= ready_at:
                    granted_at = now
                    self._next_slot = granted_at + self.interval
                    self._pages += 1
                    self._request_times.append(granted_at)
                    cutoff = granted_at - 60.0
                    while self._request_times and self._request_times[0] <= cutoff:
                        self._request_times.popleft()
                    self._peak_60s = max(self._peak_60s, len(self._request_times))
                    return granted_at
                wait_for = min(ready_at - now, 0.25)
            # Never hold the condition while sleeping. A short slice keeps
            # max-runtime/SIGINT cancellation responsive during long cooldowns.
            self._sleeper(max(0.0, wait_for))

    def defer(self, seconds: float) -> float:
        """Extend the job-wide cooldown and return its monotonic deadline."""
        delay = max(0.0, float(seconds))
        with self._condition:
            deadline = float(self._clock()) + delay
            self._cooldown_until = max(self._cooldown_until, deadline)
            self._next_slot = max(self._next_slot, self._cooldown_until)
            self._condition.notify_all()
            return self._cooldown_until

    def record_rate_limit(self) -> None:
        with self._condition:
            self._rate_limits += 1

    def record_retry(self) -> None:
        with self._condition:
            self._retries += 1

    @property
    def cooldown_until(self) -> float:
        with self._condition:
            return self._cooldown_until

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            now = float(self._clock())
            cutoff = now - 60.0
            recent = sum(1 for value in self._request_times if value > cutoff)
            elapsed = max(0.0, now - self._started_at)
            average = (self._pages * 60.0 / elapsed) if elapsed else 0.0
            return {
                "pages": self._pages,
                "retries": self._retries,
                "rate_limits": self._rate_limits,
                "effective_rpm": round(float(recent), 3),
                "average_rpm": round(average, 3),
                "peak_60s": self._peak_60s,
                "max_rpm": self.max_rpm,
                "cooldown_remaining": round(max(0.0, self._cooldown_until - now), 3),
            }


class RateLimitRetryPolicy:
    """Bounded exponential retry policy used only for structured 429 errors."""

    def __init__(
        self,
        *,
        max_retries: int = 5,
        base_delay: float = 60.0,
        max_delay: float = 300.0,
        jitter_ratio: float = 0.10,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if int(max_retries) < 0:
            raise ValueError("max_retries must be >= 0")
        if float(base_delay) < 0 or float(max_delay) < float(base_delay):
            raise ValueError("retry delays must satisfy 0 <= base_delay <= max_delay")
        if not 0 <= float(jitter_ratio) <= 1:
            raise ValueError("jitter_ratio must be in 0..1")
        self.max_retries = int(max_retries)
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.jitter_ratio = float(jitter_ratio)
        self.random_fn = random_fn

    def run(
        self,
        operation: Callable[[], Any],
        *,
        limiter: PageRateLimiter,
        stop_event: threading.Event | None = None,
    ) -> Any:
        retries = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RateLimitCancelled("warm-market stopped before page request")
            try:
                return operation()
            except AlpacaRateLimitError as exc:
                limiter.record_rate_limit()
                if retries >= self.max_retries:
                    raise
                retries += 1
                limiter.record_retry()
                exponential = min(self.max_delay, self.base_delay * (2 ** (retries - 1)))
                jittered = min(
                    self.max_delay,
                    exponential * (1.0 + self.jitter_ratio * float(self.random_fn())),
                )
                delay = max(jittered, float(exc.retry_after or 0.0))
                limiter.defer(delay)


_RETRY_AFTER_RE = re.compile(
    r"retry[-_ ]?after(?:\s*[:=]|\s+)(\d+(?:\.\d+)?)", re.IGNORECASE
)


def parse_retry_after(detail: str) -> float | None:
    match = _RETRY_AFTER_RE.search(str(detail or ""))
    return float(match.group(1)) if match else None


class WarmupFileLock:
    """Non-blocking advisory singleton lock scoped to one SQLite path."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        resolved = Path(db_path).expanduser().resolve()
        self.db_path = resolved
        self.path = Path(f"{resolved}.warm-market.lock")
        self._file = None

    def acquire(self) -> "WarmupFileLock":
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("warm-market singleton lock requires a POSIX host")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise WarmupAlreadyRunningError(
                f"warm-market already running for database: {self.db_path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "database": str(self.db_path)}, handle)
        handle.write("\n")
        handle.flush()
        self._file = handle
        return self

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "WarmupFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
