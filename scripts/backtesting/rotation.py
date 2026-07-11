"""Research-only cross-sectional Top-N rotation portfolio study.

Pre-registered design (frozen before the first run):

- Signal grid: every 21 sessions from the shared 260-session warmup; the
  63-session configs take every third grid date so both cadences share one
  score computation per date.
- Configs: rebalance step {21, 63} x top-N {10, 20}, equal weight, ranked by
  the production cross-sectional ``composite`` (descending).
- Regime filter: SPY close above its 200-session average on the signal date,
  otherwise the portfolio goes fully to cash at the next open.
- Execution: next-open rebalance, 10 bps one-way cost on traded notional.
- Selection: development window picks exactly one winner by net total return
  (Sharpe as tiebreak); the holdout window promotes it only if net return is
  at least QQQ's and max drawdown is no more than 3 pp deeper than QQQ's.

The study reuses the frozen research universe (148 symbols including
delisted names) and never touches the production scoring pipeline.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence

from .cross_sectional import regime_map
from .lifecycle_validation import write_validation_report
from .simulation import _curve_metrics, _drawdown, _round
from .universe import BANKRUPT, UNIVERSE, WARMUP


STEP_GRID = 21
CONFIGS: tuple[dict[str, int], ...] = (
    {"step": 21, "top_n": 10},
    {"step": 21, "top_n": 20},
    {"step": 63, "top_n": 10},
    {"step": 63, "top_n": 20},
)
WINDOWS: dict[str, tuple[str, str]] = {
    "development": ("2021-08-06", "2025-01-02"),
    "holdout": ("2025-04-04", "2026-07-09"),
    "full": ("2021-08-06", "2026-07-10"),
}
SELECTION_WINDOW = "development"
PROMOTION_WINDOW = "holdout"
MAX_MDD_GAP_VS_QQQ_PCT = 3.0


def plan_signal_indices(calendar: Sequence[str], *, warmup: int = WARMUP) -> list[int]:
    """Global 21-session signal grid; the last session never signals."""

    return [
        index for index in range(warmup, len(calendar) - 1, STEP_GRID)
    ]


def _price_maps(
    bars: Mapping[str, Sequence[Mapping[str, Any]]], symbols: Sequence[str]
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]], dict[str, str]]:
    opens: dict[str, dict[str, float]] = {}
    closes: dict[str, dict[str, float]] = {}
    last_date: dict[str, str] = {}
    for symbol in symbols:
        rows = bars.get(symbol) or []
        opens[symbol] = {str(row["t"])[:10]: float(row["o"]) for row in rows}
        closes[symbol] = {str(row["t"])[:10]: float(row["c"]) for row in rows}
        if rows:
            last_date[symbol] = str(rows[-1]["t"])[:10]
    return opens, closes, last_date


def simulate_rotation(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    calendar: Sequence[str],
    signal_indices: Sequence[int],
    scores_by_date: Mapping[str, Mapping[str, float]],
    *,
    top_n: int,
    cost_bps: float,
    regimes: Mapping[str, bool | None],
    universe: Sequence[str] = tuple(UNIVERSE),
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Deterministic next-open Top-N rotation with daily marks."""

    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    one_way = cost_bps / 10_000.0
    opens, closes, last_dates = _price_maps(bars, list(universe))
    signal_set = {
        index for index in signal_indices
        if window_start <= calendar[index] <= window_end
    }
    if not signal_set:
        raise ValueError("no signal dates inside the requested window")
    first_entry_index = min(signal_set) + 1
    end_index = max(
        index for index in range(len(calendar)) if calendar[index] <= window_end
    )
    cash = 1.0
    shares: dict[str, float] = {}
    last_mark: dict[str, float] = {}
    pending_signal: str | None = None
    curve: list[dict[str, Any]] = []
    rebalances: list[dict[str, Any]] = []
    total_cost = 0.0
    total_traded = 0.0
    invested_sessions = 0

    def mark_price(symbol: str, date: str) -> float | None:
        price = closes[symbol].get(date)
        if price is not None:
            last_mark[symbol] = price
            return price
        if symbol in BANKRUPT and date > last_dates.get(symbol, date):
            return 0.0
        return last_mark.get(symbol)

    for index in range(first_entry_index - 1, end_index + 1):
        date = calendar[index]

        if pending_signal is not None:
            signal_date = pending_signal
            pending_signal = None
            execution_value = cash
            valuations: dict[str, float] = {}
            for symbol, quantity in shares.items():
                price = opens[symbol].get(date)
                if price is None:
                    price = mark_price(symbol, date) or 0.0
                valuations[symbol] = price
                execution_value += quantity * price
            regime_on = bool(regimes.get(signal_date))
            target: list[str] = []
            if regime_on:
                ranked = sorted(
                    (
                        (float(score), symbol)
                        for symbol, score in (scores_by_date.get(signal_date) or {}).items()
                        if symbol in opens and isinstance(score, (int, float))
                    ),
                    key=lambda pair: (-pair[0], pair[1]),
                )
                for _, symbol in ranked:
                    if opens[symbol].get(date) is not None:
                        target.append(symbol)
                    if len(target) == top_n:
                        break
            target_value = execution_value / len(target) if target else 0.0
            traded = 0.0
            for symbol in sorted(shares):
                price = valuations.get(symbol, 0.0)
                held_value = shares[symbol] * price
                desired = target_value if symbol in target else 0.0
                if held_value > desired + 1e-12 and price > 0:
                    sell_value = held_value - desired
                    sell_shares = sell_value / price
                    shares[symbol] -= sell_shares
                    fee = sell_value * one_way
                    cash += sell_value - fee
                    total_cost += fee
                    traded += sell_value
                if shares.get(symbol, 0.0) <= 1e-12:
                    shares.pop(symbol, None)
            buys = [
                symbol for symbol in target
                if shares.get(symbol, 0.0) * valuations.get(symbol, opens[symbol][date]) <
                target_value - 1e-12
            ]
            deficits = {
                symbol: target_value - shares.get(symbol, 0.0) * (
                    valuations.get(symbol) or opens[symbol][date]
                )
                for symbol in buys
            }
            total_deficit = sum(deficits.values())
            budget = min(cash, total_deficit)
            for symbol in sorted(buys):
                if total_deficit <= 0 or budget <= 0:
                    break
                spend = budget * deficits[symbol] / total_deficit
                price = valuations.get(symbol) or opens[symbol][date]
                fee = spend * one_way
                shares[symbol] = shares.get(symbol, 0.0) + (spend - fee) / price
                cash -= spend
                total_cost += fee
                traded += spend
            total_traded += traded
            rebalances.append({
                "signal_date": signal_date,
                "execution_date": date,
                "regime_on": regime_on,
                "members": sorted(target),
                "member_count": len(target),
                "traded_notional_equity": _round(traded),
                "equity_before_costs": _round(execution_value),
            })

        equity = cash
        for symbol, quantity in list(shares.items()):
            price = mark_price(symbol, date)
            if price is None:
                continue
            if price == 0.0 and symbol in BANKRUPT:
                shares.pop(symbol)
                continue
            equity += quantity * price
        if shares:
            invested_sessions += 1
        curve.append({
            "date": date,
            "equity": _round(equity),
            "in_position": bool(shares),
        })

        if index in signal_set and index + 1 <= end_index:
            pending_signal = calendar[index]

    drawdowns = _drawdown([float(point["equity"]) for point in curve])
    for point, drawdown in zip(curve, drawdowns):
        point["drawdown_pct"] = _round(drawdown, 4)
    metrics = {
        **_curve_metrics(curve),
        "rebalance_count": len(rebalances),
        "invested_session_pct": _round(
            invested_sessions / len(curve) * 100 if curve else None, 2
        ),
        "total_cost_equity": _round(total_cost),
        "total_traded_notional_equity": _round(total_traded),
        "average_member_count": _round(
            sum(row["member_count"] for row in rebalances) / len(rebalances)
            if rebalances else None, 2
        ),
    }
    return {
        "curve": curve,
        "metrics": metrics,
        "rebalances": rebalances,
    }


def _benchmark(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    symbol: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any] | None:
    rows = [
        row for row in bars.get(symbol, [])
        if start_date <= str(row["t"])[:10] <= end_date
    ]
    if not rows:
        return None
    base = float(rows[0]["c"])
    curve = [
        {"date": str(row["t"])[:10], "equity": _round(float(row["c"]) / base)}
        for row in rows
    ]
    drawdowns = _drawdown([float(point["equity"]) for point in curve])
    for point, drawdown in zip(curve, drawdowns):
        point["drawdown_pct"] = _round(drawdown, 4)
    return {"name": f"{symbol}_buy_and_hold", "metrics": _curve_metrics(curve)}


def _config_name(config: Mapping[str, int]) -> str:
    return f"step{config['step']}_top{config['top_n']}"


def run_rotation_study(
    *,
    feed: str = "iex",
    adjustment: str = "split",
    timeout: int = 60,
    cache_mode: str = "auto",
    cache_db: str | None = None,
    cost_bps: float = 10.0,
    scores_provider: Callable[[str], Mapping[str, float]] | None = None,
    panel: Any | None = None,
) -> dict[str, Any]:
    """Run all pre-registered configs across all frozen windows."""

    if panel is None:
        from .panel import load_panel

        panel = load_panel(
            feed, adjustment, timeout, cache_mode=cache_mode, cache_db=cache_db
        )
    calendar = list(panel.calendar)
    regimes = regime_map(panel.bars.get("SPY", []))
    signal_indices = plan_signal_indices(calendar)

    def default_scores(date: str) -> dict[str, float]:
        symbols = panel.scores_at(date)
        output: dict[str, float] = {}
        for symbol in UNIVERSE:
            composite = (symbols.get(symbol, {}).get("score") or {}).get("composite")
            if isinstance(composite, (int, float)) and math.isfinite(float(composite)):
                output[symbol] = float(composite)
        return output

    provider = scores_provider or default_scores
    needed_dates = sorted({
        calendar[index] for index in signal_indices
        if any(
            start <= calendar[index] <= end for start, end in WINDOWS.values()
        )
    })
    scores_by_date = {date: provider(date) for date in needed_dates}

    grid_positions = {index: position for position, index in enumerate(signal_indices)}
    results: dict[str, dict[str, Any]] = {}
    for config in CONFIGS:
        stride = config["step"] // STEP_GRID
        config_indices = [
            index for index in signal_indices if grid_positions[index] % stride == 0
        ]
        windows_output: dict[str, Any] = {}
        for window_name, (start, end) in WINDOWS.items():
            scenario = simulate_rotation(
                panel.bars, calendar, config_indices, scores_by_date,
                top_n=config["top_n"], cost_bps=cost_bps, regimes=regimes,
                window_start=start, window_end=end,
            )
            first_date = scenario["curve"][0]["date"]
            last_date = scenario["curve"][-1]["date"]
            windows_output[window_name] = {
                "start": first_date,
                "end": last_date,
                "metrics": scenario["metrics"],
                "rebalances": scenario["rebalances"],
                "benchmarks": {
                    name: _benchmark(panel.bars, name, first_date, last_date)
                    for name in ("QQQ", "SPY")
                },
            }
        results[_config_name(config)] = {
            "config": dict(config),
            "windows": windows_output,
        }

    def sort_key(name: str) -> tuple[float, float]:
        metrics = results[name]["windows"][SELECTION_WINDOW]["metrics"]
        return (
            float(metrics["total_return_pct"] or -math.inf),
            float(metrics["sharpe_rf0"] or -math.inf),
        )

    winner = max(results, key=sort_key)
    holdout = results[winner]["windows"][PROMOTION_WINDOW]
    qqq = (holdout["benchmarks"].get("QQQ") or {}).get("metrics") or {}
    candidate_return = float(holdout["metrics"]["total_return_pct"] or 0.0)
    candidate_mdd = float(holdout["metrics"]["max_drawdown_pct"] or 0.0)
    qqq_return = qqq.get("total_return_pct")
    qqq_mdd = qqq.get("max_drawdown_pct")
    conditions = {
        "holdout_return_vs_qqq": {
            "actual": _round(candidate_return),
            "required_min": _round(qqq_return),
            "pass": qqq_return is not None and candidate_return >= float(qqq_return),
        },
        "holdout_mdd_gap_vs_qqq": {
            "actual": _round(candidate_mdd),
            "required_min": _round(
                float(qqq_mdd) - MAX_MDD_GAP_VS_QQQ_PCT if qqq_mdd is not None else None
            ),
            "pass": qqq_mdd is not None
            and candidate_mdd >= float(qqq_mdd) - MAX_MDD_GAP_VS_QQQ_PCT,
        },
    }
    decision = "promote" if all(item["pass"] for item in conditions.values()) else "reject"
    report: dict[str, Any] = {
        "report_type": "rotation_study_research",
        "lifecycle": "research_only_not_promoted",
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "design": {
            "universe_size": len(UNIVERSE),
            "signal_grid_step": STEP_GRID,
            "configs": [dict(config) for config in CONFIGS],
            "windows": {name: list(span) for name, span in WINDOWS.items()},
            "selection_window": SELECTION_WINDOW,
            "promotion_window": PROMOTION_WINDOW,
            "cost_bps_oneway": cost_bps,
            "ranking": "composite_desc_equal_weight",
            "regime_filter": "spy_close_above_ma200_on_signal_date",
            "max_mdd_gap_vs_qqq_pct": MAX_MDD_GAP_VS_QQQ_PCT,
        },
        "signal_dates": needed_dates,
        "configs": results,
        "selection": {
            "winner": winner,
            "decision": decision,
            "conditions": conditions,
        },
    }
    payload = copy.deepcopy(report)
    payload.pop("generated_at")
    for name in results:
        for window in payload["configs"][name]["windows"].values():
            window.pop("rebalances", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    report["semantic_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


__all__ = [
    "CONFIGS",
    "WINDOWS",
    "plan_signal_indices",
    "run_rotation_study",
    "simulate_rotation",
    "write_validation_report",
]
