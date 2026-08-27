"""Read-only concentration overlay for standard defined-risk option positions."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from .config import OptionsSellerConfig

_OCC = re.compile(r"^([A-Z.]{1,10}?)(\d{6})([CP])(\d{8})$")


def parse_occ_symbol(symbol: str) -> dict[str, Any] | None:
    match = _OCC.fullmatch(str(symbol or "").strip().upper().replace(" ", ""))
    if not match:
        return None
    root, expiry, option_code, strike_code = match.groups()
    return {
        "underlying_symbol": root,
        "expiration_date": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:6]}",
        "type": "call" if option_code == "C" else "put",
        "strike": int(strike_code) / 1000.0,
    }


def option_underlyings(account_context: dict[str, Any] | None) -> list[str]:
    rows = (account_context or {}).get("positions") or {}
    parsed = [parse_occ_symbol(symbol) for symbol in rows]
    return sorted({row["underlying_symbol"] for row in parsed if row})


def extract_defined_risk_spreads(account_context: dict[str, Any] | None) -> dict[str, Any]:
    """Pair standard long/short option legs conservatively by expiry and type."""
    if not isinstance(account_context, dict) or account_context.get("status") != "ok":
        return {"status": "unavailable", "spreads": [], "unpaired_positions": []}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    unpaired = []
    for symbol, position in ((account_context.get("positions") or {}).items()):
        parsed = parse_occ_symbol(symbol)
        if not parsed:
            if str((position or {}).get("asset_class") or "").lower() in {"us_option", "option"}:
                unpaired.append(symbol)
            continue
        qty = abs(float((position or {}).get("qty") or 0.0))
        side = str((position or {}).get("side") or "").lower()
        if qty <= 0 or side not in {"long", "short"}:
            unpaired.append(symbol)
            continue
        groups[(parsed["underlying_symbol"], parsed["expiration_date"], parsed["type"])].append({
            **parsed, "symbol": symbol, "side": side, "qty_remaining": qty,
        })

    spreads = []
    for (underlying, expiry, option_type), legs in sorted(groups.items()):
        shorts = sorted(
            [row for row in legs if row["side"] == "short"],
            key=lambda row: row["strike"], reverse=option_type == "put",
        )
        longs = [row for row in legs if row["side"] == "long"]
        for short in shorts:
            protectors = sorted(
                [row for row in longs if row["qty_remaining"] > 0 and (
                    row["strike"] < short["strike"] if option_type == "put"
                    else row["strike"] > short["strike"]
                )],
                key=lambda row: abs(row["strike"] - short["strike"]),
            )
            for long in protectors:
                if short["qty_remaining"] <= 0:
                    break
                qty = min(short["qty_remaining"], long["qty_remaining"])
                width = abs(short["strike"] - long["strike"])
                spreads.append({
                    "underlying_symbol": underlying,
                    "expiration_date": expiry,
                    "strategy": "bull_put" if option_type == "put" else "bear_call",
                    "short_symbol": short["symbol"],
                    "long_symbol": long["symbol"],
                    "quantity": qty,
                    "width": width,
                    "conservative_max_risk": round(width * 100 * qty, 2),
                })
                short["qty_remaining"] -= qty
                long["qty_remaining"] -= qty
        unpaired.extend(row["symbol"] for row in legs if row["qty_remaining"] > 0)
    return {
        "status": "ok",
        "spreads": spreads,
        "unpaired_positions": sorted(set(unpaired)),
    }


def _correlation(left: list[dict], right: list[dict], window: int = 60) -> float | None:
    left_map = {str(row.get("t"))[:10]: float(row["c"]) for row in left if row.get("c")}
    right_map = {str(row.get("t"))[:10]: float(row["c"]) for row in right if row.get("c")}
    dates = sorted(set(left_map) & set(right_map))[-(window + 1):]
    if len(dates) < window + 1:
        return None
    x = [left_map[dates[i]] / left_map[dates[i - 1]] - 1 for i in range(1, len(dates))]
    y = [right_map[dates[i]] / right_map[dates[i - 1]] - 1 for i in range(1, len(dates))]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else None


def evaluate_portfolio_risk(
    candidate: dict[str, Any],
    account_context: dict[str, Any] | None,
    bars_by_symbol: dict[str, list[dict]] | None,
    cfg: OptionsSellerConfig,
    market_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (account_context or {}).get("status") == "ignored_for_scan":
        return {
            "status": "not_applicable",
            "scope": "account_independent_scan",
            "portfolio_fit_score": 5,
            "hard_gates": [],
            "data_flags": [],
        }
    parsed = extract_defined_risk_spreads(account_context)
    if parsed.get("status") != "ok":
        return {"status": "unavailable", "hard_gates": [], "data_flags": ["portfolio_context_unavailable"]}
    account = (account_context or {}).get("account") or {}
    equity = float(account.get("equity") or account.get("portfolio_value") or 0.0)
    if equity <= 0:
        return {"status": "unavailable", "hard_gates": [], "data_flags": ["portfolio_equity_unavailable"]}
    symbol = str(candidate.get("underlying_symbol") or "").upper()
    candidate_risk = float(candidate.get("net_max_loss") or candidate.get("max_loss") or 0.0)
    existing = parsed["spreads"]
    total_risk = sum(float(row["conservative_max_risk"]) for row in existing)
    symbol_risk = sum(
        float(row["conservative_max_risk"])
        for row in existing if row["underlying_symbol"] == symbol
    )
    candidate_direction = 1.0 if candidate.get("strategy") == "bull_put" else -1.0
    correlations = {}
    loss_correlations = {}
    correlated_risk = symbol_risk
    bars = bars_by_symbol or {}
    for other in sorted({row["underlying_symbol"] for row in existing} - {symbol}):
        value = _correlation(bars.get(symbol) or [], bars.get(other) or [])
        correlations[other] = round(value, 4) if value is not None else None
        adjusted = []
        for row in existing:
            if row["underlying_symbol"] != other or value is None:
                continue
            existing_direction = 1.0 if row.get("strategy") == "bull_put" else -1.0
            loss_correlation = value * candidate_direction * existing_direction
            adjusted.append(loss_correlation)
            if loss_correlation >= cfg.correlation_threshold:
                correlated_risk += float(row["conservative_max_risk"])
        loss_correlations[other] = (
            round(max(adjusted), 4) if adjusted else None
        )
    expiration = str(candidate.get("expiration_date") or "")[:10]
    expiration_risk = sum(
        float(row["conservative_max_risk"])
        for row in existing if str(row.get("expiration_date") or "")[:10] == expiration
    )
    after = {
        "symbol_risk_pct": (symbol_risk + candidate_risk) / equity * 100,
        "correlated_risk_pct": (correlated_risk + candidate_risk) / equity * 100,
        "expiration_risk_pct": (expiration_risk + candidate_risk) / equity * 100,
        "total_option_risk_pct": (total_risk + candidate_risk) / equity * 100,
    }
    regime = market_regime or {}
    risk_multiplier = (
        max(0.0, min(1.0, float(regime.get("risk_multiplier") or 0.0)))
        if regime.get("status") == "ok" else 1.0
    )
    effective_limits = {
        "symbol_risk_pct": cfg.max_symbol_option_risk_pct * risk_multiplier,
        "correlated_risk_pct": cfg.max_correlated_option_risk_pct * risk_multiplier,
        "expiration_risk_pct": cfg.max_expiration_option_risk_pct * risk_multiplier,
        "total_option_risk_pct": cfg.max_total_option_risk_pct * risk_multiplier,
    }
    hard = []
    if account.get("trading_blocked") or account.get("account_blocked"):
        hard.append("account_trading_blocked")
    if after["symbol_risk_pct"] > effective_limits["symbol_risk_pct"]:
        hard.append("portfolio_symbol_concentration_above_limit")
    if after["correlated_risk_pct"] > effective_limits["correlated_risk_pct"]:
        hard.append("portfolio_correlated_concentration_above_limit")
    if after["expiration_risk_pct"] > effective_limits["expiration_risk_pct"]:
        hard.append("portfolio_expiration_concentration_above_limit")
    if after["total_option_risk_pct"] > effective_limits["total_option_risk_pct"]:
        hard.append("portfolio_total_option_risk_above_limit")
    data = ["portfolio_contains_unpaired_option_positions"] if parsed["unpaired_positions"] else []
    missing_correlations = [other for other, value in correlations.items() if value is None]
    if missing_correlations:
        data.append("portfolio_correlation_unavailable:" + ",".join(missing_correlations))
    tightest_usage = max(
        after[key] / limit if limit > 0 else math.inf
        for key, limit in effective_limits.items()
    )
    return {
        "status": "ok",
        **{key: round(value, 4) for key, value in after.items()},
        "existing_defined_risk_spreads": len(existing),
        "unpaired_option_positions": parsed["unpaired_positions"],
        "correlations": correlations,
        "loss_correlations": loss_correlations,
        "effective_limits": {
            key: round(value, 4) for key, value in effective_limits.items()
        },
        "risk_budget_multiplier": risk_multiplier,
        "portfolio_fit_score": round(max(0.0, min(10.0, 10.0 * (1 - tightest_usage)))),
        "hard_gates": hard,
        "data_flags": data,
    }
