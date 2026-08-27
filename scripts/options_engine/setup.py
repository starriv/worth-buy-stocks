"""Bollinger-based underlying setup detection for defined-risk credit spreads."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from metrics import annualized_vol, atr, bollinger_bands, bollinger_series, ma

from .config import OptionsSellerConfig


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal(
    strategy: str,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    bands: list[dict | None],
    latest: dict,
    cfg: OptionsSellerConfig,
) -> dict:
    start = max(0, len(closes) - cfg.boll_touch_lookback)
    recent = range(start, len(closes))
    if strategy == "bull_put":
        touched = any(
            isinstance(bands[i], dict) and lows[i] <= bands[i]["lower"]
            for i in recent
        )
        reentered = closes[-1] >= latest["lower"]
        reversal = len(closes) >= 2 and closes[-1] > closes[-2]
        slope_ok = latest.get("middle_slope_pct") is not None and (
            latest["middle_slope_pct"] >= -cfg.boll_flat_slope_pct
        )
        band_anchor = latest["lower"]
        label = "lower_band_reentry"
    else:
        touched = any(
            isinstance(bands[i], dict) and highs[i] >= bands[i]["upper"]
            for i in recent
        )
        reentered = closes[-1] <= latest["upper"]
        reversal = len(closes) >= 2 and closes[-1] < closes[-2]
        slope_ok = latest.get("middle_slope_pct") is not None and (
            latest["middle_slope_pct"] <= cfg.boll_flat_slope_pct
        )
        band_anchor = latest["upper"]
        label = "upper_band_rejection"

    checks = {
        "band_touched": touched,
        "closed_back_inside": reentered,
        "reversal_confirmed": reversal,
        "middle_slope_compatible": slope_ok,
    }
    score = sum((10, 8, 7, 5)[i] for i, ok in enumerate(checks.values()) if ok)
    reasons = [name for name, ok in checks.items() if not ok]
    return {
        "strategy": strategy,
        "classification": label if all(checks.values()) else "no_setup",
        "eligible": all(checks.values()),
        "boll_score": score,
        "band_anchor": round(band_anchor, 4),
        "checks": checks,
        "blocking_reasons": reasons,
    }


def evaluate_bollinger_setup(
    bars: list[dict[str, Any]], cfg: OptionsSellerConfig
) -> dict[str, Any]:
    """Evaluate both directional credit-spread setups from completed daily bars."""
    cfg.validate()
    clean = [b for b in bars or [] if all(k in b for k in ("t", "h", "l", "c", "v"))]
    minimum = cfg.boll_window + 5
    if len(clean) < minimum:
        return {"status": "insufficient_data", "reason": f"daily bars < {minimum}"}
    closes = [_number(b["c"]) for b in clean]
    highs = [_number(b["h"]) for b in clean]
    lows = [_number(b["l"]) for b in clean]
    volumes = [_number(b["v"]) for b in clean]
    if any(v is None for seq in (closes, highs, lows, volumes) for v in seq):
        return {"status": "insufficient_data", "reason": "daily bars contain non-numeric OHLCV"}
    if any(not math.isfinite(float(v)) for seq in (closes, highs, lows, volumes) for v in seq):
        return {"status": "insufficient_data", "reason": "daily bars contain non-finite OHLCV"}
    timestamps = []
    for row in clean:
        try:
            parsed = dt.datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
        except ValueError:
            return {"status": "insufficient_data", "reason": "daily bars contain invalid timestamps"}
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        parsed = parsed.astimezone(dt.timezone.utc)
        timestamps.append(parsed)
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        return {"status": "insufficient_data", "reason": "daily bars must be unique and ascending"}
    closes_f = [float(v) for v in closes]
    highs_f = [float(v) for v in highs]
    lows_f = [float(v) for v in lows]
    volumes_f = [float(v) for v in volumes]
    series = bollinger_series(closes_f, cfg.boll_window, cfg.boll_deviations)
    latest = bollinger_bands(closes_f, cfg.boll_window, cfg.boll_deviations)
    if latest is None:
        return {"status": "insufficient_data", "reason": "Bollinger bands unavailable"}

    last = closes_f[-1]
    ma60 = ma(closes_f, 60)
    avg_volume_20 = sum(volumes_f[-20:]) / min(20, len(volumes_f))
    avg_dollar_volume_20 = sum(
        c * v for c, v in zip(closes_f[-20:], volumes_f[-20:])
    ) / min(20, len(closes_f))
    atr14 = atr(highs_f, lows_f, closes_f, 14)
    rv20 = annualized_vol(closes_f, 20)
    liquidity_ok = (
        last >= cfg.min_underlying_price
        and avg_volume_20 >= cfg.min_underlying_volume
        and avg_dollar_volume_20 >= cfg.min_underlying_dollar_volume
    )
    signals = {
        strategy: _signal(strategy, closes_f, highs_f, lows_f, series, latest, cfg)
        for strategy in ("bull_put", "bear_call")
    }
    for strategy, signal in signals.items():
        if strategy == "bull_put":
            trend_checks = {
                "price_vs_middle": last >= latest["middle"],
                "price_vs_ma60": ma60 is not None and last >= ma60,
                "middle_slope_direction": latest.get("middle_slope_pct", -999) >= 0,
                "not_below_lower_band": last >= latest["lower"],
            }
        else:
            trend_checks = {
                "price_vs_middle": last <= latest["middle"],
                "price_vs_ma60": ma60 is not None and last <= ma60,
                "middle_slope_direction": latest.get("middle_slope_pct", 999) <= 0,
                "not_above_upper_band": last <= latest["upper"],
            }
        signal["trend_checks"] = trend_checks
        signal["trend_score"] = sum(5 for ok in trend_checks.values() if ok)

    return {
        "status": "ok",
        "last_date": str(clean[-1]["t"])[:10],
        "last_close": round(last, 4),
        "bollinger": latest,
        "atr14": atr14,
        "realized_vol_20_pct": rv20,
        "ma60": ma60,
        "liquidity": {
            "eligible": liquidity_ok,
            "avg_volume_20": round(avg_volume_20, 2),
            "avg_dollar_volume_20": round(avg_dollar_volume_20, 2),
        },
        "signals": signals,
    }
