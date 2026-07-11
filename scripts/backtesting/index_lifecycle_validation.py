"""Pre-registered cache-only holdout for the index-ETF lifecycle hypothesis."""
from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any, Mapping, Sequence

from .artifact import build_artifact
from .contracts import ARTIFACT_VERSION, CONTRACT_VERSION, BacktestConfig
from .data import BacktestDataError, load_backtest_data
from .lifecycle_validation import (
    _validation_row,
    validation_semantic_hash,
    write_validation_report,
)


BROAD_ETF_SYMBOLS = ("IWM", "DIA", "MDY", "RSP")
SECTOR_ETF_SYMBOLS = (
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP",
    "XLY", "XLU", "XLB", "XLRE", "XLC",
)
VALIDATION_SYMBOLS = BROAD_ETF_SYMBOLS + SECTOR_ETF_SYMBOLS
VALIDATION_START = "2025-04-04"
VALIDATION_END = "2026-07-09"
DEVELOPMENT_CUTOFF = "2025-01-02"
PURGE_SESSIONS = 63
MIN_VALID_SYMBOLS = 12
MIN_BROAD_SYMBOLS = 3
MIN_SECTOR_SYMBOLS = 8
MIN_OVERALL_IMPROVEMENT_RATE_PCT = 60.0
MIN_BROAD_IMPROVEMENT_RATE_PCT = 50.0
MIN_SECTOR_IMPROVEMENT_RATE_PCT = 60.0
MIN_CAPTURE_DELTA_PCT = 10.0
MAX_MEDIAN_MDD_DEGRADATION_PCT = 3.0
MAX_SEVERE_MDD_DEGRADATION_PCT = 10.0
MAX_SEVERE_MDD_RATE_PCT = 20.0
MAX_MEDIAN_TURNOVER_RATIO = 2.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _median(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [
        float(row[key]) for row in rows
        if isinstance(row.get(key), (int, float)) and math.isfinite(float(row[key]))
    ]
    return float(statistics.median(values)) if values else None


def _improvement_rate(rows: Sequence[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum(float(row["return_delta_pct"]) > 0 for row in rows) / len(rows) * 100.0


def _minimum_condition(actual: float | int, required: float | int) -> dict[str, Any]:
    return {"actual": actual, "required": required, "pass": actual >= required}


def _rate_condition(
    rows: Sequence[Mapping[str, Any]], required: float,
) -> dict[str, Any]:
    actual = _improvement_rate(rows)
    return {
        "actual": _round(actual, 2),
        "required_min": required,
        "pass": actual is not None and actual >= required,
    }


def _median_min_condition(
    rows: Sequence[Mapping[str, Any]], key: str, required: float, *, exclusive: bool = False,
) -> dict[str, Any]:
    actual = _median(rows, key)
    passed = actual is not None and (actual > required if exclusive else actual >= required)
    required_key = "required_min_exclusive" if exclusive else "required_min"
    return {"actual": _round(actual), required_key: required, "pass": passed}


def summarize_index_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the frozen overall and ETF-stratum promotion gates."""

    valid = [row for row in rows if row.get("status") == "ok"]
    broad = [row for row in valid if row.get("group") == "broad"]
    sector = [row for row in valid if row.get("group") == "sector"]
    positive_benchmark = [
        row for row in valid
        if isinstance(row.get("capture_delta_pct"), (int, float))
    ]
    severe_count = sum(
        float(row["mdd_delta_pct"]) < -MAX_SEVERE_MDD_DEGRADATION_PCT
        for row in valid
    )
    severe_rate = severe_count / len(valid) * 100.0 if valid else None
    median_turnover = _median(valid, "turnover_ratio")

    conditions = {
        "valid_symbols": _minimum_condition(len(valid), MIN_VALID_SYMBOLS),
        "broad_valid_symbols": _minimum_condition(len(broad), MIN_BROAD_SYMBOLS),
        "sector_valid_symbols": _minimum_condition(len(sector), MIN_SECTOR_SYMBOLS),
        "overall_return_improvement_rate_pct": _rate_condition(
            valid, MIN_OVERALL_IMPROVEMENT_RATE_PCT
        ),
        "broad_return_improvement_rate_pct": _rate_condition(
            broad, MIN_BROAD_IMPROVEMENT_RATE_PCT
        ),
        "sector_return_improvement_rate_pct": _rate_condition(
            sector, MIN_SECTOR_IMPROVEMENT_RATE_PCT
        ),
        "overall_median_return_delta_pct": _median_min_condition(
            valid, "return_delta_pct", 0.0, exclusive=True
        ),
        "broad_median_return_delta_pct": _median_min_condition(
            broad, "return_delta_pct", 0.0, exclusive=True
        ),
        "sector_median_return_delta_pct": _median_min_condition(
            sector, "return_delta_pct", 0.0, exclusive=True
        ),
        "overall_median_sharpe_delta": _median_min_condition(
            valid, "sharpe_delta", 0.0
        ),
        "broad_median_sharpe_delta": _median_min_condition(
            broad, "sharpe_delta", 0.0
        ),
        "sector_median_sharpe_delta": _median_min_condition(
            sector, "sharpe_delta", 0.0
        ),
        "median_positive_benchmark_capture_delta_pct": _median_min_condition(
            positive_benchmark, "capture_delta_pct", MIN_CAPTURE_DELTA_PCT
        ),
        "median_mdd_delta_pct": _median_min_condition(
            valid, "mdd_delta_pct", -MAX_MEDIAN_MDD_DEGRADATION_PCT
        ),
        "severe_mdd_degradation_rate_pct": {
            "actual": _round(severe_rate, 2),
            "required_max": MAX_SEVERE_MDD_RATE_PCT,
            "pass": severe_rate is not None and severe_rate <= MAX_SEVERE_MDD_RATE_PCT,
        },
        "median_turnover_ratio": {
            "actual": _round(median_turnover),
            "required_max": MAX_MEDIAN_TURNOVER_RATIO,
            "pass": median_turnover is not None and median_turnover <= MAX_MEDIAN_TURNOVER_RATIO,
        },
    }
    passed = all(condition["pass"] for condition in conditions.values())
    return {
        "decision": "promote" if passed else "reject",
        "conditions": conditions,
        "valid_symbols": len(valid),
        "broad_valid_symbols": len(broad),
        "sector_valid_symbols": len(sector),
        "excluded_symbols": len(rows) - len(valid),
    }


def run_validation(*, cache_db: str | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in VALIDATION_SYMBOLS:
        group = "broad" if symbol in BROAD_ETF_SYMBOLS else "sector"
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
            row = _validation_row(symbol, artifact)
            row["group"] = group
            rows.append(row)
        except BacktestDataError as exc:
            rows.append({
                "symbol": symbol,
                "group": group,
                "status": "excluded",
                "error": exc.code,
                "reason": exc.message,
            })

    report: dict[str, Any] = {
        "report_type": "index_etf_stateful_lifecycle_holdout",
        "contract_version": CONTRACT_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "frozen_design": {
            "broad_symbols": list(BROAD_ETF_SYMBOLS),
            "sector_symbols": list(SECTOR_ETF_SYMBOLS),
            "excluded_development_symbols": ["QQQ", "SPY"],
            "validation_start": VALIDATION_START,
            "validation_end": VALIDATION_END,
            "development_cutoff": DEVELOPMENT_CUTOFF,
            "purge_sessions": PURGE_SESSIONS,
            "data_source": "cache-only",
            "feed": "iex",
            "adjustment": "split",
            "cost_bps_oneway": 10.0,
            "baseline": "disciplined_verdict_managed",
            "candidate": "disciplined_verdict_stateful",
        },
        "rows": rows,
        "summary": summarize_index_rows(rows),
    }
    report["semantic_hash"] = validation_semantic_hash(report)
    return report


__all__ = [
    "BROAD_ETF_SYMBOLS",
    "SECTOR_ETF_SYMBOLS",
    "VALIDATION_SYMBOLS",
    "run_validation",
    "summarize_index_rows",
    "validation_semantic_hash",
    "write_validation_report",
]
