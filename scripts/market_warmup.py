#!/usr/bin/env python3
"""Bounded, resumable whole-market daily-bar warmup orchestration."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from fetching import _fetch_bars_remote, _fetch_market_calendar
from market_data_cache import MarketDataCache
from market_data_rate_limit import (
    PageRateLimiter,
    RateLimitCancelled,
    RateLimitRetryPolicy,
    WarmupFileLock,
)
from market_data_store import MarketDataStore
from scanner import fetch_active_assets, filter_common_tickers


DEFAULT_YEARS = 3
DEFAULT_WORKERS = 4
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_RPM = 120
DEFAULT_MAX_RUNTIME = 3600
DEFAULT_LIMIT = 10_000
DEFAULT_TIMEOUT = 30
MAX_FAILURE_DETAILS = 100


@dataclass(frozen=True)
class WarmupBatch:
    start: str
    end: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class WarmupPlan:
    universe: tuple[str, ...]
    start: str
    end: str
    feed: str
    adjustment: str
    covered: int
    pending_symbols: int
    pending_ranges: int
    estimated_rows: int
    batches: tuple[WarmupBatch, ...]
    scope_limited: bool

    def summary(self) -> dict[str, Any]:
        return {
            "universe": len(self.universe),
            "covered": self.covered,
            "pending": self.pending_symbols,
            "pending_ranges": self.pending_ranges,
            "estimated_rows": self.estimated_rows,
            "batches": len(self.batches),
            "range": {"start": self.start, "end": self.end},
            "feed": self.feed,
            "adjustment": self.adjustment,
            "scope_limited": self.scope_limited,
        }


def validate_warmup_options(
    *, years: int, workers: int, batch_size: int, max_rpm: int,
    max_runtime: int, feed: str, adjustment: str, timeout: int,
) -> None:
    bounds = (
        ("years", years, 1, 10),
        ("workers", workers, 1, 8),
        ("batch_size", batch_size, 1, 200),
        ("max_rpm", max_rpm, 1, 180),
        ("max_runtime", max_runtime, 1, 86_400),
        ("timeout", timeout, 1, 300),
    )
    for label, value, minimum, maximum in bounds:
        if isinstance(value, bool) or not minimum <= int(value) <= maximum:
            raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")
    if str(feed or "").lower() != "iex":
        raise ValueError("warm-market Basic mode only supports feed=iex")
    if str(adjustment or "").lower() != "split":
        raise ValueError("warm-market currently only supports adjustment=split")


def _clean_file_symbols(text: str) -> list[str]:
    raw_values = re.split(r"[\s,]+", str(text or ""))
    assets = [
        {"symbol": value.strip().upper(), "fractionable": True}
        for value in raw_values if value.strip()
    ]
    return sorted(filter_common_tickers(assets))


def load_symbols_file(path: str | Path) -> list[str]:
    source = Path(path).expanduser()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read --symbols-file {source}: {exc}") from exc
    symbols = _clean_file_symbols(text)
    if not symbols:
        raise ValueError("--symbols-file contains no valid scanner tickers")
    return symbols


def discover_universe(
    *,
    symbols_file: str | Path | None = None,
    asset_fetcher: Callable[..., list[dict[str, Any]]] = fetch_active_assets,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], bool]:
    """Return deterministic scanner tickers and whether scope is file-limited."""
    if symbols_file is not None:
        return load_symbols_file(symbols_file), True
    assets = asset_fetcher(
        exchanges=("NYSE", "NASDAQ"), asset_class="us_equity", timeout=timeout
    )
    symbols = sorted(set(filter_common_tickers(assets)))
    if not symbols:
        raise RuntimeError("NYSE/NASDAQ asset discovery returned no valid scanner tickers")
    return symbols, False


def start_date_for_years(end_date: str, years: int) -> str:
    if isinstance(years, bool) or not 1 <= int(years) <= 10:
        raise ValueError("years must be an integer in 1..10")
    end = dt.date.fromisoformat(str(end_date))
    try:
        start = end.replace(year=end.year - int(years))
    except ValueError:  # Feb 29 -> Feb 28 in a non-leap target year.
        start = end.replace(year=end.year - int(years), day=28)
    return start.isoformat()


def _estimated_weekdays(start: str, end: str) -> int:
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    days = (last - first).days + 1
    weeks, remainder = divmod(days, 7)
    return weeks * 5 + sum(
        1 for offset in range(remainder)
        if (first.weekday() + offset) % 7 < 5
    )


def latest_completed_session(
    store: MarketDataStore,
    *,
    calendar_fetcher: Callable[..., Sequence[dict[str, Any]]] = _fetch_market_calendar,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    cache = MarketDataCache(
        store,
        remote_bars_fetcher=lambda *_args: ({}, "iex", None),
        calendar_fetcher=calendar_fetcher,
    )
    state, _sessions = cache.session_state(timeout)
    if not state.latest_completed_session:
        raise RuntimeError("market calendar has no completed session before now")
    return state.latest_completed_session


def build_warmup_plan(
    store: MarketDataStore,
    universe: Iterable[str],
    *,
    latest_session: str,
    years: int = DEFAULT_YEARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    feed: str = "iex",
    adjustment: str = "split",
    scope_limited: bool = False,
) -> WarmupPlan:
    if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 200:
        raise ValueError("batch_size must be an integer in 1..200")
    clean = tuple(sorted({
        str(symbol or "").strip().upper()
        for symbol in universe
        if str(symbol or "").strip()
    }))
    if not clean:
        raise ValueError("warmup universe must not be empty")
    end = dt.date.fromisoformat(str(latest_session)).isoformat()
    start = start_date_for_years(end, years)
    feed = str(feed or "").strip().lower()
    adjustment = str(adjustment or "").strip().lower()
    gap_map = store.missing_ranges_bulk(
        clean, feed=feed, adjustment=adjustment,
        start_date=start, end_date=end,
    )
    grouped: dict[tuple[str, str], list[str]] = {}
    pending: set[str] = set()
    pending_ranges = 0
    estimated_rows = 0
    for symbol in clean:
        gaps = gap_map.get(symbol, [])
        if gaps:
            pending.add(symbol)
        for gap in gaps:
            grouped.setdefault(gap, []).append(symbol)
            pending_ranges += 1
            estimated_rows += _estimated_weekdays(*gap)
    batches: list[WarmupBatch] = []
    size = int(batch_size)
    for (gap_start, gap_end), symbols in sorted(grouped.items()):
        for offset in range(0, len(symbols), size):
            batches.append(WarmupBatch(gap_start, gap_end, tuple(symbols[offset:offset + size])))
    return WarmupPlan(
        universe=clean,
        start=start,
        end=end,
        feed=feed,
        adjustment=adjustment,
        covered=len(clean) - len(pending),
        pending_symbols=len(pending),
        pending_ranges=pending_ranges,
        estimated_rows=estimated_rows,
        batches=tuple(batches),
        scope_limited=bool(scope_limited),
    )


def _safe_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


def _result_from_plan(
    plan: WarmupPlan,
    *,
    limiter: PageRateLimiter,
    started_at: float,
    clock: Callable[[], float],
    completed: int,
    failed_symbols: set[str],
    failure_details: list[dict[str, Any]],
    pending: int,
    stop_reason: str,
) -> dict[str, Any]:
    metrics = limiter.snapshot()
    elapsed = max(0.0, float(clock()) - started_at)
    minimum_remaining_pages = max(0, pending)
    eta = minimum_remaining_pages * 60.0 / limiter.max_rpm
    return {
        **plan.summary(),
        "initial_covered": plan.covered,
        "initial_pending": plan.pending_symbols,
        "covered": len(plan.universe) - pending,
        "pending": pending,
        "completed": completed,
        "failed": len(failed_symbols),
        "pages": metrics["pages"],
        "effective_rpm": metrics["effective_rpm"],
        "average_rpm": metrics["average_rpm"],
        "peak_60s": metrics["peak_60s"],
        "retries": metrics["retries"],
        "rate_limits": metrics["rate_limits"],
        "elapsed": round(elapsed, 3),
        "eta": round(eta, 3),
        "stop_reason": stop_reason,
        "failures": failure_details[:MAX_FAILURE_DETAILS],
        "failure_details_truncated": max(0, len(failure_details) - MAX_FAILURE_DETAILS),
    }


def execute_warmup_plan(
    store: MarketDataStore,
    plan: WarmupPlan,
    *,
    workers: int = DEFAULT_WORKERS,
    max_runtime: int = DEFAULT_MAX_RUNTIME,
    timeout: int = DEFAULT_TIMEOUT,
    limit: int = DEFAULT_LIMIT,
    limiter: PageRateLimiter,
    retry_policy: RateLimitRetryPolicy,
    stop_event: threading.Event | None = None,
    remote_transport: Callable[..., Any] = _fetch_bars_remote,
    calendar_fetcher: Callable[..., Sequence[dict[str, Any]]] = _fetch_market_calendar,
    progress: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Execute only planned gaps; aggregate counts rather than market-wide bars."""
    if isinstance(workers, bool) or not 1 <= int(workers) <= 8:
        raise ValueError("workers must be an integer in 1..8")
    if isinstance(max_runtime, bool) or not 1 <= int(max_runtime) <= 86_400:
        raise ValueError("max_runtime must be an integer in 1..86400")
    started = float(clock()) if started_at is None else float(started_at)
    deadline = started + int(max_runtime)
    stopped = stop_event or threading.Event()

    def limited_remote(symbols, start, end, feed, adjustment, request_limit, request_timeout):
        return remote_transport(
            symbols, start, end, feed, adjustment, request_limit, request_timeout,
            request_limiter=limiter, retry_policy=retry_policy, stop_event=stopped,
        )

    cache = MarketDataCache(
        store,
        remote_bars_fetcher=limited_remote,
        calendar_fetcher=calendar_fetcher,
    )

    def process(batch: WarmupBatch) -> dict[str, Any]:
        cache.load_bars(
            batch.symbols, batch.start, batch.end,
            plan.feed, plan.adjustment, limit, timeout, mode="auto",
        )
        remaining = store.missing_ranges_bulk(
            batch.symbols, feed=plan.feed, adjustment=plan.adjustment,
            start_date=batch.start, end_date=batch.end,
        )
        unresolved = [symbol for symbol in batch.symbols if remaining.get(symbol)]
        return {"batch": batch, "unresolved": unresolved}

    queue = deque(plan.batches)
    inflight: dict[concurrent.futures.Future, WarmupBatch] = {}
    failed_symbols: set[str] = set()
    failure_details: list[dict[str, Any]] = []
    stop_reason: str | None = None
    completed_batches = 0
    failed_batches = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(workers)) as pool:
        while queue or inflight:
            if stopped.is_set() and stop_reason is None:
                stop_reason = "cancelled"
            if float(clock()) >= deadline and stop_reason is None:
                stop_reason = "max_runtime"
                stopped.set()

            while stop_reason is None and queue and len(inflight) < int(workers):
                batch = queue.popleft()
                inflight[pool.submit(process, batch)] = batch

            if not inflight:
                break
            done, _pending = concurrent.futures.wait(
                inflight, timeout=0.1,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                continue
            for future in done:
                batch = inflight.pop(future)
                try:
                    outcome = future.result()
                    unresolved = list(outcome.get("unresolved") or [])
                    if unresolved:
                        failed_batches += 1
                        failed_symbols.update(unresolved)
                        failure_details.append({
                            "symbols": unresolved[:10],
                            "range": {"start": batch.start, "end": batch.end},
                            "error": "coverage remained missing after remote response",
                        })
                    else:
                        completed_batches += 1
                except RateLimitCancelled as exc:
                    if stop_reason is None:
                        stop_reason = "cancelled"
                    failure_details.append({
                        "symbols": list(batch.symbols[:10]),
                        "range": {"start": batch.start, "end": batch.end},
                        "error": _safe_error(exc),
                    })
                except Exception as exc:  # noqa: BLE001 - isolate one batch
                    failed_batches += 1
                    failed_symbols.update(batch.symbols)
                    failure_details.append({
                        "symbols": list(batch.symbols[:10]),
                        "range": {"start": batch.start, "end": batch.end},
                        "error": _safe_error(exc),
                    })
                if progress is not None:
                    progress({
                        "event": "batch_complete",
                        "completed_batches": completed_batches,
                        "failed_batches": failed_batches,
                        "failed_symbols": len(failed_symbols),
                        "remaining_batches": len(queue) + len(inflight),
                        "pages": limiter.snapshot()["pages"],
                    })

    final_gaps = store.missing_ranges_bulk(
        plan.universe, feed=plan.feed, adjustment=plan.adjustment,
        start_date=plan.start, end_date=plan.end,
    )
    final_pending = sum(1 for symbol in plan.universe if final_gaps.get(symbol))
    final_covered = len(plan.universe) - final_pending
    completed = max(0, final_covered - plan.covered)
    failed_symbols.intersection_update({
        symbol for symbol in plan.universe if final_gaps.get(symbol)
    })
    if stop_reason is None:
        stop_reason = "batch_failures" if final_pending else "complete"
    return _result_from_plan(
        plan,
        limiter=limiter,
        started_at=started,
        clock=clock,
        completed=completed,
        failed_symbols=failed_symbols,
        failure_details=failure_details,
        pending=final_pending,
        stop_reason=stop_reason,
    )


def run_market_warmup(
    *,
    cache_db: str | Path | None = None,
    years: int = DEFAULT_YEARS,
    workers: int = DEFAULT_WORKERS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rpm: int = DEFAULT_MAX_RPM,
    max_runtime: int = DEFAULT_MAX_RUNTIME,
    feed: str = "iex",
    adjustment: str = "split",
    symbols_file: str | Path | None = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    stop_event: threading.Event | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    asset_fetcher: Callable[..., list[dict[str, Any]]] = fetch_active_assets,
    calendar_fetcher: Callable[..., Sequence[dict[str, Any]]] = _fetch_market_calendar,
    remote_transport: Callable[..., Any] = _fetch_bars_remote,
    store: MarketDataStore | None = None,
    limiter: PageRateLimiter | None = None,
    retry_policy: RateLimitRetryPolicy | None = None,
    lock_factory: Callable[[str | Path], Any] = WarmupFileLock,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Discover, plan, and execute one safely locked warm-market job."""
    validate_warmup_options(
        years=years, workers=workers, batch_size=batch_size, max_rpm=max_rpm,
        max_runtime=max_runtime, feed=feed, adjustment=adjustment, timeout=timeout,
    )
    started = float(clock())
    store = store or MarketDataStore(cache_db)
    limiter = limiter or PageRateLimiter(max_rpm)
    retry_policy = retry_policy or RateLimitRetryPolicy()
    with lock_factory(store.path):
        universe, scope_limited = discover_universe(
            symbols_file=symbols_file, asset_fetcher=asset_fetcher, timeout=timeout
        )
        latest = latest_completed_session(
            store, calendar_fetcher=calendar_fetcher, timeout=timeout
        )
        plan = build_warmup_plan(
            store, universe, latest_session=latest, years=years,
            batch_size=batch_size, feed=feed, adjustment=adjustment,
            scope_limited=scope_limited,
        )
        if progress is not None:
            progress({"event": "planned", **plan.summary()})
        if dry_run:
            return _result_from_plan(
                plan,
                limiter=limiter,
                started_at=started,
                clock=clock,
                completed=0,
                failed_symbols=set(),
                failure_details=[],
                pending=plan.pending_symbols,
                stop_reason="dry_run",
            )
        return execute_warmup_plan(
            store, plan, workers=workers, max_runtime=max_runtime,
            timeout=timeout, limiter=limiter, retry_policy=retry_policy,
            stop_event=stop_event, remote_transport=remote_transport,
            calendar_fetcher=calendar_fetcher, progress=progress,
            clock=clock, started_at=started,
        )
