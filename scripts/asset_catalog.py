#!/usr/bin/env python3
"""Daily Alpaca US-equity asset catalog synchronization."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fetching import _run_json
from market_data_store import ASSET_CATALOG, MarketDataStore


def fetch_alpaca_asset_catalog(timeout: int = 30) -> list[dict[str, Any]]:
    """Fetch the complete active US-equity catalog in one Alpaca CLI call."""
    raw = _run_json(
        [
            "alpaca", "asset", "list",
            "--asset-class", "us_equity",
            "--status", "active",
            "--quiet",
        ],
        timeout,
    )
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, Mapping):
        items = raw.get("assets") or raw.get("data") or []
    else:
        items = []
    assets = [dict(item) for item in items if isinstance(item, Mapping)]
    if not assets:
        raise RuntimeError("Alpaca active US-equity catalog returned no assets")
    return assets


def ensure_daily_asset_catalog(
    store: MarketDataStore,
    *,
    force: bool = False,
    timeout: int = 30,
    fetcher: Callable[..., list[dict[str, Any]]] = fetch_alpaca_asset_catalog,
) -> dict[str, Any]:
    """Refresh once per UTC day, or return the last atomic catalog state."""
    if not force and store.asset_catalog_is_fresh_today(ASSET_CATALOG):
        state = store.get_asset_catalog_state(ASSET_CATALOG) or {}
        return {
            **state,
            "source": "cache",
            "fresh": True,
            "exclusions": store.get_asset_exclusion_counts(),
        }
    assets = fetcher(timeout=timeout)
    state = store.replace_asset_catalog(assets, catalog=ASSET_CATALOG)
    return {
        **state,
        "source": "remote",
        "fresh": True,
        "exclusions": store.get_asset_exclusion_counts(),
    }
