"""Second independent holdout for pragmatic staged-entry v1."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .data import BacktestDataError, load_backtest_data
from .lifecycle_validation import write_validation_report
from .pragmatic import V1_CONFIG, simulate_pragmatic_scenario
from .pragmatic_validation import semantic_hash, summarize_rows, validation_row
from .replay import replay_scores
from .simulation import build_benchmarks, simulate_managed_scenario


VALIDATION_SYMBOLS = (
    "TXN", "QCOM", "AMAT", "MU", "INTU", "NOW", "IBM", "CSCO",
    "PANW", "LRCX", "KLAC", "SNPS", "CDNS", "BKNG", "SBUX", "TGT",
    "TJX", "F", "GM", "LLY", "PFE", "BMY", "GILD", "MDT",
)
VALIDATION_START = "2025-01-02"
VALIDATION_END = "2026-07-10"


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
            candidate = simulate_pragmatic_scenario(
                replay, bundle.bars, cost_bps=10.0, config=V1_CONFIG
            )
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
        "report_type": "pragmatic_staged_entry_v1_holdout",
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
            "candidate": V1_CONFIG.name,
            "candidate_parameters": {
                "probe_allocation_pct": V1_CONFIG.probe_allocation_pct,
                "confirmed_allocation_pct": V1_CONFIG.confirmed_allocation_pct,
                "standard_allocation_cap_pct": V1_CONFIG.standard_allocation_cap_pct,
                "exit_unconfirmed_probe": V1_CONFIG.exit_unconfirmed_probe,
                "defer_trend_break_until_qualified": V1_CONFIG.defer_trend_break_until_qualified,
            },
        },
        "rows": rows,
        "summary": summarize_rows(rows),
    }
    report["semantic_hash"] = semantic_hash(report)
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


__all__ = ["run_validation", "write_validation_report"]
