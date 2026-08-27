"""Pre-registered holdout for the pragmatic staged-entry candidate."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
from typing import Any, Mapping, Sequence

from .data import BacktestDataError, load_backtest_data
from .lifecycle_validation import write_validation_report
from .pragmatic import SCENARIO, probe_trigger, simulate_pragmatic_scenario
from .replay import replay_scores
from .simulation import build_benchmarks, simulate_managed_scenario


VALIDATION_SYMBOLS = (
    "AMD", "AVGO", "ORCL", "CRM", "ADBE", "NFLX", "DIS", "NKE",
    "BA", "GE", "HD", "LOW", "GS", "BAC", "CVX", "COP", "PEP",
    "MCD", "ABBV", "MRK", "TMO", "LIN", "UPS", "DE",
)
VALIDATION_START = "2025-01-02"
VALIDATION_END = "2026-07-10"
MIN_VALID_SYMBOLS = 18
MIN_DELAY_SAMPLES = 8
MIN_IMPROVEMENT_RATE_PCT = 60.0
MIN_CAPTURE_DELTA_PCT = 8.0
MIN_DELAY_IMPROVEMENT_SESSIONS = 2.0
MAX_MEDIAN_MDD_DEGRADATION_PCT = 2.0
MAX_WORST_MDD_DEGRADATION_PCT = 8.0
MAX_MEDIAN_EXPOSURE_DELTA_PCT = 25.0
MAX_MEDIAN_EXPOSURE_PCT = 60.0
MAX_MEDIAN_TURNOVER_RATIO = 3.0
MAX_ZERO_BASELINE_ANNUALIZED_SIDES = 12.0
MIN_WORST_RETURN_DELTA_PCT = -15.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _median(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return float(statistics.median(clean)) if clean else None


def _first_delay(
    timeline: Sequence[Mapping[str, Any]],
    calendar: Sequence[str],
    trades: Sequence[Mapping[str, Any]],
) -> tuple[int | None, str | None]:
    trigger = next((str(row["signal_date"]) for row in timeline if probe_trigger(row)), None)
    if trigger is None or trigger not in calendar:
        return None, None
    trigger_index = calendar.index(trigger)
    entries = sorted(
        str(trade["entry_date"])
        for trade in trades
        if trade.get("entry_date") in calendar and str(trade.get("entry_date")) > trigger
    )
    entry = entries[0] if entries else calendar[-1]
    return calendar.index(entry) - trigger_index, trigger


def _audit_transitions(candidate: Mapping[str, Any]) -> bool:
    transitions = candidate.get("state_transitions")
    if not isinstance(transitions, list):
        return False
    required = {"date", "from_state", "to_state", "reason"}
    if any(not isinstance(item, Mapping) or not required.issubset(item) for item in transitions):
        return False
    transition_trades = {
        str(item.get("trade_id")) for item in transitions
        if item.get("reason") in {"probe_executed", "standard_executed"}
    }
    actual_trades = {str(item.get("trade_id")) for item in candidate.get("trades") or []}
    return actual_trades.issubset(transition_trades)


def validation_row(
    symbol: str,
    replay: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    *,
    data_hash: str,
) -> dict[str, Any]:
    base = baseline["metrics"]
    cand = candidate["metrics"]
    buy_hold = benchmark["metrics"]
    base_return = float(base["total_return_pct"])
    candidate_return = float(cand["total_return_pct"])
    benchmark_return = float(buy_hold["total_return_pct"])
    capture_delta = (
        (candidate_return - base_return) / benchmark_return * 100
        if benchmark_return > 0 else None
    )
    baseline_delay, trigger = _first_delay(
        replay["timeline"], replay["calendar"], baseline.get("trades") or []
    )
    candidate_delay, _ = _first_delay(
        replay["timeline"], replay["calendar"], candidate.get("trades") or []
    )
    delay_improvement = (
        baseline_delay - candidate_delay
        if baseline_delay is not None and candidate_delay is not None else None
    )
    start = dt.date.fromisoformat(str(replay["calendar"][0]))
    end = dt.date.fromisoformat(str(replay["calendar"][-1]))
    years = max((end - start).days / 365.25, 1 / 252)
    baseline_sides = float(base["turnover_sides"])
    candidate_sides = float(cand["turnover_sides"])
    turnover_ratio = candidate_sides / baseline_sides if baseline_sides > 0 else None
    return {
        "symbol": symbol,
        "status": "ok",
        "data_hash": data_hash,
        "baseline_return_pct": base_return,
        "candidate_return_pct": candidate_return,
        "return_delta_pct": _round(candidate_return - base_return),
        "baseline_sharpe": base["sharpe_rf0"],
        "candidate_sharpe": cand["sharpe_rf0"],
        "sharpe_delta": _round(float(cand["sharpe_rf0"] or 0) - float(base["sharpe_rf0"] or 0)),
        "baseline_mdd_pct": base["max_drawdown_pct"],
        "candidate_mdd_pct": cand["max_drawdown_pct"],
        "mdd_delta_pct": _round(float(cand["max_drawdown_pct"]) - float(base["max_drawdown_pct"])),
        "benchmark_return_pct": benchmark_return,
        "positive_benchmark_capture_delta_pct": _round(capture_delta),
        "first_probe_trigger": trigger,
        "baseline_participation_delay_sessions": baseline_delay,
        "candidate_participation_delay_sessions": candidate_delay,
        "participation_delay_improvement_sessions": delay_improvement,
        "baseline_exposure_pct": base["exposure_pct"],
        "candidate_exposure_pct": cand["exposure_pct"],
        "exposure_delta_pct": _round(float(cand["exposure_pct"]) - float(base["exposure_pct"])),
        "baseline_turnover_sides": baseline_sides,
        "candidate_turnover_sides": candidate_sides,
        "turnover_ratio": _round(turnover_ratio),
        "candidate_annualized_turnover_sides": _round(candidate_sides / years),
        "candidate_cost_equity": cand["total_cost_equity"],
        "candidate_chase_violations": cand["chase_violations"],
        "candidate_trade_count": cand["trade_count"],
        "candidate_transition_count": cand["transition_count"],
        "state_audit_complete": _audit_transitions(candidate),
        "candidate": candidate,
    }


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "ok"]
    count = len(valid)
    delay_rows = [
        row for row in valid
        if isinstance(row.get("participation_delay_improvement_sessions"), (int, float))
    ]
    improvements = sum(float(row["return_delta_pct"]) > 0 for row in valid)
    improvement_rate = improvements / count * 100 if count else None
    return_delta = _median([float(row["return_delta_pct"]) for row in valid])
    sharpe_delta = _median([float(row["sharpe_delta"]) for row in valid])
    mdd_delta = _median([float(row["mdd_delta_pct"]) for row in valid])
    worst_mdd = min((float(row["mdd_delta_pct"]) for row in valid), default=None)
    capture_delta = _median([
        float(row["positive_benchmark_capture_delta_pct"])
        for row in valid
        if isinstance(row.get("positive_benchmark_capture_delta_pct"), (int, float))
    ])
    delay_delta = _median([
        float(row["participation_delay_improvement_sessions"]) for row in delay_rows
    ])
    exposure_delta = _median([float(row["exposure_delta_pct"]) for row in valid])
    exposure = _median([float(row["candidate_exposure_pct"]) for row in valid])
    turnover_ratio = _median([
        float(row["turnover_ratio"])
        for row in valid if isinstance(row.get("turnover_ratio"), (int, float))
    ])
    zero_base_turnover = max((
        float(row["candidate_annualized_turnover_sides"])
        for row in valid if float(row["baseline_turnover_sides"]) == 0
    ), default=0.0)
    worst_return = min((float(row["return_delta_pct"]) for row in valid), default=None)
    chase_violations = sum(int(row["candidate_chase_violations"]) for row in valid)
    conditions = {
        "valid_symbols": {"actual": count, "required_min": MIN_VALID_SYMBOLS, "pass": count >= MIN_VALID_SYMBOLS},
        "delay_samples": {"actual": len(delay_rows), "required_min": MIN_DELAY_SAMPLES, "pass": len(delay_rows) >= MIN_DELAY_SAMPLES},
        "return_improvement_rate_pct": {"actual": _round(improvement_rate, 2), "required_min": MIN_IMPROVEMENT_RATE_PCT, "pass": improvement_rate is not None and improvement_rate >= MIN_IMPROVEMENT_RATE_PCT},
        "median_return_delta_pct": {"actual": _round(return_delta), "required_min_exclusive": 0.0, "pass": return_delta is not None and return_delta > 0},
        "median_sharpe_delta": {"actual": _round(sharpe_delta), "required_min": 0.0, "pass": sharpe_delta is not None and sharpe_delta >= 0},
        "median_mdd_delta_pct": {"actual": _round(mdd_delta), "required_min": -MAX_MEDIAN_MDD_DEGRADATION_PCT, "pass": mdd_delta is not None and mdd_delta >= -MAX_MEDIAN_MDD_DEGRADATION_PCT},
        "worst_mdd_delta_pct": {"actual": _round(worst_mdd), "required_min": -MAX_WORST_MDD_DEGRADATION_PCT, "pass": worst_mdd is not None and worst_mdd >= -MAX_WORST_MDD_DEGRADATION_PCT},
        "median_positive_benchmark_capture_delta_pct": {"actual": _round(capture_delta), "required_min": MIN_CAPTURE_DELTA_PCT, "pass": capture_delta is not None and capture_delta >= MIN_CAPTURE_DELTA_PCT},
        "median_participation_delay_improvement_sessions": {"actual": _round(delay_delta), "required_min": MIN_DELAY_IMPROVEMENT_SESSIONS, "pass": delay_delta is not None and delay_delta >= MIN_DELAY_IMPROVEMENT_SESSIONS},
        "chase_violations": {"actual": chase_violations, "required": 0, "pass": chase_violations == 0},
        "median_exposure_delta_pct": {"actual": _round(exposure_delta), "required_max": MAX_MEDIAN_EXPOSURE_DELTA_PCT, "pass": exposure_delta is not None and exposure_delta <= MAX_MEDIAN_EXPOSURE_DELTA_PCT},
        "median_candidate_exposure_pct": {"actual": _round(exposure), "required_max": MAX_MEDIAN_EXPOSURE_PCT, "pass": exposure is not None and exposure <= MAX_MEDIAN_EXPOSURE_PCT},
        "median_turnover_ratio": {"actual": _round(turnover_ratio), "required_max": MAX_MEDIAN_TURNOVER_RATIO, "pass": turnover_ratio is None or turnover_ratio <= MAX_MEDIAN_TURNOVER_RATIO},
        "zero_baseline_max_annualized_turnover_sides": {"actual": _round(zero_base_turnover), "required_max": MAX_ZERO_BASELINE_ANNUALIZED_SIDES, "pass": zero_base_turnover <= MAX_ZERO_BASELINE_ANNUALIZED_SIDES},
        "worst_return_delta_pct": {"actual": _round(worst_return), "required_min": MIN_WORST_RETURN_DELTA_PCT, "pass": worst_return is not None and worst_return >= MIN_WORST_RETURN_DELTA_PCT},
        "state_audit_complete": {"actual": sum(bool(row["state_audit_complete"]) for row in valid), "required": count, "pass": count > 0 and all(bool(row["state_audit_complete"]) for row in valid)},
    }
    return {
        "decision": "promote" if all(item["pass"] for item in conditions.values()) else "reject",
        "conditions": conditions,
        "valid_symbols": count,
        "excluded_symbols": len(rows) - count,
    }


def semantic_hash(report: Mapping[str, Any]) -> str:
    payload = dict(report)
    payload.pop("generated_at", None)
    payload.pop("semantic_hash", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_validation(*, cache_db: str | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in VALIDATION_SYMBOLS:
        try:
            bundle = load_backtest_data(
                [symbol, "SPY", "QQQ"], VALIDATION_START, VALIDATION_END,
                mode="cache-only", cache_db=cache_db,
            )
            replay = replay_scores(bundle, symbol)
            baseline = simulate_managed_scenario(
                replay, bundle.bars, policy="disciplined_verdict", cost_bps=10.0
            )
            candidate = simulate_pragmatic_scenario(replay, bundle.bars, cost_bps=10.0)
            benchmarks = build_benchmarks(replay, bundle.bars)
            rows.append(validation_row(
                symbol, replay, baseline, candidate, benchmarks[symbol],
                data_hash=bundle.data_hash,
            ))
        except BacktestDataError as exc:
            rows.append({
                "symbol": symbol, "status": "excluded",
                "error": exc.code, "reason": exc.message,
            })
    report: dict[str, Any] = {
        "report_type": "pragmatic_staged_entry_holdout",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "frozen_design": {
            "symbols": list(VALIDATION_SYMBOLS),
            "validation_start": VALIDATION_START,
            "validation_end": VALIDATION_END,
            "data_source": "cache-only",
            "feed": "iex",
            "adjustment": "split",
            "warmup_sessions": 260,
            "cost_bps_oneway": 10.0,
            "execution": "next_open",
            "baseline": "disciplined_verdict_managed",
            "candidate": SCENARIO,
        },
        "rows": rows,
        "summary": summarize_rows(rows),
    }
    report["semantic_hash"] = semantic_hash(report)
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


__all__ = [
    "run_validation", "semantic_hash", "summarize_rows", "validation_row",
    "write_validation_report",
]
