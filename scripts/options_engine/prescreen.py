"""Underlying-only discovery: no contracts, option quotes, IV or Greeks."""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
from collections import Counter
from zoneinfo import ZoneInfo

from .contracts import validate_offline_input
from .events import macro_event_gates, normalize_events, normalize_macro_events
from .setup import evaluate_bollinger_setup
from .strategies import strategy_modules

SCREEN_VERSION = "worth-buy-stocks.options-prescreen.v1"
_ORDER = {"优先核对": 0, "观察等待": 1, "回避": 2, "数据不足": 3}

_STRUCTURES = {
    "bull_put": {
        "strategy_name": "Bull Put 看涨 Put 信用价差", "direction": "偏多",
        "option_types": ["put"], "premium_flow": "net_credit_required",
        "priority": 1,
        "leg_template": [
            {"action": "sell", "option_type": "put", "strike_relation": "higher"},
            {"action": "buy", "option_type": "put", "strike_relation": "lower"},
        ],
    },
    "bear_call": {
        "strategy_name": "Bear Call 看跌 Call 信用价差", "direction": "偏空",
        "option_types": ["call"], "premium_flow": "net_credit_required",
        "priority": 1,
        "leg_template": [
            {"action": "sell", "option_type": "call", "strike_relation": "lower"},
            {"action": "buy", "option_type": "call", "strike_relation": "higher"},
        ],
    },
    "iron_condor": {
        "strategy_name": "Iron Condor 铁鹰信用价差", "direction": "区间震荡",
        "option_types": ["put", "call"], "premium_flow": "net_credit_required",
        "priority": 2,
        "leg_template": [
            {"action": "buy", "option_type": "put", "strike_relation": "lowest"},
            {"action": "sell", "option_type": "put", "strike_relation": "below_spot"},
            {"action": "sell", "option_type": "call", "strike_relation": "above_spot"},
            {"action": "buy", "option_type": "call", "strike_relation": "highest"},
        ],
    },
    "iron_butterfly": {
        "strategy_name": "Iron Butterfly 铁蝶信用价差", "direction": "窄区间震荡",
        "option_types": ["put", "call"], "premium_flow": "net_credit_required",
        "priority": 3, "risk_label": "high_gamma_and_pin_risk",
        "leg_template": [
            {"action": "buy", "option_type": "put", "strike_relation": "lower_wing"},
            {"action": "sell", "option_type": "put", "strike_relation": "atm_common_body"},
            {"action": "sell", "option_type": "call", "strike_relation": "atm_common_body"},
            {"action": "buy", "option_type": "call", "strike_relation": "upper_wing"},
        ],
    },
    "calendar_iv": {
        "strategy_name": "长日历借记价差", "direction": "近月低方向性",
        "option_types": ["call_or_put_requires_chain"], "premium_flow": "net_debit",
        "priority": 4, "risk_label": "outside_income_objective",
        "leg_template": [
            {"action": "sell", "option_type": "same_type_to_confirm", "tenor": "front", "strike_relation": "same"},
            {"action": "buy", "option_type": "same_type_to_confirm", "tenor": "back", "strike_relation": "same"},
        ],
    },
}


def validate_scan_input(payload: dict) -> None:
    """Validate only consumed data; ignore legacy chains/account/IV overlays."""
    if not isinstance(payload, dict):
        raise ValueError("离线初筛输入必须是对象")
    validate_offline_input({
        "as_of": payload.get("as_of"), "bars": payload.get("bars"),
        "events": payload.get("events") or {}, "option_chains": {},
    }, validate_account_context=False)


def _rejections(setup: dict, family: str, cfg) -> list[str]:
    if setup.get("status") != "ok":
        return [setup.get("reason") or "daily_bars_unavailable"]
    reasons = []
    if not setup["liquidity"]["eligible"]:
        reasons.append("underlying_liquidity_below_minimum")
    if family == "vertical_credit":
        for strategy, signal in setup["signals"].items():
            reasons.extend(f"{strategy}:{reason}" for reason in signal["blocking_reasons"])
            if not signal["trend_checks"]["price_vs_ma60"]:
                reasons.append(f"{strategy}:ma60_direction_not_confirmed")
    else:
        boll = setup["bollinger"]
        if abs(boll["percent_b"] - 0.5) > getattr(cfg, family.replace("calendar_iv", "calendar") + "_max_pct_b_deviation"):
            reasons.append("price_outside_center_zone")
        if abs(boll["middle_slope_pct"]) > getattr(cfg, family.replace("calendar_iv", "calendar") + "_max_middle_slope_pct"):
            reasons.append("middle_slope_too_directional")
    return reasons


def prepare_setups(bars: dict, cfg, families: tuple) -> tuple[dict, list, dict]:
    """Calculate each symbol's daily indicators once and reuse module gates."""
    setups, rows, diagnostics = {}, [], {}
    modules = strategy_modules(families)
    for symbol in sorted(bars):
        setup = evaluate_bollinger_setup(bars[symbol], cfg)
        setups[symbol] = setup
        diagnostics[symbol] = {}
        for module in modules:
            screened = module.screen_underlying(symbol, setup, cfg)
            if module.family == "vertical_credit":
                screened = [row for row in screened if
                            setup["signals"][row["strategy"]]["trend_checks"]["price_vs_ma60"]]
            diagnostics[symbol][module.family] = {
                "status": "通过" if screened else (
                    "数据不足" if setup.get("status") != "ok" else "未通过"
                ),
                "reasons": [] if screened else _rejections(setup, module.family, cfg),
            }
            rows.extend(screened)
    rows.sort(key=lambda row: (-row["underlying_score"], row["symbol"], row["strategy"]))
    return setups, rows, diagnostics


def select_screen_symbols(rows: list, limit: int, families: tuple) -> list[str]:
    """Round-robin family rankings; scores across families aren't comparable."""
    queues = {family: list(dict.fromkeys(
        row["symbol"] for row in rows if row["strategy_family"] == family
    )) for family in families}
    selected = []
    while len(selected) < limit:
        progressed = False
        for family in families:
            while queues[family] and queues[family][0] in selected:
                queues[family].pop(0)
            if queues[family]:
                selected.append(queues[family].pop(0))
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


def _window(family: str, cfg) -> tuple[int, int]:
    if family == "vertical_credit":
        return cfg.dte_min, cfg.dte_max
    if family == "calendar_iv":
        return cfg.calendar_front_dte_min, cfg.calendar_front_dte_max
    return getattr(cfg, family + "_dte_min"), getattr(cfg, family + "_dte_max")


def _candidate(row: dict, events: dict, macro: dict, regime: dict, cfg, today: dt.date) -> dict:
    family = row["strategy_family"]
    low, high = _window(family, cfg)
    earliest, latest = today + dt.timedelta(days=low), today + dt.timedelta(days=high)
    reasons, warnings = [], []
    if events["status"] != "ok":
        warnings.append("event_calendar_unavailable")
    # Earnings constrain the feasible horizon; a later event need not reject
    # shorter expirations. No dates here claim listed/executable contracts.
    deadlines = [(day, "earnings") for day in events["earnings_dates"]]
    if row["strategy"] in {"bear_call", "iron_condor", "iron_butterfly"}:
        deadlines += [(day, "ex_dividend") for day in events["ex_dividend_dates"]]
    constraints = []
    for day, event_type in sorted(deadlines):
        event_day = dt.date.fromisoformat(day)
        if today <= event_day <= latest:
            latest = min(latest, event_day - dt.timedelta(days=1))
            constraints.append(f"{event_type}:{day}")
    if latest < earliest:
        reasons.append("no_event_safe_horizon")
    if events["red_flags"]:
        reasons.append("material_event_risk")
    if regime.get("status") != "ok":
        warnings.append("market_regime_unavailable")
    if family != "calendar_iv":
        if regime.get("state") == "stress":
            reasons.append("market_regime_stress")
        macro_reasons, macro_flags = macro_event_gates(
            macro, as_of_date=today.isoformat(), blackout_days=cfg.macro_event_blackout_days,
        )
        reasons.extend(macro_reasons)
        warnings.extend(macro_flags)
    verdict = "回避" if reasons else "观察等待" if warnings else "优先核对"
    candidate = {
        "symbol": row["symbol"], "strategy_family": family, "strategy": row["strategy"],
        "classification": row["classification"],
        "shape_score": row["underlying_score"],
        "shape_score_max": 50 if family == "vertical_credit" else 100,
        "verdict": verdict, "blocking_reasons": reasons, "data_flags": warnings,
        "horizon": {
            "minimum_days": low, "maximum_days": high,
            "earliest_date": earliest.isoformat(),
            "latest_event_safe_date": latest.isoformat() if latest >= earliest else None,
            "constraints": constraints,
        },
        "broker_checks": ["listed_expirations_and_strikes", "live_bid_ask_and_sizes",
                          "open_interest_and_option_liquidity", "IV_and_Greeks",
                          "net_credit_or_debit_and_maximum_risk", "account_risk_budget"],
    }
    candidate.update(deepcopy(_STRUCTURES[row["strategy"]]))
    candidate["expiration_structure"] = "front_back_same_strike" if family == "calendar_iv" else "same_expiration"
    candidate["income_eligibility"] = "requires_broker_net_credit_confirmation" if family != "calendar_iv" else "not_income_strategy"
    if family == "calendar_iv":
        candidate["back_horizon_days"] = [cfg.calendar_back_dte_min, cfg.calendar_back_dte_max]
    if family == "vertical_credit":
        candidate["bollinger_anchor"] = row["band_anchor"]
    return candidate


def build_screen(setups, rows, diagnostics, events, macro, regime, cfg, *,
                 as_of, selected, requested, top, families) -> dict:
    timestamp = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    today = timestamp.astimezone(ZoneInfo("America/New_York")).date()
    macro = normalize_macro_events(macro)
    symbols, ranked = {}, {family: [] for family in families}
    for symbol in selected:
        setup = setups[symbol]
        event = normalize_events(events.get(symbol), symbol, as_of_date=today.isoformat())
        candidates = [_candidate(row, event, macro, regime, cfg, today)
                      for row in rows if row["symbol"] == symbol]
        for candidate in candidates:
            ranked[candidate["strategy_family"]].append(candidate)
        symbols[symbol] = {
            "underlying": setup, "strategy_diagnostics": diagnostics[symbol],
            "events": event, "candidates": candidates,
            "status": ("数据不足" if setup.get("status") != "ok" else
                       "未通过初筛" if not candidates else
                       min(candidates, key=lambda c: _ORDER[c["verdict"]])["verdict"]),
        }
    for family, candidates in ranked.items():
        candidates.sort(key=lambda c: (_ORDER[c["verdict"]], -c["shape_score"], c["symbol"], c["strategy"]))
        ranked[family] = candidates[:top]
    returned = set(requested) | {c["symbol"] for cs in ranked.values() for c in cs}
    symbols = {s: row for s, row in symbols.items() if s in returned}
    return {
        "contract_version": SCREEN_VERSION, "kind": "options_prescreen_result",
        "status": "ok", "mode": "scan", "scope": "underlying_only",
        "as_of": as_of, "market_regime": regime, "macro_events": macro,
        "objective": "net_credit_income" if "calendar_iv" not in families else "explicit_mixed_strategies",
        "strategy_order": sorted(families, key=lambda f: {"vertical_credit": 1, "iron_condor": 2, "iron_butterfly": 3, "calendar_iv": 4}[f]),
        "symbols": symbols, "rankings_by_strategy": ranked,
        "rejection_counts_by_strategy": {
            family: dict(Counter(
                reason for diagnostic in diagnostics.values()
                for reason in diagnostic[family]["reasons"]
            )) for family in families
        },
        "counts": {
            "underlyings_with_setup": len({row["symbol"] for row in rows}),
            "shape_candidates": len(rows), "symbols_returned": len(symbols),
            "candidates": sum(len(row["candidates"]) for row in symbols.values()),
            "verdicts": dict(Counter(c["verdict"] for row in symbols.values() for c in row["candidates"])),
        },
    }
