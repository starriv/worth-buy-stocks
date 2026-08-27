"""Delta-neutral, same-expiry symmetric Iron Condor screening module."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from ..config import OptionsSellerConfig
from ..events import event_gates
from ..multi_leg import (
    conservative_credit,
    contract_leg,
    date_value,
    leg_quality_gates,
    net_greeks,
    number,
)
from ..portfolio_risk import evaluate_portfolio_risk
from ..structure_optimizer import candidate_id, dynamic_width_bounds


def _valid_contract(
    row: Any,
    symbol: str,
    as_of_date: dt.date,
    cfg: OptionsSellerConfig,
    rejected: dict[str, int],
) -> bool:
    if not isinstance(row, dict) or row.get("type") not in {"put", "call"}:
        return False
    if not row.get("tradable", True):
        rejected["contract_not_tradable"] += 1
        return False
    if str(row.get("underlying_symbol") or "").upper() != symbol:
        rejected["underlying_symbol_mismatch"] += 1
        return False
    if str(row.get("root_symbol") or "").upper() != symbol:
        rejected["adjusted_or_mismatched_root"] += 1
        return False
    if str(row.get("style") or "").lower() != "american":
        rejected["unsupported_option_style"] += 1
        return False
    if int(row.get("multiplier") or 0) != 100:
        rejected["non_standard_multiplier"] += 1
        return False
    expiry = date_value(row.get("expiration_date"))
    strike = number(row.get("strike_price"))
    if expiry is None or strike is None:
        rejected["invalid_expiration_or_strike"] += 1
        return False
    if not cfg.iron_condor_dte_min <= (expiry - as_of_date).days <= cfg.iron_condor_dte_max:
        rejected["dte_outside_iron_condor_range"] += 1
        return False
    return True


def _build_candidate(
    symbol: str,
    long_put: dict[str, Any],
    short_put: dict[str, Any],
    short_call: dict[str, Any],
    long_call: dict[str, Any],
    spot: float,
    as_of_date: dt.date,
    round_trip_cost: float | None,
) -> dict[str, Any]:
    shorts = [short_put, short_call]
    longs = [long_put, long_call]
    natural_credit = conservative_credit(
        shorts, longs, short_price_key="bid", long_price_key="ask"
    )
    mid_credit = conservative_credit(
        shorts, longs, short_price_key="mid", long_price_key="mid"
    )
    put_width = float(short_put["strike_price"]) - float(long_put["strike_price"])
    call_width = float(long_call["strike_price"]) - float(short_call["strike_price"])
    multiplier = int(short_put.get("multiplier") or 0)
    put_loss = (
        (put_width - natural_credit) * multiplier
        if natural_credit is not None and 0 < natural_credit < put_width
        else None
    )
    call_loss = (
        (call_width - natural_credit) * multiplier
        if natural_credit is not None and 0 < natural_credit < call_width
        else None
    )
    max_loss = max(put_loss, call_loss) if put_loss is not None and call_loss is not None else None
    max_profit = natural_credit * multiplier if natural_credit is not None else None
    expiry = date_value(short_put.get("expiration_date"))
    per_contract, per_spread = net_greeks(
        [(1, long_put), (-1, short_put), (-1, short_call), (1, long_call)],
        multiplier,
    )
    legs = [
        contract_leg(long_put, "buy_to_open", "long_put_wing"),
        contract_leg(short_put, "sell_to_open", "short_put"),
        contract_leg(short_call, "sell_to_open", "short_call"),
        contract_leg(long_call, "buy_to_open", "long_call_wing"),
    ]
    net_max_profit = (
        max_profit - round_trip_cost
        if max_profit is not None and round_trip_cost is not None
        else None
    )
    net_max_loss = (
        max_loss + round_trip_cost
        if max_loss is not None and round_trip_cost is not None
        else None
    )
    short_put_strike = float(short_put["strike_price"])
    short_call_strike = float(short_call["strike_price"])
    return {
        "underlying_symbol": symbol,
        "strategy_family": "iron_condor",
        "strategy": "iron_condor",
        "expiration_date": short_put.get("expiration_date"),
        "dte": (expiry - as_of_date).days if expiry else None,
        "legs": legs,
        "long_put_leg": legs[0],
        "short_put_leg": legs[1],
        "short_call_leg": legs[2],
        "long_call_leg": legs[3],
        "put_width": round(put_width, 4),
        "call_width": round(call_width, 4),
        "wing_width": round(max(put_width, call_width), 4),
        "profit_zone_width": round(short_call_strike - short_put_strike, 4),
        "natural_credit": round(natural_credit, 4) if natural_credit is not None else None,
        "mid_credit": round(mid_credit, 4) if mid_credit is not None else None,
        "multiplier": multiplier,
        "max_profit": round(max_profit, 2) if max_profit is not None else None,
        "downside_max_loss": round(put_loss, 2) if put_loss is not None else None,
        "upside_max_loss": round(call_loss, 2) if call_loss is not None else None,
        "max_loss": round(max_loss, 2) if max_loss is not None else None,
        "estimated_round_trip_cost": round_trip_cost,
        "net_max_profit": round(net_max_profit, 2) if net_max_profit is not None else None,
        "net_max_loss": round(net_max_loss, 2) if net_max_loss is not None else None,
        "lower_break_even": round(short_put_strike - natural_credit, 4)
        if natural_credit is not None else None,
        "upper_break_even": round(short_call_strike + natural_credit, 4)
        if natural_credit is not None else None,
        "credit_width_pct": round(natural_credit / max(put_width, call_width) * 100, 2)
        if natural_credit is not None else None,
        "return_on_risk_pct": round(max_profit / max_loss * 100, 2)
        if max_profit is not None and max_loss and max_loss > 0 else None,
        "net_return_on_risk_pct": round(net_max_profit / net_max_loss * 100, 2)
        if net_max_profit is not None and net_max_loss and net_max_loss > 0 else None,
        "net_greeks_per_contract": per_contract,
        "net_greeks_per_spread": per_spread,
        "abs_net_delta": abs(per_contract["delta"]) if "delta" in per_contract else None,
        "spot": spot,
        "risk_notes": [
            "defined_risk_only_if_all_four_legs_remain_intact",
            "american_short_legs_retain_early_assignment_risk",
            "expiration_pin_and_after_hours_assignment_risk_remain",
            "delta_is_a_snapshot_and_will_drift_with_price_time_and_volatility",
            "lifecycle_manager_is_read_only_and_requires_user_confirmed_fills",
        ],
    }


def build_iron_condors(
    symbol: str,
    chain: dict[str, Any],
    setup: dict[str, Any],
    cfg: OptionsSellerConfig,
    *,
    as_of_date: dt.date,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pair two same-expiry credit spreads with exactly equal wing widths."""
    rejected: dict[str, int] = defaultdict(int)
    by_expiry: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"put": [], "call": []}
    )
    contracts_seen = 0
    spot = float(setup["last_close"])
    distance = cfg.max_short_distance_pct / 100.0
    for row in chain.get("contracts") or []:
        if not isinstance(row, dict) or row.get("type") not in {"put", "call"}:
            continue
        contracts_seen += 1
        if not _valid_contract(row, symbol, as_of_date, cfg, rejected):
            continue
        strike = float(row["strike_price"])
        if not spot * (1 - distance) <= strike <= spot * (1 + distance):
            rejected["strike_too_far_from_spot"] += 1
            continue
        by_expiry[str(row["expiration_date"])][str(row["type"])].append(row)

    min_width, max_width = dynamic_width_bounds(setup, cfg)
    candidates: list[dict[str, Any]] = []
    qualified_short_puts = 0
    qualified_short_calls = 0
    for expiry in sorted(by_expiry):
        puts = sorted(by_expiry[expiry]["put"], key=lambda row: float(row["strike_price"]))
        calls = sorted(by_expiry[expiry]["call"], key=lambda row: float(row["strike_price"]))
        put_arms: dict[float, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        call_arms: dict[float, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        for short in puts:
            short_strike = float(short["strike_price"])
            delta = number((short.get("greeks") or {}).get("delta"))
            if short_strike >= spot:
                rejected["short_put_not_otm"] += 1
                continue
            if delta is None or delta == 0:
                rejected["short_put_delta_missing"] += 1
                continue
            if not (
                cfg.iron_condor_short_delta_min <= abs(delta) <= cfg.iron_condor_short_delta_max
            ):
                rejected["short_put_delta_outside_range"] += 1
                continue
            qualified_short_puts += 1
            protectors = 0
            for long in puts:
                width = short_strike - float(long["strike_price"])
                if min_width <= width <= max_width:
                    put_arms[round(width, 6)].append((long, short))
                    protectors += 1
            if not protectors:
                rejected["long_put_wing_unavailable"] += 1
        for short in calls:
            short_strike = float(short["strike_price"])
            delta = number((short.get("greeks") or {}).get("delta"))
            if short_strike <= spot:
                rejected["short_call_not_otm"] += 1
                continue
            if delta is None or delta == 0:
                rejected["short_call_delta_missing"] += 1
                continue
            if not (
                cfg.iron_condor_short_delta_min <= abs(delta) <= cfg.iron_condor_short_delta_max
            ):
                rejected["short_call_delta_outside_range"] += 1
                continue
            qualified_short_calls += 1
            protectors = 0
            for long in calls:
                width = float(long["strike_price"]) - short_strike
                if min_width <= width <= max_width:
                    call_arms[round(width, 6)].append((short, long))
                    protectors += 1
            if not protectors:
                rejected["long_call_wing_unavailable"] += 1

        common_widths = sorted(set(put_arms) & set(call_arms))
        if (put_arms or call_arms) and not common_widths:
            rejected["asymmetric_wing_width"] += 1
        for width in common_widths:
            for long_put, short_put in put_arms[width]:
                for short_call, long_call in call_arms[width]:
                    candidates.append(_build_candidate(
                        symbol, long_put, short_put, short_call, long_call, spot,
                        as_of_date, cfg.round_trip_cost_per_spread,
                    ))
    if diagnostics is not None:
        diagnostics.update({
            "contracts_seen": contracts_seen,
            "qualified_short_puts": qualified_short_puts,
            "qualified_short_calls": qualified_short_calls,
            "candidates_built": len(candidates),
            "dynamic_width_bounds": {
                "minimum": min_width,
                "maximum": max_width,
                "atr14": setup.get("atr14"),
            },
            "rejections": dict(sorted(rejected.items())),
        })
    return candidates


def _surface_features(
    candidate: dict[str, Any], setup: dict[str, Any], surface: dict[str, Any] | None
) -> dict[str, Any]:
    expiry = ((surface or {}).get("expirations") or {}).get(
        str(candidate.get("expiration_date"))
    ) or {}
    spot = number(setup.get("last_close"))
    expected = number(expiry.get("one_sigma_expected_move_abs"))
    put_strike = number(candidate["short_put_leg"].get("strike"))
    call_strike = number(candidate["short_call_leg"].get("strike"))
    put_coverage = (spot - put_strike) / expected if spot and expected and put_strike else None
    call_coverage = (call_strike - spot) / expected if spot and expected and call_strike else None
    short_ivs = [
        number(candidate["short_put_leg"].get("implied_volatility")),
        number(candidate["short_call_leg"].get("implied_volatility")),
    ]
    short_iv = sum(short_ivs) / 2 if all(value is not None for value in short_ivs) else None
    atm_iv = number(expiry.get("atm_iv"))
    history = (surface or {}).get("historical_iv") or {}
    return {
        "atm_iv_pct": expiry.get("atm_iv_pct"),
        "average_short_iv_pct": round(short_iv * 100, 2) if short_iv is not None else None,
        "short_iv_premium_vol_points": round((short_iv - atm_iv) * 100, 2)
        if short_iv is not None and atm_iv is not None else None,
        "one_sigma_expected_move_abs": expected,
        "put_expected_move_coverage": round(put_coverage, 4) if put_coverage is not None else None,
        "call_expected_move_coverage": round(call_coverage, 4) if call_coverage is not None else None,
        "minimum_expected_move_coverage": round(min(put_coverage, call_coverage), 4)
        if put_coverage is not None and call_coverage is not None else None,
        "historical_iv_rank": history.get("iv_rank"),
        "historical_iv_percentile": history.get("iv_percentile"),
        "risk_reversal_25d_vol_points": expiry.get("risk_reversal_25d_vol_points"),
    }


def _score(
    candidate: dict[str, Any],
    setup: dict[str, Any],
    cfg: OptionsSellerConfig,
    *,
    option_feed: str,
    events: dict[str, Any],
    as_of: str | None,
    market_regime: dict[str, Any] | None,
    account_equity: float | None,
    portfolio_risk: dict[str, Any],
) -> dict[str, Any]:
    hard: list[str] = []
    data: list[str] = []
    for label, leg in (
        ("long_put_leg", candidate["long_put_leg"]),
        ("short_put_leg", candidate["short_put_leg"]),
        ("short_call_leg", candidate["short_call_leg"]),
        ("long_call_leg", candidate["long_call_leg"]),
    ):
        leg_hard, leg_data = leg_quality_gates(
            leg, label, cfg, expected_date=str(setup.get("last_date") or ""), as_of=as_of
        )
        hard.extend(leg_hard)
        data.extend(leg_data)

    for label in ("short_put_leg", "short_call_leg"):
        delta = number((candidate[label].get("greeks") or {}).get("delta"))
        if delta is None or delta == 0:
            data.append(f"{label}_delta_missing")
        elif not cfg.iron_condor_short_delta_min <= abs(delta) <= cfg.iron_condor_short_delta_max:
            hard.append(f"{label}_delta_outside_range")
    abs_delta = number(candidate.get("abs_net_delta"))
    if abs_delta is None:
        data.append("iron_condor_net_delta_unavailable")
    elif abs_delta > cfg.iron_condor_max_abs_net_delta:
        hard.append("iron_condor_net_delta_outside_tolerance")
    per_contract = candidate.get("net_greeks_per_contract") or {}
    theta, vega = number(per_contract.get("theta")), number(per_contract.get("vega"))
    gamma = number(per_contract.get("gamma"))
    if theta is None:
        data.append("iron_condor_net_theta_unavailable")
    elif theta <= 0:
        hard.append("iron_condor_not_positive_theta")
    if vega is None:
        data.append("iron_condor_net_vega_unavailable")
    elif vega >= 0:
        hard.append("iron_condor_not_short_vega")
    if gamma is None:
        data.append("iron_condor_net_gamma_unavailable")

    credit = number(candidate.get("natural_credit"))
    width = number(candidate.get("wing_width"))
    if credit is None or width is None:
        data.append("iron_condor_credit_unavailable")
    elif credit <= 0 or credit >= width:
        hard.append("invalid_iron_condor_natural_credit")
    elif credit / width * 100 < cfg.iron_condor_min_credit_width_pct:
        hard.append("iron_condor_credit_width_below_minimum")
    if (
        candidate.get("estimated_round_trip_cost") is not None
        and (number(candidate.get("net_max_profit")) or 0.0) <= 0
    ):
        hard.append("iron_condor_net_profit_after_cost_not_positive")
    coverage = number((candidate.get("volatility_surface") or {}).get(
        "minimum_expected_move_coverage"
    ))
    if coverage is None:
        data.append("iron_condor_expected_move_coverage_unavailable")
    elif coverage < cfg.iron_condor_min_expected_move_coverage:
        hard.append("iron_condor_short_strike_inside_minimum_expected_move")

    if events.get("status") != "ok":
        data.append("event_calendar_unavailable")
    hard.extend(event_gates(
        "bear_call", str(candidate.get("expiration_date")), events,
        as_of_date=str(setup.get("last_date") or ""),
    ))
    regime = market_regime or {"status": "unavailable", "state": "unknown", "risk_multiplier": 0.0}
    if regime.get("status") != "ok":
        data.append("market_regime_unavailable")
    elif regime.get("state") == "stress":
        hard.append("market_regime_stress")
    hard.extend(portfolio_risk.get("hard_gates") or [])
    data.extend(portfolio_risk.get("data_flags") or [])

    risk_multiplier = max(0.0, min(1.0, number(regime.get("risk_multiplier")) or 0.0))
    max_loss = number(candidate.get("net_max_loss") or candidate.get("max_loss"))
    risk_budget = risk_pct = None
    max_spreads = None
    if account_equity is not None and account_equity > 0 and regime.get("status") == "ok":
        risk_budget = account_equity * cfg.max_risk_pct / 100 * risk_multiplier
        if max_loss is None:
            data.append("iron_condor_max_loss_unavailable")
        else:
            risk_pct = max_loss / account_equity * 100
            max_spreads = math.floor(risk_budget / max_loss) if max_loss > 0 else 0
            if max_loss > risk_budget:
                hard.append("account_risk_above_limit")

    boll = setup.get("bollinger") or {}
    percent_b = number(boll.get("percent_b"))
    slope = abs(number(boll.get("middle_slope_pct")) or 0.0)
    center = abs(percent_b - 0.5) if percent_b is not None else 0.5
    underlying_points = round(max(0.0, 12 * (1 - center / cfg.iron_condor_max_pct_b_deviation)))
    underlying_points += round(max(
        0.0, 5 * (1 - slope / max(cfg.iron_condor_max_middle_slope_pct, 1e-9))
    ))
    underlying_points += min(3, int(regime.get("score_points") or 0))

    surface = candidate.get("volatility_surface") or {}
    short_iv_pct = number(surface.get("average_short_iv_pct"))
    if short_iv_pct is None:
        data.append("iron_condor_short_iv_unavailable")
    rv_pct = number(setup.get("realized_vol_20_pct"))
    iv_rv_ratio = short_iv_pct / rv_pct if short_iv_pct is not None and rv_pct and rv_pct > 0 else None
    if iv_rv_ratio is None:
        data.append("iv_rv_unavailable")
    volatility_points = round(max(0.0, min(15.0, ((iv_rv_ratio or 0.75) - 0.75) / 1.25 * 15)))
    iv_rank = number(surface.get("historical_iv_rank"))
    volatility_points += round(max(0.0, min(7.0, (iv_rank or 50.0) / 100 * 7)))
    risk_reversal = abs(number(surface.get("risk_reversal_25d_vol_points")) or 0.0)
    volatility_points += round(max(0.0, 3.0 * (1 - risk_reversal / 10.0)))

    credit_width = max(0.0, number(candidate.get("credit_width_pct")) or 0.0)
    net_profit = number(candidate.get("net_max_profit"))
    net_loss = number(candidate.get("net_max_loss"))
    return_on_risk = (
        max(0.0, net_profit / net_loss * 100)
        if net_profit is not None and net_loss and net_loss > 0
        else max(0.0, number(candidate.get("return_on_risk_pct")) or 0.0)
    )
    theta_per_spread = max(0.0, number((candidate.get("net_greeks_per_spread") or {}).get("theta")) or 0.0)
    economics_points = round(min(10.0, credit_width / cfg.iron_condor_min_credit_width_pct * 8.0))
    economics_points += round(min(6.0, return_on_risk / 30.0 * 6.0))
    economics_points += round(min(4.0, theta_per_spread / max(1.0, max_loss or 1.0) * 100))

    oi_values = [number(leg.get("open_interest")) for leg in candidate["legs"]]
    oi_points = round(min(8.0, min(oi_values) / max(1, cfg.min_open_interest) * 4)) \
        if all(value is not None for value in oi_values) else 0
    quote_spreads = [number((leg.get("quote") or {}).get("spread_pct")) for leg in candidate["legs"]]
    quote_points = round(max(0.0, 7 * (1 - max(quote_spreads) / cfg.max_quote_spread_pct))) \
        if all(value is not None for value in quote_spreads) else 0
    tail_points = round(max(0.0, min(8.0, (coverage or 0.0) / 1.5 * 8)))
    tail_points += 2 if events.get("status") == "ok" else 0
    portfolio_points = int(portfolio_risk.get("portfolio_fit_score") or 0) \
        if portfolio_risk.get("status") == "ok" else 5
    if risk_budget and max_loss is not None:
        portfolio_points = round(max(0.0, min(10.0, 10 * (1.25 - max_loss / risk_budget))))
    components = {
        "underlying_neutrality_and_regime": int(min(20, underlying_points)),
        "volatility_edge": int(min(25, volatility_points)),
        "structure_economics": int(min(20, economics_points)),
        "chain_liquidity": int(min(15, oi_points + quote_points)),
        "expected_move_and_event_safety": int(min(10, tail_points)),
        "portfolio_fit": int(min(10, portfolio_points)),
    }
    composite = min(100, sum(components.values()))
    critical = [
        reason for reason in data
        if not reason.startswith(("iv_rv_", "portfolio_context_", "portfolio_equity_"))
    ]
    if hard:
        verdict = "回避"
    elif critical:
        verdict = "数据不足"
    elif option_feed == "indicative":
        composite = min(composite, cfg.suitable_score - 1)
        verdict = "观察等待"
    elif composite >= cfg.suitable_score:
        verdict = "适合构建"
    elif composite >= cfg.watch_score:
        verdict = "观察等待"
    else:
        verdict = "回避"
    return {
        "verdict": verdict,
        "strategy_score": composite,
        "seller_score": composite,
        "components": components,
        "hard_gate_reasons": sorted(set(hard)),
        "data_flags": sorted(set(data)),
        "iv_rv_ratio": round(iv_rv_ratio, 3) if iv_rv_ratio is not None else None,
        "minimum_expected_move_coverage": coverage,
        "market_regime": regime.get("state"),
        "risk_budget_multiplier": risk_multiplier,
        "portfolio_risk": portfolio_risk,
        "account_risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "account_risk_budget": round(risk_budget, 2) if risk_budget is not None else None,
        "max_spreads_by_risk": max_spreads,
        "option_feed": option_feed,
    }


def _profiles(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row for row in candidates
        if (row.get("score") or {}).get("verdict") in {"适合构建", "观察等待"}
    ]
    if not eligible:
        return {}

    def value(row: dict[str, Any], key: str, fallback: float = -1.0) -> float:
        parsed = number(row.get(key))
        return parsed if parsed is not None else fallback

    selectors = {
        "balanced": lambda row: (
            (row.get("score") or {}).get("strategy_score", 0),
            -value(row, "abs_net_delta", 99.0),
        ),
        "safety": lambda row: (
            value((row.get("volatility_surface") or {}), "minimum_expected_move_coverage"),
            -value(row, "max_loss", 1e12),
        ),
        "income": lambda row: (
            value(
                row, "net_return_on_risk_pct", value(row, "return_on_risk_pct")
            ),
            value(row, "credit_width_pct"),
        ),
    }
    profiles: dict[str, Any] = {}
    chosen: set[str] = set()
    for name, selector in selectors.items():
        ordered = sorted(eligible, key=selector, reverse=True)
        selected = next((row for row in ordered if row["candidate_id"] not in chosen), ordered[0])
        chosen.add(selected["candidate_id"])
        selected.setdefault("optimization_profiles", []).append(name)
        profiles[name] = {
            "profile": name,
            "candidate_id": selected["candidate_id"],
            "expiration_date": selected.get("expiration_date"),
            "short_put_strike": selected["short_put_leg"].get("strike"),
            "short_call_strike": selected["short_call_leg"].get("strike"),
            "wing_width": selected.get("wing_width"),
            "natural_credit": selected.get("natural_credit"),
            "net_max_loss": selected.get("net_max_loss") or selected.get("max_loss"),
            "abs_net_delta": selected.get("abs_net_delta"),
            "strategy_score": (selected.get("score") or {}).get("strategy_score"),
        }
    return profiles


class IronCondorStrategy:
    family = "iron_condor"

    def screen_underlying(
        self, symbol: str, setup: dict[str, Any], cfg: OptionsSellerConfig
    ) -> list[dict[str, Any]]:
        if setup.get("status") != "ok" or not (setup.get("liquidity") or {}).get("eligible"):
            return []
        boll = setup.get("bollinger") or {}
        percent_b = number(boll.get("percent_b"))
        slope = number(boll.get("middle_slope_pct"))
        if percent_b is None or slope is None:
            return []
        center_distance = abs(percent_b - 0.5)
        if (
            center_distance > cfg.iron_condor_max_pct_b_deviation
            or abs(slope) > cfg.iron_condor_max_middle_slope_pct
        ):
            return []
        center_score = round(40 * (1 - center_distance / cfg.iron_condor_max_pct_b_deviation))
        slope_score = round(30 * (1 - abs(slope) / max(cfg.iron_condor_max_middle_slope_pct, 1e-9)))
        liquidity = setup.get("liquidity") or {}
        dollar_volume = number(liquidity.get("avg_dollar_volume_20")) or 0.0
        liquidity_score = round(min(
            30.0, 15 + math.log10(max(
                1.0, dollar_volume / max(1.0, cfg.min_underlying_dollar_volume)
            )) * 10,
        ))
        return [{
            "symbol": symbol,
            "strategy_family": self.family,
            "strategy": self.family,
            "underlying_score": int(min(100, center_score + slope_score + liquidity_score)),
            "last_close": setup.get("last_close"),
            "last_date": setup.get("last_date"),
            "classification": "bollinger_centered_range_bound",
            "bollinger": setup.get("bollinger"),
        }]

    def chain_bounds(
        self, setup: dict[str, Any], cfg: OptionsSellerConfig, as_of_date: dt.date
    ) -> dict[str, Any]:
        spot = float(setup["last_close"])
        distance = cfg.max_short_distance_pct / 100.0
        return {
            "expiration_gte": as_of_date + dt.timedelta(days=cfg.iron_condor_dte_min),
            "expiration_lte": as_of_date + dt.timedelta(days=cfg.iron_condor_dte_max),
            "strike_gte": max(0.01, spot * (1 - distance) - cfg.max_width),
            "strike_lte": spot * (1 + distance) + cfg.max_width,
        }

    def analyze(
        self,
        symbol: str,
        setup: dict[str, Any],
        chain: dict[str, Any],
        events: dict[str, Any],
        cfg: OptionsSellerConfig,
        *,
        as_of_date: dt.date,
        as_of: str | None,
        option_feed: str,
        market_regime: dict[str, Any] | None,
        account_equity: float | None,
        account_context: dict[str, Any] | None,
        bars_by_symbol: dict[str, list[dict]] | None,
        volatility_surface: dict[str, Any] | None,
    ) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {}
        candidates = build_iron_condors(
            symbol, chain, setup, cfg, as_of_date=as_of_date, diagnostics=diagnostics
        )
        for candidate in candidates:
            candidate["volatility_surface"] = _surface_features(
                candidate, setup, volatility_surface
            )
            candidate["portfolio_risk"] = evaluate_portfolio_risk(
                candidate, account_context, bars_by_symbol, cfg
            )
            candidate["score"] = _score(
                candidate, setup, cfg, option_feed=option_feed, events=events,
                as_of=as_of, market_regime=market_regime, account_equity=account_equity,
                portfolio_risk=candidate["portfolio_risk"],
            )
            candidate["candidate_id"] = candidate_id(candidate)
        candidates.sort(key=lambda row: (
            {"适合构建": 0, "观察等待": 1, "回避": 2, "数据不足": 3}.get(
                (row.get("score") or {}).get("verdict"), 9
            ),
            -int((row.get("score") or {}).get("strategy_score") or 0),
            value_or(row.get("abs_net_delta"), 99.0),
            -value_or((row.get("volatility_surface") or {}).get(
                "minimum_expected_move_coverage"
            ), -1.0),
        ))
        return {
            "candidates": candidates,
            "diagnostics": {"iron_condor": diagnostics},
            "profiles": _profiles(candidates),
        }


def value_or(value: Any, fallback: float) -> float:
    parsed = number(value)
    return parsed if parsed is not None else fallback
