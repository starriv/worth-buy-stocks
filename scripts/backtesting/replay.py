"""Point-in-time score replay that calls the production scoring pipeline."""
from __future__ import annotations

from bisect import bisect_right
from typing import Any, Mapping, Sequence

from pipeline import build_result

from .data import DataBundle


BENCHMARKS = ("SPY", "QQQ")


def _dates(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(row["t"])[:10] for row in rows]


def _score_record(symbol: str, signal_date: str, analysis: Mapping[str, Any]) -> dict[str, Any]:
    score = analysis.get("score") if isinstance(analysis, Mapping) else None
    if not isinstance(score, Mapping):
        return {
            "signal_date": signal_date,
            "symbol": symbol,
            "status": "unavailable",
            "reason": analysis.get("error", "score missing") if isinstance(analysis, Mapping) else "score missing",
        }
    breakdown = score.get("factor_breakdown") or {}
    relative = (breakdown.get("rel_strength") or {}).get("score_pct")
    raw = score.get("raw_composite")
    core_data_complete = raw is not None and relative is not None and score.get("verdict") != "无法评分"
    trade_plan = score.get("trade_plan") if isinstance(score.get("trade_plan"), Mapping) else {}
    return {
        "signal_date": signal_date,
        "symbol": symbol,
        "status": "ok" if core_data_complete else "unavailable",
        "last_close": analysis.get("last_close"),
        "raw_composite": raw,
        "composite": score.get("composite"),
        "base_verdict": score.get("verdict_before_entry_timing"),
        "verdict": score.get("verdict"),
        "suggested_action": score.get("suggested_action"),
        "core_data_complete": core_data_complete,
        "factor_breakdown": breakdown,
        "risk_gates": list(score.get("risk_gates") or []),
        "blocking_reasons": list(score.get("blocking_reasons") or []),
        "cap_applied": score.get("cap_applied"),
        "confirmation": score.get("confirmation") or {},
        "entry_timing": score.get("entry_timing") or {},
        "entry_timing_detail": analysis.get("entry_timing") or {},
        "suggested_position_pct": score.get("suggested_position_pct"),
        "position_exit": score.get("position_exit") or {},
        "trade_plan": {
            key: trade_plan.get(key)
            for key in (
                "status", "suggested_entry_price", "pullback_entry_price",
                "stop_loss_price", "take_profit_price", "take_profit_2_price",
                "trailing_stop_pct", "max_chase_price", "note",
                "break_even_trigger_r", "break_even_buffer_pct",
                "trailing_activation_r",
            )
            if key in trade_plan
        },
        "data_flags": list(score.get("data_flags") or []),
    }


def replay_scores(
    bundle: DataBundle,
    symbol: str,
    *,
    signal_step: int = 1,
) -> dict[str, Any]:
    """Replay current production scoring using only bars available at each date."""

    symbol = str(symbol).strip().upper()
    if signal_step < 1:
        raise ValueError("signal_step must be >= 1")
    symbols = [symbol, *BENCHMARKS]
    missing = [name for name in symbols if name not in bundle.bars]
    if missing:
        raise ValueError(f"missing replay symbols: {', '.join(missing)}")
    date_index = {name: _dates(bundle.bars[name]) for name in symbols}
    calendar = [
        date for date in date_index["SPY"]
        if bundle.requested_start <= date <= bundle.requested_end
    ]
    timeline: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for position in range(0, len(calendar), signal_step):
        signal_date = calendar[position]
        sliced: dict[str, list[dict[str, Any]]] = {}
        for name in symbols:
            end = bisect_right(date_index[name], signal_date)
            sliced[name] = bundle.bars[name][:end]
        if not sliced[symbol] or sliced[symbol][-1]["t"][:10] != signal_date:
            exclusions.append({
                "signal_date": signal_date,
                "reason": "symbol_missing_on_spy_session",
            })
            continue
        result = build_result(symbols, sliced, bundle.feed, bundle.adjustment)
        record = _score_record(symbol, signal_date, result.get("symbols", {}).get(symbol, {}))
        if record["status"] != "ok":
            exclusions.append({
                "signal_date": signal_date,
                "reason": "score_unavailable",
                "detail": record.get("reason") or record.get("blocking_reasons"),
            })
        timeline.append(record)
    return {
        "symbol": symbol,
        "signal_step": signal_step,
        "calendar": calendar,
        "timeline": timeline,
        "exclusions": exclusions,
    }
