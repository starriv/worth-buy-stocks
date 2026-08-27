"""Dynamic spread-width constraints and three risk/reward profiles."""
from __future__ import annotations

from typing import Any

from .config import OptionsSellerConfig


def dynamic_width_bounds(setup: dict[str, Any], cfg: OptionsSellerConfig) -> tuple[float, float]:
    atr = float(setup.get("atr14") or 0.0)
    lower = max(cfg.min_width, min(cfg.max_width, atr * cfg.dynamic_width_atr_min))
    upper = min(cfg.max_width, max(lower, atr * cfg.dynamic_width_atr_max))
    return round(lower, 4), round(upper, 4)


def candidate_id(candidate: dict[str, Any]) -> str:
    legs = candidate.get("legs")
    if isinstance(legs, list) and legs:
        leg_key = ",".join(
            f"{leg.get('role') or leg.get('side')}:{leg.get('symbol') or leg.get('strike')}"
            for leg in legs if isinstance(leg, dict)
        )
        return "|".join(str(value) for value in (
            candidate.get("underlying_symbol"), candidate.get("strategy"),
            candidate.get("expiration_date"), candidate.get("back_expiration_date") or "",
            leg_key,
        ))
    return "|".join(str(value) for value in (
        candidate.get("underlying_symbol"), candidate.get("strategy"),
        candidate.get("expiration_date"),
        candidate.get("back_expiration_date") or "",
        (candidate.get("short_leg") or {}).get("strike"),
        (candidate.get("long_leg") or {}).get("strike"),
    ))


def _num(value: Any, fallback: float = -1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _summary(profile: str, row: dict[str, Any]) -> dict[str, Any]:
    surface = row.get("volatility_surface") or {}
    return {
        "profile": profile,
        "candidate_id": row["candidate_id"],
        "verdict": (row.get("score") or {}).get("verdict"),
        "seller_score": (row.get("score") or {}).get("seller_score"),
        "expiration_date": row.get("expiration_date"),
        "short_strike": (row.get("short_leg") or {}).get("strike"),
        "long_strike": (row.get("long_leg") or {}).get("strike"),
        "width": row.get("width"),
        "expected_move_coverage": surface.get("expected_move_coverage"),
        "return_on_risk_pct": (
            row.get("net_return_on_risk_pct")
            if row.get("net_return_on_risk_pct") is not None
            else row.get("return_on_risk_pct")
        ),
        "net_max_loss": row.get("net_max_loss") if row.get("net_max_loss") is not None else row.get("max_loss"),
    }


def assign_profiles(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Label up to three executable candidates as safety/balanced/income."""
    rows = [dict(row, candidate_id=candidate_id(row)) for row in candidates]
    eligible = [row for row in rows if (row.get("score") or {}).get("verdict") in {"适合构建", "观察等待"}]
    if not eligible:
        return rows, {}

    def safety(row: dict) -> tuple:
        return (
            _num((row.get("volatility_surface") or {}).get("expected_move_coverage")),
            _num(row.get("band_buffer_pct")),
            -abs(_num(((row.get("short_leg") or {}).get("greeks") or {}).get("delta"), 1.0)),
            -_num(row.get("max_loss"), 1e12),
        )

    def balanced(row: dict) -> tuple:
        return (
            _num((row.get("score") or {}).get("seller_score")),
            _num(
                row.get("net_return_on_risk_pct")
                if row.get("net_return_on_risk_pct") is not None
                else row.get("return_on_risk_pct")
            ),
        )

    def income(row: dict) -> tuple:
        theta = _num((row.get("net_greeks_per_spread") or {}).get("theta"), 0.0)
        loss = max(1.0, _num(row.get("net_max_loss") or row.get("max_loss"), 1.0))
        return (
            _num(
                row.get("net_return_on_risk_pct")
                if row.get("net_return_on_risk_pct") is not None
                else row.get("return_on_risk_pct")
            ),
            theta / loss,
            _num(
                row.get("net_credit_width_pct")
                if row.get("net_credit_width_pct") is not None
                else row.get("credit_width_pct")
            ),
        )

    profiles: dict[str, Any] = {}
    chosen: set[str] = set()
    for name, key in (("balanced", balanced), ("safety", safety), ("income", income)):
        ordered = sorted(eligible, key=key, reverse=True)
        selected = next((row for row in ordered if row["candidate_id"] not in chosen), ordered[0])
        chosen.add(selected["candidate_id"])
        profiles[name] = _summary(name, selected)
        selected.setdefault("optimization_profiles", []).append(name)
    return rows, profiles
