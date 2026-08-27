"""Shared, conservative calculations for defined-risk multi-leg structures."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import OptionsSellerConfig

_NEW_YORK = ZoneInfo("America/New_York")


def number(value: Any) -> float | None:
    """Return a finite float, or ``None`` for unusable market data."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def date_value(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def datetime_value(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def contract_leg(contract: dict[str, Any], side: str, role: str) -> dict[str, Any]:
    """Normalize a chain contract without discarding audit-relevant fields."""
    return {
        "symbol": contract.get("symbol"),
        "underlying_symbol": contract.get("underlying_symbol"),
        "root_symbol": contract.get("root_symbol"),
        "style": contract.get("style"),
        "multiplier": contract.get("multiplier"),
        "side": side,
        "role": role,
        "type": contract.get("type"),
        "expiration_date": contract.get("expiration_date"),
        "strike": contract.get("strike_price"),
        "open_interest": contract.get("open_interest"),
        "open_interest_date": contract.get("open_interest_date"),
        "quote": contract.get("quote") if isinstance(contract.get("quote"), dict) else {},
        "implied_volatility": contract.get("implied_volatility"),
        "greeks": contract.get("greeks") if isinstance(contract.get("greeks"), dict) else {},
    }


def conservative_credit(
    short_contracts: Iterable[dict[str, Any]],
    long_contracts: Iterable[dict[str, Any]],
    *,
    short_price_key: str,
    long_price_key: str,
) -> float | None:
    """Price a credit at natural or midpoint prices without optimistic fallback."""
    short_values = [
        number(((row.get("quote") or {}).get(short_price_key))) for row in short_contracts
    ]
    long_values = [
        number(((row.get("quote") or {}).get(long_price_key))) for row in long_contracts
    ]
    if not short_values or not long_values or any(
        value is None for value in short_values + long_values
    ):
        return None
    return sum(short_values) - sum(long_values)  # type: ignore[arg-type]


def net_greeks(
    positions: Iterable[tuple[int, dict[str, Any]]], multiplier: int
) -> tuple[dict[str, float], dict[str, float]]:
    """Aggregate signed Greeks; long=+1 and short=-1."""
    rows = list(positions)
    per_contract: dict[str, float] = {}
    per_spread: dict[str, float] = {}
    for greek in ("delta", "gamma", "theta", "vega", "rho"):
        values = [number((row.get("greeks") or {}).get(greek)) for _, row in rows]
        if any(value is None for value in values):
            continue
        total = sum(sign * value for (sign, _), value in zip(rows, values))  # type: ignore[operator]
        per_contract[greek] = round(total, 8)
        per_spread[greek] = round(total * multiplier, 6)
    return per_contract, per_spread


def leg_quality_gates(
    leg: dict[str, Any],
    label: str,
    cfg: OptionsSellerConfig,
    *,
    expected_date: str,
    as_of: str | None,
) -> tuple[list[str], list[str]]:
    """Validate one leg's quote, executable side, OI, and session freshness."""
    hard: list[str] = []
    data: list[str] = []
    quote = leg.get("quote") if isinstance(leg.get("quote"), dict) else {}
    bid, ask = number(quote.get("bid")), number(quote.get("ask"))
    spread_pct = number(quote.get("spread_pct"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        data.append(f"{label}_quote_invalid")
    elif spread_pct is None:
        data.append(f"{label}_quote_spread_missing")
    elif spread_pct > cfg.max_quote_spread_pct:
        hard.append(f"{label}_quote_spread_too_wide")

    size_key = "bid_size" if leg.get("side") == "sell_to_open" else "ask_size"
    if (number(quote.get(size_key)) or 0.0) <= 0:
        data.append(f"{label}_executable_size_missing")

    oi = number(leg.get("open_interest"))
    if oi is None:
        data.append(f"{label}_open_interest_missing")
    elif oi < cfg.min_open_interest:
        hard.append(f"{label}_open_interest_below_minimum")

    quoted_at = datetime_value(quote.get("timestamp"))
    observed_at = datetime_value(as_of)
    expected = date_value(expected_date)
    if quoted_at is None:
        data.append(f"{label}_quote_timestamp_invalid")
        return hard, data
    if expected and quoted_at.astimezone(_NEW_YORK).date() < expected:
        data.append(f"{label}_quote_stale")
    if observed_at and quoted_at > observed_at + dt.timedelta(minutes=1):
        data.append(f"{label}_quote_timestamp_future")
    if observed_at and expected:
        observed_et = observed_at.astimezone(_NEW_YORK)
        market_open = (
            observed_et.weekday() < 5
            and dt.time(9, 30) <= observed_et.time() <= dt.time(16, 15)
            and observed_et.date() == expected
        )
        if market_open and observed_at - quoted_at > dt.timedelta(
            minutes=cfg.max_quote_age_minutes
        ):
            data.append(f"{label}_quote_stale")
    return hard, data
