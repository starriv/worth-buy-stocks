"""Single-symbol fixed-horizon event study and pre-registered validation."""
from __future__ import annotations

import statistics as py_stats
from bisect import bisect_right
from typing import Any, Mapping, Sequence

from .contracts import BOOTSTRAP_SEED, MIN_INFERENCE_GROUP, PRIMARY_HORIZON
from .statistics import (
    block_bootstrap_ci,
    block_bootstrap_difference_ci,
    block_length_for,
    spearman,
)
from .universe import BANKRUPT


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(rows: Sequence[Mapping[str, Any]], field: str = "spy_excess_pct") -> dict[str, Any]:
    values = [float(row[field]) for row in rows if isinstance(row.get(field), (int, float))]
    if not values:
        return {"n": 0, "mean_pct": None, "median_pct": None, "win_rate_pct": None,
                "p25_pct": None, "p75_pct": None}
    return {
        "n": len(values),
        "mean_pct": _round(sum(values) / len(values)),
        "median_pct": _round(float(py_stats.median(values))),
        "win_rate_pct": _round(sum(value > 0 for value in values) / len(values) * 100, 2),
        "p25_pct": _round(_percentile(values, 0.25)),
        "p75_pct": _round(_percentile(values, 0.75)),
    }


def _grouped(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(field) or "unknown"), []).append(row)
    return {key: _summary(groups[key]) for key in sorted(groups)}


def _score_bucket(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unavailable"
    if value < 60:
        return "lt_60"
    if value < 75:
        return "60_to_74_9"
    return "gte_75"


def _bar_maps(bars: Mapping[str, Sequence[Mapping[str, Any]]]):
    by_date = {
        symbol: {str(row["t"])[:10]: row for row in rows}
        for symbol, rows in bars.items()
    }
    dates = {symbol: sorted(mapping) for symbol, mapping in by_date.items()}
    return by_date, dates


def _last_on_or_before(
    symbol: str,
    target_date: str,
    signal_date: str,
    by_date: Mapping[str, Mapping[str, Mapping[str, Any]]],
    dates: Mapping[str, Sequence[str]],
) -> tuple[str, Mapping[str, Any]] | None:
    values = dates.get(symbol, [])
    index = bisect_right(values, target_date) - 1
    if index < 0 or values[index] <= signal_date:
        return None
    date = values[index]
    return date, by_date[symbol][date]


def _event(
    record: Mapping[str, Any],
    symbol: str,
    horizon: int,
    calendar: Sequence[str],
    calendar_index: Mapping[str, int],
    by_date: Mapping[str, Mapping[str, Mapping[str, Any]]],
    dates: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    signal_date = str(record["signal_date"])
    position = calendar_index.get(signal_date)
    if position is None or position + horizon >= len(calendar):
        return None, {"signal_date": signal_date, "horizon": horizon, "reason": "horizon_immature"}
    target_date = calendar[position + horizon]
    entry = by_date.get(symbol, {}).get(signal_date)
    target = by_date.get(symbol, {}).get(target_date)
    target_price_date = target_date
    degraded: str | None = None
    if target is None:
        # Halts and delistings correlate with bad outcomes; excluding these
        # events would bias the study upward.  A stale mark from the last
        # close strictly after the signal is conservative and never uses
        # post-resumption future prices.
        stale = _last_on_or_before(symbol, target_date, signal_date, by_date, dates)
        if stale is None:
            return None, {
                "signal_date": signal_date,
                "horizon": horizon,
                "target_date": target_date,
                "reason": "no_price_after_signal",
            }
        target_price_date, target = stale
        symbol_ended = bool(dates.get(symbol)) and dates[symbol][-1] < target_date
        if symbol in BANKRUPT and symbol_ended:
            degraded = "bankrupt_terminal"
        elif symbol_ended:
            degraded = "terminal_price_before_horizon"
        else:
            degraded = "stale_target_price"
    spy_entry = by_date.get("SPY", {}).get(signal_date)
    spy_target = by_date.get("SPY", {}).get(target_date)
    qqq_entry = by_date.get("QQQ", {}).get(signal_date)
    qqq_target = by_date.get("QQQ", {}).get(target_date)
    if not all((entry, target, spy_entry, spy_target, qqq_entry, qqq_target)):
        return None, {"signal_date": signal_date, "horizon": horizon, "reason": "price_missing"}
    start_price = float(entry["c"])
    if degraded == "bankrupt_terminal":
        symbol_return = -100.0
        target_price = 0.0
        target_price_date = target_date
    else:
        target_price = float(target["c"])
        symbol_return = (target_price / start_price - 1) * 100
    spy_return = (float(spy_target["c"]) / float(spy_entry["c"]) - 1) * 100
    qqq_return = (float(qqq_target["c"]) / float(qqq_entry["c"]) - 1) * 100
    path = [
        row for date, row in by_date.get(symbol, {}).items()
        if signal_date < date <= target_date
    ]
    mfe = (max(float(row["h"]) for row in path) / start_price - 1) * 100 if path else None
    mae = (min(float(row["l"]) for row in path) / start_price - 1) * 100 if path else None
    if degraded == "bankrupt_terminal":
        mae = -100.0
    raw = record.get("raw_composite")
    confirmation = record.get("confirmation") or {}
    entry_timing = record.get("entry_timing") or {}
    rank_signal = bool(record.get("core_data_complete") and isinstance(raw, (int, float)) and raw >= 75)
    disciplined = record.get("verdict") == "是"
    return {
        "signal_date": signal_date,
        "target_date": target_date,
        "target_price_date": target_price_date,
        "degraded": degraded,
        "calendar_index": position,
        "horizon": horizon,
        "start_close": _round(start_price),
        "target_close": _round(target_price),
        "symbol_return_pct": _round(symbol_return),
        "spy_return_pct": _round(spy_return),
        "qqq_return_pct": _round(qqq_return),
        "spy_excess_pct": _round(symbol_return - spy_return),
        "qqq_excess_pct": _round(symbol_return - qqq_return),
        "mfe_pct": _round(mfe),
        "mae_pct": _round(mae),
        "raw_composite": raw,
        "composite": record.get("composite"),
        "score_bucket": _score_bucket(record.get("composite")),
        "verdict": record.get("verdict"),
        "rank_signal": rank_signal,
        "rank_signal_group": "rank_signal" if rank_signal else "no_rank_signal",
        "disciplined_signal": disciplined,
        "disciplined_group": "yes" if disciplined else "not_yes",
        "confirmation_group": "confirmed" if confirmation.get("ok") else "blocked",
        "risk_gate_group": "gated" if record.get("risk_gates") else "ungated",
        "entry_timing": entry_timing.get("classification") or "unknown",
    }, None


def _non_overlapping(events: Sequence[Mapping[str, Any]], horizon: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_index: int | None = None
    for event in sorted(events, key=lambda row: int(row["calendar_index"])):
        index = int(event["calendar_index"])
        if last_index is None or index - last_index >= horizon:
            selected.append(dict(event))
            last_index = index
    return selected


def _inference(events: Sequence[Mapping[str, Any]], horizon: int) -> dict[str, Any]:
    sample = _non_overlapping(events, horizon)
    scored = [
        row for row in sample
        if isinstance(row.get("composite"), (int, float))
        and isinstance(row.get("spy_excess_pct"), (int, float))
    ]
    ic = spearman(
        [float(row["composite"]) for row in scored],
        [float(row["spy_excess_pct"]) for row in scored],
    )
    yes = [float(row["spy_excess_pct"]) for row in sample if row["disciplined_signal"]]
    not_yes = [float(row["spy_excess_pct"]) for row in sample if not row["disciplined_signal"]]
    # Non-overlap removes mechanical window overlap, but "yes" signals still
    # cluster within regimes; circular block resampling keeps that serial
    # dependence in the CI instead of assuming iid draws.
    yes_ci = block_bootstrap_ci(yes, seed=BOOTSTRAP_SEED)
    contrast_ci = block_bootstrap_difference_ci(yes, not_yes, seed=BOOTSTRAP_SEED)
    return {
        "anchor": sample[0]["signal_date"] if sample else None,
        "sample_n": len(sample),
        "ci_method": "circular_block_bootstrap",
        "ci_block_lengths": {
            "yes": block_length_for(len(yes)) if len(yes) >= 3 else None,
            "not_yes": block_length_for(len(not_yes)) if len(not_yes) >= 3 else None,
        },
        "spearman_ic": _round(ic),
        "disciplined_yes": _summary(
            [row for row in sample if row["disciplined_signal"]]
        ),
        "not_yes": _summary(
            [row for row in sample if not row["disciplined_signal"]]
        ),
        "yes_mean_ci_95": [_round(value) for value in yes_ci] if yes_ci else None,
        "yes_minus_not_yes_mean_pct": _round(
            (sum(yes) / len(yes) - sum(not_yes) / len(not_yes))
            if yes and not_yes else None
        ),
        "yes_minus_not_yes_ci_95": (
            [_round(value) for value in contrast_ci] if contrast_ci else None
        ),
        "events": sample,
    }


def _validation(primary: Mapping[str, Any], min_group: int) -> dict[str, Any]:
    yes_n = int((primary.get("disciplined_yes") or {}).get("n") or 0)
    not_yes_n = int((primary.get("not_yes") or {}).get("n") or 0)
    yes_ci = primary.get("yes_mean_ci_95")
    contrast_ci = primary.get("yes_minus_not_yes_ci_95")
    enough = yes_n >= min_group and not_yes_n >= min_group
    supports = bool(enough and yes_ci and contrast_ci and yes_ci[0] > 0 and contrast_ci[0] > 0)
    contradicts = bool(enough and yes_ci and contrast_ci and yes_ci[1] < 0 and contrast_ci[1] < 0)
    status = "supports" if supports else "contradicts" if contradicts else "inconclusive"
    return {
        "status": status,
        "primary_horizon": PRIMARY_HORIZON,
        "minimum_group_n": min_group,
        "conditions": {
            "disciplined_yes_n": {"actual": yes_n, "required": min_group, "pass": yes_n >= min_group},
            "not_yes_n": {"actual": not_yes_n, "required": min_group, "pass": not_yes_n >= min_group},
            "yes_spy_excess_ci": {"actual": yes_ci, "supports": bool(yes_ci and yes_ci[0] > 0),
                                  "contradicts": bool(yes_ci and yes_ci[1] < 0)},
            "yes_minus_not_yes_ci": {
                "actual": contrast_ci,
                "supports": bool(contrast_ci and contrast_ci[0] > 0),
                "contradicts": bool(contrast_ci and contrast_ci[1] < 0),
            },
        },
    }


def build_event_study(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    horizons: Sequence[int] = (21, 63),
    min_inference_group: int = MIN_INFERENCE_GROUP,
) -> dict[str, Any]:
    symbol = str(replay["symbol"])
    calendar = list(replay["calendar"])
    calendar_index = {date: index for index, date in enumerate(calendar)}
    by_date, dates = _bar_maps(bars)
    horizon_results: dict[str, Any] = {}
    all_exclusions = list(replay.get("exclusions") or [])
    for horizon in horizons:
        events: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for record in replay.get("timeline") or []:
            if record.get("status") != "ok":
                continue
            event, exclusion = _event(
                record, symbol, int(horizon), calendar, calendar_index, by_date, dates
            )
            if event:
                events.append(event)
            if exclusion:
                exclusions.append(exclusion)
        inference = _inference(events, int(horizon))
        horizon_results[str(horizon)] = {
            "horizon": int(horizon),
            "descriptive_n": len(events),
            "degraded_n": sum(1 for event in events if event.get("degraded")),
            "inference_n": inference["sample_n"],
            "descriptive": {
                "all": _summary(events),
                "score_buckets": _grouped(events, "score_bucket"),
                "verdicts": _grouped(events, "verdict"),
                "rank_signal": _grouped(events, "rank_signal_group"),
                "disciplined_signal": _grouped(events, "disciplined_group"),
                "confirmation": _grouped(events, "confirmation_group"),
                "risk_gate": _grouped(events, "risk_gate_group"),
                "entry_timing": _grouped(events, "entry_timing"),
            },
            "inference": inference,
            "events": events,
            "exclusions": exclusions,
        }
        all_exclusions.extend(exclusions)
    primary = horizon_results.get(str(PRIMARY_HORIZON), {}).get("inference") or {}
    return {
        "symbol": symbol,
        "horizons": horizon_results,
        "exclusions": all_exclusions,
        "validation": _validation(primary, min_inference_group),
    }
