"""Read-only Alpaca hydration for ledger-backed lifecycle reviews."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from fetching import fetch_snapshots
from portfolio import fetch_account_context

from .data import fetch_normalized_chain
from .ledger import position_legs
from .live import collect_live_events

_NEW_YORK = ZoneInfo("America/New_York")


def reconcile_position(position: dict[str, Any], account_context: dict[str, Any]) -> dict[str, Any]:
    return reconcile_positions([position], account_context)[0]


def reconcile_positions(
    ledger_positions: list[dict[str, Any]], account_context: dict[str, Any]
) -> list[dict[str, Any]]:
    """Confirm ledger rows without allocating the same account quantity twice."""
    if not isinstance(account_context, dict) or account_context.get("status") != "ok":
        return [
            {"status": "unavailable", "reason": "account_position_confirmation_unavailable"}
            for _ in ledger_positions
        ]
    positions = account_context.get("positions") or {}
    capacity = {
        symbol: abs(float(row.get("qty") or 0.0))
        for symbol, row in positions.items()
    }
    confirmations = []
    for position in ledger_positions:
        required_qty = float(position.get("quantity") or 0.0)
        legs = position_legs(position)
        failures = []
        requirements: list[tuple[str, str, float, str]] = []
        for leg in legs:
            symbol = str(leg.get("symbol") or "").upper()
            expected_side = (
                "short" if str(leg.get("side") or "").lower() in {"short", "sell", "sell_to_open"}
                else "long"
            )
            ratio = float(leg.get("ratio") or 1.0)
            leg_qty = required_qty * ratio
            account_leg = positions.get(symbol) or {}
            if str(account_leg.get("side") or "").lower() != expected_side:
                failures.append(f"{leg.get('role') or symbol}:side")
            elif capacity.get(symbol, 0.0) < leg_qty:
                failures.append(f"{leg.get('role') or symbol}:quantity")
            requirements.append((symbol, expected_side, leg_qty, str(leg.get("role") or "")))
        if not legs or failures:
            confirmations.append({
                "status": "mismatch", "reason": "ledger_legs_not_confirmed_in_account",
                "unconfirmed_legs": failures or ["position_legs_missing"],
            })
            continue
        for symbol, _, leg_qty, _ in requirements:
            capacity[symbol] -= leg_qty
        confirmations.append({
            "status": "confirmed",
            "confirmed_quantity": required_qty,
            "confirmed_legs": len(requirements),
            "account_as_of": account_context.get("as_of"),
        })
    return confirmations


def _spot(snapshot: dict[str, Any]) -> float | None:
    for path in (
        ("latest_trade", "price"), ("minute_bar", "close"),
        ("daily_bar", "close"),
    ):
        row = snapshot.get(path[0]) or {}
        if row.get(path[1]) is not None:
            try:
                return float(row[path[1]])
            except (TypeError, ValueError):
                continue
    quote = snapshot.get("quote") or {}
    if quote.get("bid") is not None and quote.get("ask") is not None:
        try:
            return (float(quote["bid"]) + float(quote["ask"])) / 2
        except (TypeError, ValueError):
            return None
    return None


def hydrate_live_payload(
    positions: list[dict[str, Any]],
    *,
    option_feed: str = "auto",
    equity_feed: str = "iex",
    adjustment: str = "split",
    cache_mode: str = "auto",
    cache_db: str | None = None,
    events_mode: str = "auto",
    timeout: int = 30,
    finnhub_timeout: int = 15,
    workers: int = 4,
    account_fetcher=fetch_account_context,
    chain_fetcher=fetch_normalized_chain,
    snapshot_fetcher=fetch_snapshots,
    events_fetcher=collect_live_events,
    now_fn=None,
) -> dict[str, Any]:
    """Hydrate open ledger rows with current chains, spots, events, and account proof."""
    current = now_fn() if callable(now_fn) else dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    as_of = current.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    today = current.astimezone(_NEW_YORK).date()
    if not positions:
        return {
            "as_of": as_of, "positions": [], "option_chains": {}, "events": {},
            "underlying_spots": {}, "data_flags": [],
            "account_summary": {"status": "not_requested_no_open_positions"},
            "snapshot_status": "not_requested_no_open_positions",
            "expected_quote_session": None,
        }
    symbols = sorted({str(row.get("underlying_symbol") or "").upper() for row in positions})
    flags: list[str] = []
    try:
        account = account_fetcher(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - review will fail closed per position
        account = {"status": "unavailable", "reason": "account_fetch_failed"}
        flags.append(f"账户持仓核对不可用 ({type(exc).__name__})")
    confirmations = reconcile_positions(positions, account)
    hydrated_positions = [
        {**row, "account_confirmation": confirmation}
        for row, confirmation in zip(positions, confirmations)
    ]

    chain_specs = {}
    for symbol in symbols:
        rows = [row for row in positions if row.get("underlying_symbol") == symbol]
        future = [row for row in rows if str(row.get("expiration_date")) >= today.isoformat()]
        if not future:
            continue
        chain_specs[symbol] = {
            "expiration_gte": min(str(row["expiration_date"]) for row in future),
            "expiration_lte": max(str(row["expiration_date"]) for row in future),
            "strike_gte": min(
                float(leg["strike"]) for row in future for leg in position_legs(row)
            ),
            "strike_lte": max(
                float(leg["strike"]) for row in future for leg in position_legs(row)
            ),
        }

    def fetch_chain(symbol: str) -> tuple[str, dict]:
        spec = chain_specs[symbol]
        try:
            return symbol, chain_fetcher(
                symbol, spec["expiration_gte"], spec["expiration_lte"],
                feed=option_feed, strike_gte=spec["strike_gte"],
                strike_lte=spec["strike_lte"], timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one underlying
            return symbol, {
                "status": "unavailable", "symbol": symbol,
                "reason": "option_chain_fetch_failed", "error_type": type(exc).__name__,
            }

    chains = {}
    if chain_specs:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chain_specs)))) as pool:
            chains = dict(pool.map(fetch_chain, sorted(chain_specs)))
    try:
        snapshots = snapshot_fetcher(
            symbols, equity_feed, timeout, adjustment=adjustment,
            cache_mode=cache_mode, cache_db=cache_db,
        ) if symbols else {"status": "unavailable"}
        if not isinstance(snapshots, dict):
            raise ValueError("snapshot payload must be an object")
    except Exception as exc:  # noqa: BLE001 - spots degrade independently
        snapshots = {"status": "unavailable", "symbols": {}}
        flags.append(f"标的 Snapshot 不可用 ({type(exc).__name__})")
    spots = {
        symbol: value
        for symbol in symbols
        if (value := _spot(((snapshots.get("symbols") or {}).get(symbol) or {}))) is not None
    }
    expected_quote_sessions = {
        symbol: str(
            ((((snapshots.get("symbols") or {}).get(symbol) or {}).get("daily_bar") or {}).get("date") or "")
        )
        for symbol in symbols
    }
    snapshot_dates = list(expected_quote_sessions.values())
    expected_quote_session = max((value for value in snapshot_dates if value), default=None)
    max_expiry = max(
        [str(row.get("expiration_date")) for row in positions] or [today.isoformat()]
    )
    event_args = SimpleNamespace(
        events=events_mode,
        dte_max=max(0, (dt.date.fromisoformat(max_expiry) - today).days),
        finnhub_timeout=finnhub_timeout,
        timeout=timeout,
    )
    try:
        events, event_flags = events_fetcher(
            symbols, today.isoformat(), max(today.isoformat(), max_expiry), event_args
        ) if symbols else ({}, [])
        if not isinstance(events, dict) or not isinstance(event_flags, list):
            raise ValueError("event payload shape is invalid")
    except Exception as exc:  # noqa: BLE001 - events degrade independently
        events = {symbol: {"status": "unavailable"} for symbol in symbols}
        event_flags = [f"事件日历不可用 ({type(exc).__name__})"]
    flags.extend(event_flags)
    return {
        "as_of": as_of,
        "positions": hydrated_positions,
        "option_chains": chains,
        "events": events,
        "underlying_spots": spots,
        "data_flags": flags,
        "account_summary": {
            "status": account.get("status"),
            "as_of": account.get("as_of"),
            "positions_count": account.get("positions_count"),
        },
        "snapshot_status": snapshots.get("status"),
        "expected_quote_session": expected_quote_session,
        "expected_quote_sessions": expected_quote_sessions,
    }
