#!/usr/bin/env python3
"""Asset-catalog-driven audit and repair for completed daily-bar coverage."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from asset_catalog import ensure_daily_asset_catalog, fetch_alpaca_asset_catalog
from fetching import _fetch_bars_remote, _fetch_market_calendar
from market_data_rate_limit import PageRateLimiter, RateLimitRetryPolicy, WarmupFileLock
from market_data_store import MarketDataStore
from market_warmup import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_RPM,
    DEFAULT_MAX_RUNTIME,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    DEFAULT_YEARS,
    _result_from_plan,
    build_warmup_plan,
    execute_warmup_plan,
    latest_completed_session,
    load_symbols_file,
    validate_warmup_options,
)


def _attach_catalog_summary(
    result: dict[str, Any],
    *,
    catalog: dict[str, Any],
    store: MarketDataStore,
    universe: list[str],
    end_date: str,
) -> dict[str, Any]:
    latest = store.get_latest_daily_bars(
        universe, feed="iex", adjustment="split", on_or_before=end_date
    )
    result["universe_source"] = "asset_catalog"
    result["asset_catalog"] = {
        key: catalog.get(key)
        for key in (
            "catalog", "synced_at", "source", "source_count", "current_count",
            "tradable_count", "eligible_count", "unsupported_count",
            "new_count", "disappeared_count", "exclusions",
        )
        if key in catalog
    }
    result["bar_presence"] = {
        "symbols_with_final_bars": len(latest),
        "symbols_without_final_bars": len(universe) - len(latest),
    }
    return result


def run_market_reconcile(
    *,
    cache_db: str | Path | None = None,
    years: int = DEFAULT_YEARS,
    workers: int = DEFAULT_WORKERS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rpm: int = DEFAULT_MAX_RPM,
    max_runtime: int = DEFAULT_MAX_RUNTIME,
    feed: str = "iex",
    adjustment: str = "split",
    dry_run: bool = False,
    force_assets: bool = False,
    symbols_file: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    stop_event: threading.Event | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    asset_fetcher: Callable[..., list[dict[str, Any]]] = fetch_alpaca_asset_catalog,
    calendar_fetcher: Callable[..., Sequence[dict[str, Any]]] = _fetch_market_calendar,
    remote_transport: Callable[..., Any] = _fetch_bars_remote,
    store: MarketDataStore | None = None,
    limiter: PageRateLimiter | None = None,
    retry_policy: RateLimitRetryPolicy | None = None,
    lock_factory: Callable[[str | Path], Any] = WarmupFileLock,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Ensure today's catalog, audit three-year coverage, and repair only gaps."""
    validate_warmup_options(
        years=years, workers=workers, batch_size=batch_size, max_rpm=max_rpm,
        max_runtime=max_runtime, feed=feed, adjustment=adjustment, timeout=timeout,
    )
    started = float(clock())
    store = store or MarketDataStore(cache_db)
    limiter = limiter or PageRateLimiter(max_rpm)
    retry_policy = retry_policy or RateLimitRetryPolicy()
    with lock_factory(store.path):
        catalog = ensure_daily_asset_catalog(
            store, force=force_assets, timeout=timeout, fetcher=asset_fetcher
        )
        eligible_universe = store.get_asset_symbols(data_eligible=True)
        if not eligible_universe:
            raise RuntimeError("asset catalog has no IEX-eligible tradable symbols")
        if symbols_file is not None:
            requested = load_symbols_file(symbols_file)
            unsupported = sorted(set(requested) - set(eligible_universe))
            if unsupported:
                raise ValueError(
                    "--symbols-file includes symbols outside the current IEX-eligible catalog: "
                    + ",".join(unsupported[:10])
                )
            universe = requested
            scope_limited = True
        else:
            universe = eligible_universe
            scope_limited = False
        latest = latest_completed_session(
            store, calendar_fetcher=calendar_fetcher, timeout=timeout
        )
        plan = build_warmup_plan(
            store, universe, latest_session=latest, years=years,
            batch_size=batch_size, feed=feed, adjustment=adjustment,
            scope_limited=scope_limited,
        )
        if progress is not None:
            progress({
                "event": "planned",
                **plan.summary(),
                "catalog_source": catalog.get("source"),
                "catalog_current": catalog.get("current_count"),
                "catalog_eligible": catalog.get("eligible_count"),
                "catalog_unsupported": catalog.get("unsupported_count"),
            })
        if dry_run:
            result = _result_from_plan(
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
        else:
            result = execute_warmup_plan(
                store, plan, workers=workers, max_runtime=max_runtime,
                timeout=timeout, limiter=limiter, retry_policy=retry_policy,
                stop_event=stop_event, remote_transport=remote_transport,
                calendar_fetcher=calendar_fetcher, progress=progress,
                clock=clock, started_at=started,
            )
        return _attach_catalog_summary(
            result, catalog=catalog, store=store,
            universe=universe, end_date=plan.end,
        )
