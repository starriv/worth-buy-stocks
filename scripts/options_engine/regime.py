"""Broad-market regime gate for short-volatility structures."""
from __future__ import annotations

import math
from typing import Any


def _sample_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _benchmark(symbol: str, bars: list[dict]) -> dict[str, Any] | None:
    try:
        closes = [float(row["c"]) for row in bars]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) < 61 or any(not math.isfinite(value) or value <= 0 for value in closes):
        return None
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    prior_ma20 = sum(closes[-25:-5]) / 20
    returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))]
    std = _sample_std(returns)
    return {
        "symbol": symbol,
        "close": round(closes[-1], 4),
        "above_ma20": closes[-1] >= ma20,
        "above_ma60": closes[-1] >= ma60,
        "ma20_slope_5d_pct": round((ma20 / prior_ma20 - 1) * 100, 3),
        "realized_vol_20_pct": round(std * math.sqrt(252) * 100, 2) if std is not None else None,
    }


def evaluate_market_regime(bars_by_symbol: dict[str, list[dict]]) -> dict[str, Any]:
    """Classify SPY/QQQ trend and realized-volatility state.

    Stress pauses new credit spreads. Caution halves the account risk budget.
    Missing benchmark history fails closed at the scoring layer.
    """
    details = {
        symbol: result
        for symbol in ("SPY", "QQQ")
        if (result := _benchmark(symbol, bars_by_symbol.get(symbol) or [])) is not None
    }
    if len(details) < 2:
        return {
            "status": "unavailable",
            "state": "unknown",
            "risk_multiplier": 0.0,
            "score_points": 0,
            "benchmarks": details,
            "reasons": ["SPY_QQQ_history_unavailable"],
        }
    below60 = sum(not row["above_ma60"] for row in details.values())
    negative = sum(row["ma20_slope_5d_pct"] < 0 for row in details.values())
    spy_rv = details["SPY"].get("realized_vol_20_pct") or 0.0
    if below60 == 2 or (not details["SPY"]["above_ma60"] and spy_rv >= 30):
        state, multiplier, points = "stress", 0.0, 0
    elif below60 or negative or spy_rv >= 25:
        state, multiplier, points = "cautious", 0.5, 2
    else:
        state, multiplier, points = "normal", 1.0, 5
    reasons = []
    if below60:
        reasons.append(f"benchmarks_below_ma60:{below60}")
    if negative:
        reasons.append(f"benchmarks_negative_ma20_slope:{negative}")
    if spy_rv >= 25:
        reasons.append(f"SPY_realized_vol_elevated:{spy_rv}")
    return {
        "status": "ok",
        "state": state,
        "risk_multiplier": multiplier,
        "score_points": points,
        "benchmarks": details,
        "reasons": reasons,
    }
