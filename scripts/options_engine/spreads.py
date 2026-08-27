"""Construct same-expiry defined-risk vertical credit spread candidates."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from .config import OptionsSellerConfig
from .structure_optimizer import dynamic_width_bounds


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _quote(contract: dict) -> dict:
    quote = contract.get("quote") if isinstance(contract.get("quote"), dict) else {}
    return quote


def _leg(contract: dict, side: str) -> dict:
    return {
        "symbol": contract.get("symbol"),
        "underlying_symbol": contract.get("underlying_symbol"),
        "root_symbol": contract.get("root_symbol"),
        "style": contract.get("style"),
        "multiplier": contract.get("multiplier"),
        "side": side,
        "strike": contract.get("strike_price"),
        "open_interest": contract.get("open_interest"),
        "open_interest_date": contract.get("open_interest_date"),
        "quote": _quote(contract),
        "implied_volatility": contract.get("implied_volatility"),
        "greeks": contract.get("greeks") or {},
    }


def _candidate(
    symbol: str,
    strategy: str,
    short: dict,
    long: dict,
    spot: float,
    band_anchor: float,
    as_of_date: dt.date,
    round_trip_cost: float | None,
) -> dict:
    short_strike = float(short["strike_price"])
    long_strike = float(long["strike_price"])
    width = abs(short_strike - long_strike)
    short_quote, long_quote = _quote(short), _quote(long)
    short_bid, short_ask = short_quote.get("bid"), short_quote.get("ask")
    long_bid, long_ask = long_quote.get("bid"), long_quote.get("ask")
    natural_credit = (
        float(short_bid) - float(long_ask)
        if short_bid is not None and long_ask is not None else None
    )
    short_mid = short_quote.get("mid")
    long_mid = long_quote.get("mid")
    mid_credit = (
        float(short_mid) - float(long_mid)
        if short_mid is not None and long_mid is not None else None
    )
    expiry = _date(short.get("expiration_date"))
    dte = (expiry - as_of_date).days if expiry else None
    multiplier = int(short.get("multiplier") or 0)
    if strategy == "bull_put":
        break_even = short_strike - natural_credit if natural_credit is not None else None
        spot_distance = (spot - short_strike) / spot * 100
        band_buffer = (band_anchor - short_strike) / spot * 100
    else:
        break_even = short_strike + natural_credit if natural_credit is not None else None
        spot_distance = (short_strike - spot) / spot * 100
        band_buffer = (short_strike - band_anchor) / spot * 100
    max_loss = (
        (width - natural_credit) * multiplier
        if natural_credit is not None and 0 < natural_credit < width else None
    )
    max_profit = natural_credit * multiplier if natural_credit is not None else None
    net_max_profit = (
        max_profit - round_trip_cost
        if max_profit is not None and round_trip_cost is not None else None
    )
    net_max_loss = (
        max_loss + round_trip_cost
        if max_loss is not None and round_trip_cost is not None else None
    )
    net_credit = (
        natural_credit - round_trip_cost / multiplier
        if natural_credit is not None and round_trip_cost is not None and multiplier > 0
        else None
    )
    net_greeks = {}
    short_greeks = short.get("greeks") or {}
    long_greeks = long.get("greeks") or {}
    for greek in ("delta", "gamma", "theta", "vega", "rho"):
        short_value = short_greeks.get(greek)
        long_value = long_greeks.get(greek)
        if short_value is not None and long_value is not None and multiplier > 0:
            net_greeks[greek] = round(
                (float(long_value) - float(short_value)) * multiplier, 6
            )
    return {
        "underlying_symbol": symbol,
        "strategy": strategy,
        "expiration_date": short.get("expiration_date"),
        "dte": dte,
        "short_leg": _leg(short, "sell_to_open"),
        "long_leg": _leg(long, "buy_to_open"),
        "width": round(width, 4),
        "natural_credit": round(natural_credit, 4) if natural_credit is not None else None,
        "mid_credit": round(mid_credit, 4) if mid_credit is not None else None,
        "multiplier": multiplier,
        "max_profit": round(max_profit, 2) if max_profit is not None else None,
        "max_loss": round(max_loss, 2) if max_loss is not None else None,
        "estimated_round_trip_cost": round_trip_cost,
        "net_credit": round(net_credit, 4) if net_credit is not None else None,
        "net_max_profit": round(net_max_profit, 2) if net_max_profit is not None else None,
        "net_max_loss": round(net_max_loss, 2) if net_max_loss is not None else None,
        "net_greeks_per_spread": net_greeks,
        "break_even": round(break_even, 4) if break_even is not None else None,
        "credit_width_pct": (
            round(natural_credit / width * 100, 2)
            if natural_credit is not None and width else None
        ),
        "net_credit_width_pct": (
            round(net_credit / width * 100, 2)
            if net_credit is not None and width else None
        ),
        "return_on_risk_pct": (
            round(natural_credit / (width - natural_credit) * 100, 2)
            if natural_credit is not None and 0 < natural_credit < width else None
        ),
        "net_return_on_risk_pct": (
            round(net_max_profit / net_max_loss * 100, 2)
            if net_max_profit is not None and net_max_loss and net_max_loss > 0 else None
        ),
        "short_strike_distance_pct": round(spot_distance, 2),
        "band_buffer_pct": round(band_buffer, 2),
    }


def build_vertical_spreads(
    symbol: str,
    strategy: str,
    chain: dict[str, Any],
    setup: dict[str, Any],
    cfg: OptionsSellerConfig,
    *,
    as_of_date: dt.date,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build all plausible bull-put or bear-call verticals before scoring."""
    if strategy not in {"bull_put", "bear_call"}:
        raise ValueError("strategy must be bull_put or bear_call")
    option_type = "put" if strategy == "bull_put" else "call"
    spot = float(setup["last_close"])
    signal = setup["signals"][strategy]
    band_anchor = float(signal["band_anchor"])
    by_expiry: dict[str, list[dict]] = defaultdict(list)
    rejected: dict[str, int] = defaultdict(int)
    contracts_seen = 0

    def reject(reason: str) -> None:
        rejected[reason] += 1

    for row in chain.get("contracts") or []:
        if not isinstance(row, dict) or row.get("type") != option_type:
            continue
        contracts_seen += 1
        if not row.get("tradable", True):
            reject("contract_not_tradable")
            continue
        if str(row.get("underlying_symbol") or "").upper() != symbol:
            reject("underlying_symbol_mismatch")
            continue
        if str(row.get("root_symbol") or "").upper() != symbol:
            reject("adjusted_or_mismatched_root")
            continue
        if str(row.get("style") or "").lower() != "american":
            reject("unsupported_option_style")
            continue
        if int(row.get("multiplier") or 0) != 100:
            reject("non_standard_multiplier")
            continue
        expiry = _date(row.get("expiration_date"))
        strike = row.get("strike_price")
        if expiry is None or strike is None:
            reject("invalid_expiration_or_strike")
            continue
        dte = (expiry - as_of_date).days
        if not cfg.dte_min <= dte <= cfg.dte_max:
            reject("dte_outside_range")
            continue
        strike_f = float(strike)
        if abs(strike_f - spot) / spot * 100 > cfg.max_short_distance_pct:
            reject("strike_too_far_from_spot")
            continue
        by_expiry[expiry.isoformat()].append(row)

    out = []
    qualified_shorts = 0
    dynamic_min_width, dynamic_max_width = dynamic_width_bounds(setup, cfg)
    for expiry in sorted(by_expiry):
        rows = sorted(by_expiry[expiry], key=lambda row: float(row["strike_price"]))
        for short in rows:
            strike = float(short["strike_price"])
            delta = (short.get("greeks") or {}).get("delta")
            abs_delta = abs(float(delta)) if delta is not None else None
            location_ok = strike <= band_anchor if strategy == "bull_put" else strike >= band_anchor
            otm = strike < spot if strategy == "bull_put" else strike > spot
            delta_ok = abs_delta is None or cfg.short_delta_min <= abs_delta <= cfg.short_delta_max
            if not location_ok:
                reject("short_strike_inside_bollinger_band")
                continue
            if not otm:
                reject("short_strike_not_otm")
                continue
            if not delta_ok:
                reject("short_delta_outside_range")
                continue
            qualified_shorts += 1
            longs = [
                row for row in rows
                if (
                    float(row["strike_price"]) < strike
                    if strategy == "bull_put"
                    else float(row["strike_price"]) > strike
                )
                and dynamic_min_width <= abs(float(row["strike_price"]) - strike) <= dynamic_max_width
            ]
            if not longs:
                reject("protective_leg_unavailable")
            for long in longs:
                if any(
                    long.get(key) != short.get(key)
                    for key in ("underlying_symbol", "root_symbol", "style", "multiplier")
                ):
                    reject("incompatible_spread_legs")
                    continue
                out.append(_candidate(
                    symbol,
                    strategy,
                    short,
                    long,
                    spot,
                    band_anchor,
                    as_of_date,
                    cfg.round_trip_cost_per_spread,
                ))
    if diagnostics is not None:
        diagnostics.update({
            "contracts_seen": contracts_seen,
            "qualified_shorts": qualified_shorts,
            "candidates_built": len(out),
            "dynamic_width_bounds": {
                "minimum": dynamic_min_width,
                "maximum": dynamic_max_width,
                "atr14": setup.get("atr14"),
            },
            "rejections": dict(sorted(rejected.items())),
        })
    return out
