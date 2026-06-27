#!/usr/bin/env python3
"""Trade plan (entry/exit prices) and reverse-volatility position sizing.

``_trade_plan`` builds the deterministic ATR + MA60 + 30-day-structure plan.
``_position_pct`` is the reverse-volatility position-size suggestion.
``_thirty_day_high``, ``_price``, and ``_pct`` are local helpers; ``_num`` is
imported from ``utils``.
"""
from __future__ import annotations

import math
from typing import Any

from utils import to_num as _num

from .constants import TARGET_VOL_PCT


def _position_pct(a: dict[str, Any], composite: float | None) -> float | None:
    """反波动率仓位建议：目标波动/实现波动 × 信号强度。"""
    vol = (a.get("volatility") or {}).get("ann_vol_3m_pct")
    if not vol or composite is None:
        return None
    size = min(TARGET_VOL_PCT / vol, 1.0) * (composite / 100.0)
    return round(size * 100, 1)


def _price(value: Any) -> float | None:
    n = _num(value)
    if n is None or not math.isfinite(n):
        return None
    return round(n, 4 if abs(n) < 10 else 2)


def _pct(value: Any) -> float | None:
    n = _num(value)
    return round(n, 1) if n is not None and math.isfinite(n) else None


def _thirty_day_high(a: dict[str, Any], last: float) -> float | None:
    dist = _num((a.get("structure_30d") or {}).get("dist_to_30d_high_pct"))
    if dist is None or dist <= -99.0:
        return None
    high = last / (1 + dist / 100.0)
    return high if high > 0 else None


def _trade_plan(
    a: dict[str, Any],
    verdict: str,
    blocking_reasons: list[str],
    confirmation_ok: bool,
) -> dict[str, Any]:
    """Deterministic entry/exit plan from ATR + MA60 + 30-day structure.

    It is a risk-control plan, not an order instruction. For avoided symbols the entry
    price is intentionally None, while the protective exit still helps if the user
    already has a position.
    """
    last = _num(a.get("last_close"))
    if not last or last <= 0:
        return {"status": "unavailable", "reason": "缺少最新收盘价，无法生成入场/出场计划"}

    risk = a.get("risk") or {}
    ma_data = a.get("ma") or {}
    atr = _num(risk.get("atr14"))
    atr_pct = _num(risk.get("atr_pct"))
    ma20 = _num(ma_data.get("MA20"))
    ma60 = _num(ma_data.get("MA60"))
    unit = atr if atr and atr > 0 else last * 0.04
    high_30d = _thirty_day_high(a, last)

    stop_candidates = [last - 2 * unit]
    if ma60 and 0 < ma60 < last:
        stop_candidates.append(ma60 * 0.98)
    stop_candidates = [s for s in stop_candidates if s and 0 < s < last]
    stop_loss = max(stop_candidates) if stop_candidates else last * 0.92
    # Ensure stop has >=3% buffer below last close to absorb daily noise.
    stop_loss = min(stop_loss, last * 0.97)

    pullback_entry = last - unit
    if ma20 and 0 < ma20 < last:
        pullback_entry = max(pullback_entry, ma20)
    pullback_entry = min(max(pullback_entry, stop_loss * 1.01), last)

    if high_30d and high_30d > last * 1.005:
        breakout_entry = high_30d * 1.002
    else:
        breakout_entry = last + 0.5 * unit
    max_chase = last + 0.5 * unit

    if verdict == "是":
        status = "entry_allowed"
        entry = last
        note = "趋势与确认通过，可按当前价附近分批；超过追价上限则等待回踩或突破确认"
    elif verdict == "观察" and not blocking_reasons and confirmation_ok is False:
        status = "wait_confirmation"
        entry = pullback_entry
        note = "评分足够但技术确认不足，等待回踩或重新确认后再入场"
    elif verdict == "观察" and not blocking_reasons:
        status = "wait_pullback"
        entry = pullback_entry
        note = "观察等待，不追价；只在回踩计划价附近或突破确认后考虑"
    else:
        status = "avoid_entry"
        entry = None
        note = "当前不建议新开仓；若已有持仓，优先按保护性出场价控制风险"

    entry_ref = entry if entry is not None else last
    risk_per_share = entry_ref - stop_loss if entry_ref > stop_loss else None
    take_profit_1 = entry_ref + 2 * risk_per_share if risk_per_share else None
    take_profit_2 = entry_ref + 3 * risk_per_share if risk_per_share else None
    trail = atr_pct * 2 if atr_pct is not None else 8.0
    trail = min(max(trail, 6.0), 18.0)

    return {
        "status": status,
        "reference_price": _price(last),
        "suggested_entry_price": _price(entry),
        "pullback_entry_price": _price(pullback_entry),
        "breakout_entry_price": _price(breakout_entry),
        "max_chase_price": _price(max_chase),
        "suggested_exit_price": _price(stop_loss),
        "stop_loss_price": _price(stop_loss),
        "take_profit_price": _price(take_profit_1),
        "take_profit_2_price": _price(take_profit_2),
        "trailing_stop_pct": _pct(trail),
        "risk_per_share": _price(risk_per_share),
        "basis": "ATR14 x2 + MA60 保护线；止盈按 2R/3R，移动止损按 ATR 波动率约束",
        "note": note,
    }
