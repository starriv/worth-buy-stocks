"""Pre-registered cache-only holdout for the stateful lifecycle experiment."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact import build_artifact
from .contracts import ARTIFACT_VERSION, CONTRACT_VERSION, BacktestConfig
from .data import BacktestDataError, load_backtest_data


VALIDATION_SYMBOLS = (
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    "JPM", "XOM", "UNH", "CAT", "WMT", "COST", "JNJ", "KO",
)
VALIDATION_START = "2025-04-04"
VALIDATION_END = "2026-07-09"
DEVELOPMENT_CUTOFF = "2025-01-02"
PURGE_SESSIONS = 63
MIN_VALID_SYMBOLS = 12
MIN_IMPROVEMENT_RATE_PCT = 60.0
MIN_CAPTURE_DELTA_PCT = 10.0
MAX_MEDIAN_MDD_DEGRADATION_PCT = 3.0
MAX_SEVERE_MDD_DEGRADATION_PCT = 10.0
MAX_SEVERE_MDD_RATE_PCT = 20.0
MAX_MEDIAN_TURNOVER_RATIO = 2.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _median(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return float(statistics.median(clean)) if clean else None


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the frozen promotion gates to per-symbol validation rows."""

    valid = [row for row in rows if row.get("status") == "ok"]
    count = len(valid)
    improvements = sum(float(row["return_delta_pct"]) > 0 for row in valid)
    improvement_rate = improvements / count * 100 if count else None
    return_delta = _median([float(row["return_delta_pct"]) for row in valid])
    sharpe_delta = _median([float(row["sharpe_delta"]) for row in valid])
    capture_delta = _median([
        float(row["capture_delta_pct"])
        for row in valid if isinstance(row.get("capture_delta_pct"), (int, float))
    ])
    mdd_delta = _median([float(row["mdd_delta_pct"]) for row in valid])
    severe_mdd = sum(
        float(row["mdd_delta_pct"]) < -MAX_SEVERE_MDD_DEGRADATION_PCT
        for row in valid
    )
    severe_mdd_rate = severe_mdd / count * 100 if count else None
    turnover_ratio = _median([
        float(row["turnover_ratio"])
        for row in valid if isinstance(row.get("turnover_ratio"), (int, float))
    ])
    conditions = {
        "valid_symbols": {
            "actual": count, "required": MIN_VALID_SYMBOLS,
            "pass": count >= MIN_VALID_SYMBOLS,
        },
        "return_improvement_rate_pct": {
            "actual": _round(improvement_rate, 2), "required_min": MIN_IMPROVEMENT_RATE_PCT,
            "pass": improvement_rate is not None and improvement_rate >= MIN_IMPROVEMENT_RATE_PCT,
        },
        "median_return_delta_pct": {
            "actual": _round(return_delta), "required_min_exclusive": 0.0,
            "pass": return_delta is not None and return_delta > 0,
        },
        "median_sharpe_delta": {
            "actual": _round(sharpe_delta), "required_min": 0.0,
            "pass": sharpe_delta is not None and sharpe_delta >= 0,
        },
        "median_positive_benchmark_capture_delta_pct": {
            "actual": _round(capture_delta), "required_min": MIN_CAPTURE_DELTA_PCT,
            "pass": capture_delta is not None and capture_delta >= MIN_CAPTURE_DELTA_PCT,
        },
        "median_mdd_delta_pct": {
            "actual": _round(mdd_delta),
            "required_min": -MAX_MEDIAN_MDD_DEGRADATION_PCT,
            "pass": mdd_delta is not None and mdd_delta >= -MAX_MEDIAN_MDD_DEGRADATION_PCT,
        },
        "severe_mdd_degradation_rate_pct": {
            "actual": _round(severe_mdd_rate, 2),
            "required_max": MAX_SEVERE_MDD_RATE_PCT,
            "pass": severe_mdd_rate is not None and severe_mdd_rate <= MAX_SEVERE_MDD_RATE_PCT,
        },
        "median_turnover_ratio": {
            "actual": _round(turnover_ratio), "required_max": MAX_MEDIAN_TURNOVER_RATIO,
            "pass": turnover_ratio is not None and turnover_ratio <= MAX_MEDIAN_TURNOVER_RATIO,
        },
    }
    passed = all(item["pass"] for item in conditions.values())
    return {
        "decision": "promote" if passed else "reject",
        "conditions": conditions,
        "valid_symbols": count,
        "excluded_symbols": len(rows) - count,
    }


def _validation_row(symbol: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    baseline = artifact["simulations"]["disciplined_verdict_managed"]["metrics"]
    stateful = artifact["simulations"]["disciplined_verdict_stateful"]["metrics"]
    benchmark = artifact["benchmarks"][symbol]["metrics"]
    base_return = float(baseline["total_return_pct"])
    state_return = float(stateful["total_return_pct"])
    benchmark_return = float(benchmark["total_return_pct"])
    capture_delta = None
    if benchmark_return > 0:
        capture_delta = (state_return - base_return) / benchmark_return * 100.0
    base_turnover = float(baseline["turnover_sides"])
    state_turnover = float(stateful["turnover_sides"])
    turnover_ratio = (
        state_turnover / base_turnover
        if base_turnover > 0 else 1.0 if state_turnover == 0 else None
    )
    return {
        "symbol": symbol,
        "status": "ok",
        "data_hash": artifact["provenance"]["data_hash"],
        "semantic_hash": artifact["semantic_hash"],
        "baseline_return_pct": base_return,
        "stateful_return_pct": state_return,
        "return_delta_pct": _round(state_return - base_return),
        "baseline_sharpe": baseline["sharpe_rf0"],
        "stateful_sharpe": stateful["sharpe_rf0"],
        "sharpe_delta": _round(float(stateful["sharpe_rf0"] or 0) - float(baseline["sharpe_rf0"] or 0)),
        "baseline_mdd_pct": baseline["max_drawdown_pct"],
        "stateful_mdd_pct": stateful["max_drawdown_pct"],
        "mdd_delta_pct": _round(float(stateful["max_drawdown_pct"]) - float(baseline["max_drawdown_pct"])),
        "benchmark_return_pct": benchmark_return,
        "capture_delta_pct": _round(capture_delta),
        "baseline_exposure_pct": baseline["exposure_pct"],
        "stateful_exposure_pct": stateful["exposure_pct"],
        "baseline_turnover_sides": baseline["turnover_sides"],
        "stateful_turnover_sides": stateful["turnover_sides"],
        "turnover_ratio": _round(turnover_ratio),
        "stateful_transitions": len(
            artifact["simulations"]["disciplined_verdict_stateful"].get("state_transitions") or []
        ),
    }


def validation_semantic_hash(report: Mapping[str, Any]) -> str:
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
            artifact = build_artifact(
                BacktestConfig(
                    symbol=symbol,
                    start=VALIDATION_START,
                    end=VALIDATION_END,
                    data_source="cache-only",
                ),
                bundle,
            )
            rows.append(_validation_row(symbol, artifact))
        except BacktestDataError as exc:
            rows.append({
                "symbol": symbol, "status": "excluded",
                "error": exc.code, "reason": exc.message,
            })
    report: dict[str, Any] = {
        "report_type": "stateful_lifecycle_holdout",
        "contract_version": CONTRACT_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "frozen_design": {
            "symbols": list(VALIDATION_SYMBOLS),
            "validation_start": VALIDATION_START,
            "validation_end": VALIDATION_END,
            "development_cutoff": DEVELOPMENT_CUTOFF,
            "purge_sessions": PURGE_SESSIONS,
            "data_source": "cache-only",
            "cost_bps_oneway": 10.0,
            "baseline": "disciplined_verdict_managed",
            "candidate": "disciplined_verdict_stateful",
        },
        "rows": rows,
        "summary": summarize_rows(rows),
    }
    report["semantic_hash"] = validation_semantic_hash(report)
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


def write_validation_report(path: str | os.PathLike[str], report: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
