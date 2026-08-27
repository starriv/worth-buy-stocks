"""Current-chain volatility surface and expected-move diagnostics.

This module deliberately does not label a current IV observation as IV rank or
percentile.  Those measures require a point-in-time history that the live chain
does not provide.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _nearest(rows: list[dict], key, target: float) -> dict | None:
    usable = [(abs(value - target), row) for row in rows if (value := key(row)) is not None]
    return min(usable, key=lambda pair: pair[0])[1] if usable else None


def _iv(row: dict | None) -> float | None:
    value = _num((row or {}).get("implied_volatility"))
    return value if value is not None and value > 0 else None


def _abs_delta(row: dict) -> float | None:
    value = _num((row.get("greeks") or {}).get("delta"))
    return abs(value) if value is not None and value != 0 else None


def build_volatility_surface(
    chain: dict[str, Any], spot: float, *, as_of_date: dt.date
) -> dict[str, Any]:
    """Build a compact expiry/strike surface from same-snapshot option data."""
    by_expiry: dict[str, list[dict]] = {}
    symbol = str(chain.get("symbol") or "").upper()
    for row in chain.get("contracts") or []:
        if not isinstance(row, dict) or not row.get("tradable", True):
            continue
        if symbol and (
            str(row.get("underlying_symbol") or "").upper() != symbol
            or str(row.get("root_symbol") or "").upper() != symbol
        ):
            continue
        if str(row.get("style") or "").lower() != "american":
            continue
        if int(row.get("multiplier") or 0) != 100:
            continue
        expiry = _date(row.get("expiration_date"))
        strike = _num(row.get("strike_price"))
        if expiry is None or expiry <= as_of_date or strike is None or _iv(row) is None:
            continue
        if str(row.get("type") or "").lower() not in {"put", "call"}:
            continue
        by_expiry.setdefault(expiry.isoformat(), []).append(row)

    expiries: dict[str, dict[str, Any]] = {}
    for expiry_text, rows in sorted(by_expiry.items()):
        expiry = dt.date.fromisoformat(expiry_text)
        calls = [row for row in rows if row.get("type") == "call"]
        puts = [row for row in rows if row.get("type") == "put"]
        atm_rows = [
            row for group in (calls, puts)
            if (row := _nearest(group, lambda item: _num(item.get("strike_price")), spot))
        ]
        atm_values = [value for row in atm_rows if (value := _iv(row)) is not None]
        if not atm_values:
            continue
        atm_iv = statistics.median(atm_values)
        put25 = _nearest(
            puts,
            _abs_delta,
            0.25,
        )
        call25 = _nearest(
            calls,
            _abs_delta,
            0.25,
        )
        put25_iv, call25_iv = _iv(put25), _iv(call25)
        dte = (expiry - as_of_date).days
        expected_move_abs = spot * atm_iv * math.sqrt(dte / 365.0)
        expiries[expiry_text] = {
            "dte": dte,
            "atm_iv": round(atm_iv, 6),
            "atm_iv_pct": round(atm_iv * 100, 2),
            "one_sigma_expected_move_abs": round(expected_move_abs, 4),
            "one_sigma_expected_move_pct": round(expected_move_abs / spot * 100, 2),
            "put_25d_iv_pct": round(put25_iv * 100, 2) if put25_iv else None,
            "call_25d_iv_pct": round(call25_iv * 100, 2) if call25_iv else None,
            "put_skew_25d_vol_points": round((put25_iv - atm_iv) * 100, 2) if put25_iv else None,
            "call_skew_25d_vol_points": round((call25_iv - atm_iv) * 100, 2) if call25_iv else None,
            "risk_reversal_25d_vol_points": (
                round((put25_iv - call25_iv) * 100, 2)
                if put25_iv and call25_iv else None
            ),
            "contracts_with_iv": len(rows),
        }

    ordered = list(expiries)
    term = None
    if ordered:
        near, far = expiries[ordered[0]], expiries[ordered[-1]]
        term = {
            "near_expiration": ordered[0],
            "far_expiration": ordered[-1],
            "atm_iv_slope_vol_points": round(far["atm_iv_pct"] - near["atm_iv_pct"], 2),
            "front_to_back_ratio": round(near["atm_iv"] / far["atm_iv"], 4)
            if far["atm_iv"] else None,
        }
    return {
        "status": "ok" if expiries else "unavailable",
        "as_of": chain.get("as_of"),
        "expirations": expiries,
        "term_structure": term,
        "historical_iv_rank": None,
        "historical_iv_rank_status": "unavailable_without_point_in_time_history",
        "data_flags": [] if expiries else ["volatility_surface_unavailable"],
    }


def enrich_spread_with_surface(
    spread: dict[str, Any], surface: dict[str, Any], setup: dict[str, Any]
) -> dict[str, Any]:
    """Attach expiry-matched IV and expected-move features to a spread copy."""
    out = dict(spread)
    expiry = (surface.get("expirations") or {}).get(str(spread.get("expiration_date"))) or {}
    spot = _num(setup.get("last_close"))
    short_strike = _num((spread.get("short_leg") or {}).get("strike"))
    short_iv = _num((spread.get("short_leg") or {}).get("implied_volatility"))
    atm_iv = _num(expiry.get("atm_iv"))
    expected_move = _num(expiry.get("one_sigma_expected_move_abs"))
    distance = abs(spot - short_strike) if spot is not None and short_strike is not None else None
    coverage = distance / expected_move if distance is not None and expected_move else None
    out["volatility_surface"] = {
        "atm_iv_pct": expiry.get("atm_iv_pct"),
        "short_iv_premium_vol_points": round((short_iv - atm_iv) * 100, 2)
        if short_iv is not None and atm_iv is not None else None,
        "one_sigma_expected_move_abs": expected_move,
        "one_sigma_expected_move_pct": expiry.get("one_sigma_expected_move_pct"),
        "expected_move_coverage": round(coverage, 4) if coverage is not None else None,
        "put_skew_25d_vol_points": expiry.get("put_skew_25d_vol_points"),
        "call_skew_25d_vol_points": expiry.get("call_skew_25d_vol_points"),
        "term_structure_slope_vol_points": (
            (surface.get("term_structure") or {}).get("atm_iv_slope_vol_points")
        ),
        "constant_maturity_30d_iv_pct": (
            (surface.get("constant_maturity_30d") or {}).get("iv_pct")
        ),
        "historical_iv_rank": (surface.get("historical_iv") or {}).get("iv_rank"),
        "historical_iv_percentile": (
            (surface.get("historical_iv") or {}).get("iv_percentile")
        ),
    }
    return out
