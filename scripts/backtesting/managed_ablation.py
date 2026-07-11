"""Research-only managed-scenario ablation study.

Runs the frozen managed baseline plus a pre-registered grid of ablation
variants on one shared point-in-time replay per symbol, so every variant
consumes exactly the same production scores. The three frozen axes come from
the 2026-07 NVDA/AAPL/META diagnosis:

- ``A`` hold_through_heat: ignore the overextended-reversal position exit
  (initial stop, break-even, trailing stop and trend-break exits stay live);
- ``B`` skip_pullback_entry: drop pullback-reversal entries, keeping trend
  continuation and the capped recovery path;
- ``C`` full_allocation: invest the full equity per signal instead of the
  suggested fraction (recovery-reversal entries keep their capped trial size).

This module never touches the production scoring pipeline and its output is
labelled research-only: promotion still requires the pre-registered holdout
gates from :mod:`backtesting.lifecycle_validation`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from .data import BacktestDataError, load_backtest_data
from .lifecycle_validation import summarize_rows, write_validation_report
from .replay import replay_scores
from .simulation import (
    ManagedVariant,
    build_benchmarks,
    simulate_managed_scenario,
    simulate_managed_variant,
)


ABLATION_VARIANTS: tuple[ManagedVariant, ...] = (
    ManagedVariant(
        name="hold_through_heat",
        ignored_exit_classes=frozenset({"overextended_reversal"}),
    ),
    ManagedVariant(
        name="skip_pullback_entry",
        excluded_entry_timings=frozenset({"pullback_reversal"}),
    ),
    ManagedVariant(name="full_allocation", allocation_override_pct=100.0),
    ManagedVariant(
        name="heat_and_entry",
        ignored_exit_classes=frozenset({"overextended_reversal"}),
        excluded_entry_timings=frozenset({"pullback_reversal"}),
    ),
    ManagedVariant(
        name="heat_and_alloc",
        ignored_exit_classes=frozenset({"overextended_reversal"}),
        allocation_override_pct=100.0,
    ),
    ManagedVariant(
        name="entry_and_alloc",
        excluded_entry_timings=frozenset({"pullback_reversal"}),
        allocation_override_pct=100.0,
    ),
    ManagedVariant(
        name="heat_entry_alloc",
        ignored_exit_classes=frozenset({"overextended_reversal"}),
        excluded_entry_timings=frozenset({"pullback_reversal"}),
        allocation_override_pct=100.0,
    ),
)
BASELINE_SCENARIO = "disciplined_verdict_managed"


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _delta_row(
    symbol: str,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    benchmark_return: float | None,
) -> dict[str, Any]:
    """Build a per-symbol comparison row in the frozen holdout-row shape."""

    base = baseline["metrics"]
    cand = candidate["metrics"]
    base_return = float(base["total_return_pct"] or 0.0)
    cand_return = float(cand["total_return_pct"] or 0.0)
    capture_delta = None
    if isinstance(benchmark_return, (int, float)) and benchmark_return > 0:
        capture_delta = (cand_return - base_return) / benchmark_return * 100.0
    base_turnover = float(base["turnover_sides"])
    cand_turnover = float(cand["turnover_sides"])
    turnover_ratio = (
        cand_turnover / base_turnover
        if base_turnover > 0 else 1.0 if cand_turnover == 0 else None
    )
    return {
        "symbol": symbol,
        "status": "ok",
        "baseline_return_pct": _round(base_return),
        "candidate_return_pct": _round(cand_return),
        "return_delta_pct": _round(cand_return - base_return),
        "baseline_sharpe": base["sharpe_rf0"],
        "candidate_sharpe": cand["sharpe_rf0"],
        "sharpe_delta": _round(
            float(cand["sharpe_rf0"] or 0.0) - float(base["sharpe_rf0"] or 0.0)
        ),
        "baseline_mdd_pct": base["max_drawdown_pct"],
        "candidate_mdd_pct": cand["max_drawdown_pct"],
        "mdd_delta_pct": _round(
            float(cand["max_drawdown_pct"] or 0.0) - float(base["max_drawdown_pct"] or 0.0)
        ),
        "benchmark_return_pct": _round(benchmark_return),
        "capture_delta_pct": _round(capture_delta),
        "baseline_exposure_pct": base["exposure_pct"],
        "candidate_exposure_pct": cand["exposure_pct"],
        "baseline_trades": base["trade_count"],
        "candidate_trades": cand["trade_count"],
        "turnover_ratio": _round(turnover_ratio),
    }


def run_ablation(
    symbols: Sequence[str],
    start: str,
    end: str,
    *,
    cost_bps: float = 10.0,
    cache_db: str | None = None,
    variants: Sequence[ManagedVariant] = ABLATION_VARIANTS,
) -> dict[str, Any]:
    """Run baseline + variants per symbol on one shared replay each."""

    per_symbol: list[dict[str, Any]] = []
    rows_by_variant: dict[str, list[dict[str, Any]]] = {
        variant.name: [] for variant in variants
    }
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        try:
            bundle = load_backtest_data(
                [symbol, "SPY", "QQQ"], start, end,
                mode="cache-only", cache_db=cache_db,
            )
        except BacktestDataError as exc:
            entry = {"symbol": symbol, "status": "excluded", **exc.as_dict()}
            per_symbol.append(entry)
            for variant in variants:
                rows_by_variant[variant.name].append({
                    "symbol": symbol, "status": "excluded",
                    "error": exc.code, "reason": exc.message,
                })
            continue
        replay = replay_scores(bundle, symbol)
        baseline = simulate_managed_scenario(
            replay, bundle.bars, policy="disciplined_verdict", cost_bps=cost_bps
        )
        benchmarks = build_benchmarks(replay, bundle.bars)
        benchmark_return = (
            benchmarks.get(symbol, {}).get("metrics", {}).get("total_return_pct")
        )
        scenarios: dict[str, Any] = {}
        for variant in variants:
            candidate = simulate_managed_variant(
                replay, bundle.bars,
                policy="disciplined_verdict", cost_bps=cost_bps, variant=variant,
            )
            row = _delta_row(symbol, baseline, candidate, benchmark_return)
            rows_by_variant[variant.name].append(row)
            scenarios[variant.name] = {
                "metrics": candidate["metrics"],
                "delta": row,
            }
        per_symbol.append({
            "symbol": symbol,
            "status": "ok",
            "actual_start": bundle.actual_start,
            "actual_end": bundle.actual_end,
            "data_hash": bundle.data_hash,
            "baseline": {"metrics": baseline["metrics"]},
            "benchmark_return_pct": _round(benchmark_return),
            "spy_return_pct": _round(
                benchmarks.get("SPY", {}).get("metrics", {}).get("total_return_pct")
            ),
            "variants": scenarios,
        })
    variant_summaries = {
        variant.name: {
            "definition": {
                "ignored_exit_classes": sorted(variant.ignored_exit_classes),
                "excluded_entry_timings": sorted(variant.excluded_entry_timings),
                "allocation_override_pct": variant.allocation_override_pct,
            },
            "gates": summarize_rows(rows_by_variant[variant.name]),
            "rows": rows_by_variant[variant.name],
        }
        for variant in variants
    }
    report: dict[str, Any] = {
        "report_type": "managed_ablation_research",
        "lifecycle": "research_only_not_promoted",
        "baseline": BASELINE_SCENARIO,
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "design": {
            "symbols": [str(symbol).strip().upper() for symbol in symbols],
            "start": start,
            "end": end,
            "cost_bps_oneway": cost_bps,
            "data_source": "cache-only",
        },
        "per_symbol": per_symbol,
        "variants": variant_summaries,
    }
    payload = dict(report)
    payload.pop("generated_at")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    report["semantic_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


def _graduated_overheat(record: dict[str, Any]) -> None:
    """Overheated strong-trend days enter at half size instead of waiting."""

    if record.get("base_verdict") != "是" or record.get("verdict") == "是":
        return
    timing = (record.get("entry_timing") or {}).get("classification")
    if timing != "overextended":
        return
    record["verdict"] = "是"
    position = record.get("suggested_position_pct")
    record["suggested_position_pct"] = (
        round(float(position) * 0.5, 1)
        if isinstance(position, (int, float)) and not isinstance(position, bool)
        else 25.0
    )


def _confirm_relax_2of3(record: dict[str, Any]) -> None:
    """Score-qualified days pass with 2-of-3 confirmations when gates are clear."""

    if record.get("base_verdict") != "观察" or record.get("verdict") == "是":
        return
    composite = record.get("composite")
    if not isinstance(composite, (int, float)) or composite < 75:
        return
    if record.get("risk_gates"):
        return
    timing = (record.get("entry_timing") or {}).get("classification")
    if timing in ("overextended", "trend_broken"):
        return
    confirmation = record.get("confirmation") or {}
    minimums = confirmation.get("minimums_pct") or {}
    passed = sum(
        1
        for value_key, min_key in (
            ("technical_pct", "technical"),
            ("volume_pct", "volume"),
            ("trend_quality_pct", "trend_quality"),
        )
        if isinstance(confirmation.get(value_key), (int, float))
        and isinstance(minimums.get(min_key), (int, float))
        and confirmation[value_key] >= minimums[min_key]
    )
    if passed >= 2:
        record["verdict"] = "是"


def _rs_floor_50(record: dict[str, Any]) -> None:
    """Demote entries whose relative-strength sub-score is below 50."""

    if record.get("verdict") != "是":
        return
    breakdown = record.get("factor_breakdown") or {}
    rs = (breakdown.get("rel_strength") or {}).get("score_pct")
    if isinstance(rs, (int, float)) and rs < 50:
        record["verdict"] = "观察"


SIGNAL_VARIANTS: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("graduated_overheat", (_graduated_overheat,)),
    ("confirm_relax_2of3", (_confirm_relax_2of3,)),
    ("rs_floor_50", (_rs_floor_50,)),
    ("overheat_and_rs", (_graduated_overheat, _rs_floor_50)),
    ("overheat_relax_rs", (_graduated_overheat, _confirm_relax_2of3, _rs_floor_50)),
)


def _transform_replay(replay: Mapping[str, Any], transforms: tuple[Any, ...]) -> dict[str, Any]:
    output = dict(replay)
    timeline = []
    for record in replay.get("timeline") or []:
        clone = json.loads(json.dumps(record))
        if clone.get("status") == "ok":
            for transform in transforms:
                transform(clone)
        timeline.append(clone)
    output["timeline"] = timeline
    return output


def run_signal_ablation(
    symbols: Sequence[str],
    start: str,
    end: str,
    *,
    cost_bps: float = 10.0,
    cache_db: str | None = None,
) -> dict[str, Any]:
    """Signal-layer ablations: transform the replay timeline, keep execution frozen."""

    per_symbol: list[dict[str, Any]] = []
    rows_by_variant: dict[str, list[dict[str, Any]]] = {
        name: [] for name, _ in SIGNAL_VARIANTS
    }
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        try:
            bundle = load_backtest_data(
                [symbol, "SPY", "QQQ"], start, end,
                mode="cache-only", cache_db=cache_db,
            )
        except BacktestDataError as exc:
            per_symbol.append({"symbol": symbol, "status": "excluded", **exc.as_dict()})
            for name, _ in SIGNAL_VARIANTS:
                rows_by_variant[name].append({
                    "symbol": symbol, "status": "excluded",
                    "error": exc.code, "reason": exc.message,
                })
            continue
        replay = replay_scores(bundle, symbol)
        baseline = simulate_managed_scenario(
            replay, bundle.bars, policy="disciplined_verdict", cost_bps=cost_bps
        )
        benchmarks = build_benchmarks(replay, bundle.bars)
        benchmark_return = (
            benchmarks.get(symbol, {}).get("metrics", {}).get("total_return_pct")
        )
        scenarios: dict[str, Any] = {}
        for name, transforms in SIGNAL_VARIANTS:
            candidate = simulate_managed_scenario(
                _transform_replay(replay, transforms), bundle.bars,
                policy="disciplined_verdict", cost_bps=cost_bps,
            )
            row = _delta_row(symbol, baseline, candidate, benchmark_return)
            rows_by_variant[name].append(row)
            scenarios[name] = {"metrics": candidate["metrics"], "delta": row}
        per_symbol.append({
            "symbol": symbol,
            "status": "ok",
            "actual_start": bundle.actual_start,
            "actual_end": bundle.actual_end,
            "data_hash": bundle.data_hash,
            "baseline": {"metrics": baseline["metrics"]},
            "benchmark_return_pct": _round(benchmark_return),
            "variants": scenarios,
        })
    report: dict[str, Any] = {
        "report_type": "signal_ablation_research",
        "lifecycle": "research_only_not_promoted",
        "baseline": BASELINE_SCENARIO,
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "design": {
            "symbols": [str(symbol).strip().upper() for symbol in symbols],
            "start": start,
            "end": end,
            "cost_bps_oneway": cost_bps,
            "data_source": "cache-only",
            "variants": [name for name, _ in SIGNAL_VARIANTS],
        },
        "per_symbol": per_symbol,
        "variants": {
            name: {
                "gates": summarize_rows(rows_by_variant[name]),
                "rows": rows_by_variant[name],
            }
            for name, _ in SIGNAL_VARIANTS
        },
    }
    payload = dict(report)
    payload.pop("generated_at")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    report["semantic_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


__all__ = [
    "ABLATION_VARIANTS",
    "SIGNAL_VARIANTS",
    "ManagedVariant",
    "run_ablation",
    "run_signal_ablation",
    "write_validation_report",
]
