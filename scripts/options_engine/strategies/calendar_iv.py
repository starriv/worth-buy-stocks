"""Near-delta-neutral long calendar spread volatility screen."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

from ..config import OptionsSellerConfig
from ..portfolio_risk import evaluate_portfolio_risk
from ..structure_optimizer import candidate_id

_NEW_YORK = ZoneInfo("America/New_York")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _leg(contract: dict[str, Any], side: str) -> dict[str, Any]:
    return {
        "symbol": contract.get("symbol"),
        "underlying_symbol": contract.get("underlying_symbol"),
        "root_symbol": contract.get("root_symbol"),
        "style": contract.get("style"),
        "multiplier": contract.get("multiplier"),
        "side": side,
        "expiration_date": contract.get("expiration_date"),
        "strike": contract.get("strike_price"),
        "open_interest": contract.get("open_interest"),
        "open_interest_date": contract.get("open_interest_date"),
        "quote": contract.get("quote") or {},
        "implied_volatility": contract.get("implied_volatility"),
        "greeks": contract.get("greeks") or {},
    }


def _event_gates(
    option_type: str,
    front_expiration: str,
    events: dict[str, Any],
    as_of_date: str,
) -> list[str]:
    expiry = _date(front_expiration)
    start = _date(as_of_date)
    if expiry is None:
        return ["invalid_front_expiration"]

    def in_front_window(value: Any) -> bool:
        day = _date(value)
        return bool(day and (start is None or start <= day) and day <= expiry)

    reasons = []
    if any(in_front_window(day) for day in events.get("earnings_dates") or []):
        reasons.append("earnings_before_front_expiration")
    if option_type == "call" and any(
        in_front_window(day) for day in events.get("ex_dividend_dates") or []
    ):
        reasons.append("ex_dividend_before_front_expiration")
    if any(
        isinstance(flag, dict)
        and flag.get("severity") in {"medium", "high"}
        and flag.get("type") != "earnings_event"
        for flag in events.get("red_flags") or []
    ):
        reasons.append("material_event_risk")
    return reasons


def _quote_gates(
    leg: dict[str, Any],
    label: str,
    cfg: OptionsSellerConfig,
    *,
    expected_date: str,
    as_of: str | None,
) -> tuple[list[str], list[str]]:
    quote = leg.get("quote") or {}
    bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
    spread_pct = _num(quote.get("spread_pct"))
    hard: list[str] = []
    data: list[str] = []
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        data.append(f"{label}_quote_invalid")
    elif spread_pct is None:
        data.append(f"{label}_quote_spread_missing")
    elif spread_pct > cfg.max_quote_spread_pct:
        hard.append(f"{label}_quote_spread_too_wide")
    size_key = "bid_size" if label == "front_short_leg" else "ask_size"
    if (_num(quote.get(size_key)) or 0) <= 0:
        data.append(f"{label}_executable_size_missing")
    quoted_at = _datetime(quote.get("timestamp"))
    observed_at = _datetime(as_of)
    if quoted_at is None:
        data.append(f"{label}_quote_timestamp_invalid")
    else:
        expected = _date(expected_date)
        if expected and quoted_at.astimezone(_NEW_YORK).date() < expected:
            data.append(f"{label}_quote_stale")
        if observed_at and quoted_at > observed_at + dt.timedelta(minutes=1):
            data.append(f"{label}_quote_timestamp_future")
        if observed_at and expected:
            observed_et = observed_at.astimezone(_NEW_YORK)
            market_open = (
                observed_et.weekday() < 5
                and dt.time(9, 30) <= observed_et.time() <= dt.time(16, 15)
                and observed_et.date() == expected
            )
            if market_open and observed_at - quoted_at > dt.timedelta(
                minutes=cfg.max_quote_age_minutes
            ):
                data.append(f"{label}_quote_stale")
    oi = leg.get("open_interest")
    if oi is None:
        data.append(f"{label}_open_interest_missing")
    elif int(oi) < cfg.min_open_interest:
        hard.append(f"{label}_open_interest_below_minimum")
    return hard, data


def _candidate(
    symbol: str,
    front: dict[str, Any],
    back: dict[str, Any],
    spot: float,
    as_of_date: dt.date,
    round_trip_cost: float | None,
) -> dict[str, Any]:
    front_quote, back_quote = front.get("quote") or {}, back.get("quote") or {}
    natural_debit = (
        float(back_quote["ask"]) - float(front_quote["bid"])
        if back_quote.get("ask") is not None and front_quote.get("bid") is not None
        else None
    )
    mid_debit = (
        float(back_quote["mid"]) - float(front_quote["mid"])
        if back_quote.get("mid") is not None and front_quote.get("mid") is not None
        else None
    )
    multiplier = int(front.get("multiplier") or 0)
    debit_at_risk = natural_debit * multiplier if natural_debit is not None else None
    net_debit_at_risk = (
        debit_at_risk + round_trip_cost
        if debit_at_risk is not None and round_trip_cost is not None
        else None
    )
    front_expiry, back_expiry = _date(front.get("expiration_date")), _date(back.get("expiration_date"))
    front_greeks, back_greeks = front.get("greeks") or {}, back.get("greeks") or {}
    net_greeks = {}
    raw_net = {}
    for greek in ("delta", "gamma", "theta", "vega", "rho"):
        front_value, back_value = _num(front_greeks.get(greek)), _num(back_greeks.get(greek))
        if front_value is not None and back_value is not None:
            raw_net[greek] = round(back_value - front_value, 8)
            net_greeks[greek] = round((back_value - front_value) * multiplier, 6)
    front_iv, back_iv = _num(front.get("implied_volatility")), _num(back.get("implied_volatility"))
    strike = float(front["strike_price"])
    option_type = str(front.get("type"))
    return {
        "underlying_symbol": symbol,
        "strategy_family": "calendar_iv",
        "strategy": f"long_{option_type}_calendar",
        "option_type": option_type,
        "expiration_date": front.get("expiration_date"),
        "front_expiration_date": front.get("expiration_date"),
        "back_expiration_date": back.get("expiration_date"),
        "front_dte": (front_expiry - as_of_date).days if front_expiry else None,
        "back_dte": (back_expiry - as_of_date).days if back_expiry else None,
        "expiry_gap_days": (back_expiry - front_expiry).days if front_expiry and back_expiry else None,
        "strike": strike,
        "moneyness_pct": round(abs(strike - spot) / spot * 100, 4),
        "short_leg": _leg(front, "sell_to_open"),
        "long_leg": _leg(back, "buy_to_open"),
        "natural_debit": round(natural_debit, 4) if natural_debit is not None else None,
        "mid_debit": round(mid_debit, 4) if mid_debit is not None else None,
        "multiplier": multiplier,
        "debit_at_risk": round(debit_at_risk, 2) if debit_at_risk is not None else None,
        "max_loss": round(debit_at_risk, 2) if debit_at_risk is not None else None,
        "estimated_round_trip_cost": round_trip_cost,
        "net_max_loss": round(net_debit_at_risk, 2) if net_debit_at_risk is not None else None,
        "max_profit": None,
        "max_profit_status": "not_determinable_before_front_expiration",
        "net_greeks_per_contract": raw_net,
        "net_greeks_per_spread": net_greeks,
        "abs_net_delta": abs(raw_net["delta"]) if "delta" in raw_net else None,
        "front_iv": front_iv,
        "back_iv": back_iv,
        "front_back_iv_edge": round(front_iv - back_iv, 6)
        if front_iv is not None and back_iv is not None else None,
        "front_back_iv_edge_vol_points": round((front_iv - back_iv) * 100, 2)
        if front_iv is not None and back_iv is not None else None,
        "risk_notes": [
            "net_debit_is_structure_loss_cap_before_assignment",
            "short_front_leg_assignment_can_create_stock_exposure",
            "delta_is_snapshot_not_a_persistent_hedge",
            "close_or_roll_requires_review_before_front_expiration",
        ],
    }


def build_calendar_spreads(
    symbol: str,
    chain: dict[str, Any],
    setup: dict[str, Any],
    cfg: OptionsSellerConfig,
    *,
    as_of_date: dt.date,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pair standard same-type/same-strike contracts across two expirations."""
    spot = float(setup["last_close"])
    groups: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    rejected: dict[str, int] = defaultdict(int)
    contracts_seen = 0
    for row in chain.get("contracts") or []:
        if not isinstance(row, dict) or row.get("type") not in {"call", "put"}:
            continue
        contracts_seen += 1
        if not row.get("tradable", True):
            rejected["contract_not_tradable"] += 1
            continue
        if str(row.get("underlying_symbol") or "").upper() != symbol:
            rejected["underlying_symbol_mismatch"] += 1
            continue
        if str(row.get("root_symbol") or "").upper() != symbol:
            rejected["adjusted_or_mismatched_root"] += 1
            continue
        if str(row.get("style") or "").lower() != "american":
            rejected["unsupported_option_style"] += 1
            continue
        if int(row.get("multiplier") or 0) != 100:
            rejected["non_standard_multiplier"] += 1
            continue
        expiry, strike = _date(row.get("expiration_date")), _num(row.get("strike_price"))
        if expiry is None or strike is None:
            rejected["invalid_expiration_or_strike"] += 1
            continue
        dte = (expiry - as_of_date).days
        in_front = cfg.calendar_front_dte_min <= dte <= cfg.calendar_front_dte_max
        in_back = cfg.calendar_back_dte_min <= dte <= cfg.calendar_back_dte_max
        if not (in_front or in_back):
            rejected["dte_outside_calendar_ranges"] += 1
            continue
        if abs(strike - spot) / spot * 100 > cfg.calendar_max_moneyness_pct:
            rejected["strike_outside_calendar_moneyness"] += 1
            continue
        groups[(str(row["type"]), strike)].append(row)

    out = []
    for rows in groups.values():
        ordered = sorted(rows, key=lambda row: str(row["expiration_date"]))
        fronts = [
            row for row in ordered
            if cfg.calendar_front_dte_min
            <= (_date(row["expiration_date"]) - as_of_date).days
            <= cfg.calendar_front_dte_max
        ]
        backs = [
            row for row in ordered
            if cfg.calendar_back_dte_min
            <= (_date(row["expiration_date"]) - as_of_date).days
            <= cfg.calendar_back_dte_max
        ]
        for front in fronts:
            for back in backs:
                gap = (_date(back["expiration_date"]) - _date(front["expiration_date"])).days
                if not cfg.calendar_min_expiry_gap_days <= gap <= cfg.calendar_max_expiry_gap_days:
                    rejected["expiry_gap_outside_range"] += 1
                    continue
                out.append(_candidate(
                    symbol, front, back, spot, as_of_date, cfg.round_trip_cost_per_spread
                ))
    if diagnostics is not None:
        diagnostics.update({
            "contracts_seen": contracts_seen,
            "same_strike_groups": len(groups),
            "candidates_built": len(out),
            "rejections": dict(sorted(rejected.items())),
        })
    return out


def _score_calendar(
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
    volatility_surface: dict[str, Any] | None,
) -> dict[str, Any]:
    hard: list[str] = []
    data: list[str] = []
    for label, leg in (
        ("front_short_leg", candidate["short_leg"]),
        ("back_long_leg", candidate["long_leg"]),
    ):
        leg_hard, leg_data = _quote_gates(
            leg, label, cfg, expected_date=str(setup.get("last_date") or ""), as_of=as_of
        )
        hard.extend(leg_hard)
        data.extend(leg_data)

    debit = _num(candidate.get("natural_debit"))
    if debit is None:
        data.append("calendar_natural_debit_unavailable")
    elif debit <= 0:
        hard.append("calendar_natural_debit_not_positive")
    abs_delta = _num(candidate.get("abs_net_delta"))
    if abs_delta is None:
        data.append("calendar_net_delta_unavailable")
    elif abs_delta > cfg.calendar_max_abs_net_delta:
        hard.append("calendar_net_delta_outside_tolerance")
    net_vega = _num((candidate.get("net_greeks_per_contract") or {}).get("vega"))
    net_theta = _num((candidate.get("net_greeks_per_contract") or {}).get("theta"))
    if net_vega is None:
        data.append("calendar_net_vega_unavailable")
    elif net_vega <= cfg.calendar_min_net_vega:
        hard.append("calendar_not_long_vega")
    if net_theta is None:
        data.append("calendar_net_theta_unavailable")
    iv_edge = _num(candidate.get("front_back_iv_edge"))
    if iv_edge is None:
        data.append("calendar_iv_term_edge_unavailable")
    elif iv_edge < cfg.calendar_min_front_back_iv_edge:
        hard.append("calendar_front_iv_too_cheap_vs_back")

    if events.get("status") != "ok":
        data.append("event_calendar_unavailable")
    hard.extend(_event_gates(
        str(candidate.get("option_type")),
        str(candidate.get("front_expiration_date")),
        events,
        str(setup.get("last_date") or ""),
    ))
    regime = market_regime or {"status": "unavailable", "state": "unknown", "risk_multiplier": 0.0}
    if regime.get("status") != "ok":
        data.append("market_regime_unavailable")
    elif regime.get("state") == "stress":
        hard.append("market_regime_stress")
    hard.extend(portfolio_risk.get("hard_gates") or [])
    data.extend(portfolio_risk.get("data_flags") or [])

    risk_multiplier = max(0.0, min(1.0, _num(regime.get("risk_multiplier")) or 0.0))
    risk = _num(candidate.get("net_max_loss") or candidate.get("max_loss"))
    risk_budget = None
    risk_pct = None
    max_spreads = None
    if account_equity is not None and account_equity > 0 and regime.get("status") == "ok":
        risk_budget = account_equity * cfg.max_risk_pct / 100 * risk_multiplier
        if risk is None:
            data.append("calendar_debit_at_risk_unavailable")
        else:
            risk_pct = risk / account_equity * 100
            max_spreads = math.floor(risk_budget / risk) if risk > 0 else 0
            if risk > risk_budget:
                hard.append("account_risk_above_limit")

    delta_score = 0 if abs_delta is None else round(
        max(0.0, 25.0 * (1 - abs_delta / cfg.calendar_max_abs_net_delta))
    )
    term_score = 0 if iv_edge is None else round(max(0.0, min(12.0, 6.0 + iv_edge * 200)))
    vega_efficiency = (
        net_vega * 100 / max(1.0, risk or 0.0)
        if net_vega is not None and net_vega > 0 and risk else 0.0
    )
    vega_score = round(max(0.0, min(10.0, vega_efficiency * 50)))
    history = (volatility_surface or {}).get("historical_iv") or {}
    iv_percentile = _num(history.get("iv_percentile"))
    history_score = round(max(0.0, min(8.0, (100 - iv_percentile) / 100 * 8))) \
        if iv_percentile is not None else 4
    theta_efficiency = (
        max(0.0, net_theta) * 100 / max(1.0, risk or 0.0)
        if net_theta is not None and risk else 0.0
    )
    carry_score = round(max(0.0, min(9.0, theta_efficiency * 90)))
    gap = _num(candidate.get("expiry_gap_days"))
    gap_score = round(max(0.0, min(6.0, 6 - abs((gap or 42) - 42) / 7)))
    oi_values = [_num(candidate["short_leg"].get("open_interest")), _num(candidate["long_leg"].get("open_interest"))]
    oi_score = round(min(10.0, min(oi_values) / max(1, cfg.min_open_interest) * 5)) \
        if all(value is not None for value in oi_values) else 0
    quote_spreads = [
        _num((candidate["short_leg"].get("quote") or {}).get("spread_pct")),
        _num((candidate["long_leg"].get("quote") or {}).get("spread_pct")),
    ]
    quote_score = round(max(0.0, 10 * (1 - max(quote_spreads) / cfg.max_quote_spread_pct))) \
        if all(value is not None for value in quote_spreads) else 0
    portfolio_score = (
        int(portfolio_risk.get("portfolio_fit_score") or 0)
        if portfolio_risk.get("status") == "ok" else 5
    )
    components = {
        "delta_neutrality": int(min(25, delta_score)),
        "iv_term_and_vega": int(min(30, term_score + vega_score + history_score)),
        "carry_and_structure": int(min(15, carry_score + gap_score)),
        "chain_liquidity": int(min(20, oi_score + quote_score)),
        "event_and_portfolio_fit": int(min(10, portfolio_score)),
    }
    composite = min(100, sum(components.values()))
    critical = [
        reason for reason in data
        if not reason.startswith(("portfolio_context_", "portfolio_equity_"))
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
        "option_feed": option_feed,
        "account_risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "account_risk_budget": round(risk_budget, 2) if risk_budget is not None else None,
        "max_spreads_by_risk": max_spreads,
        "historical_iv_percentile": iv_percentile,
        "portfolio_risk": portfolio_risk,
        "market_regime": regime.get("state"),
    }


def _profiles(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row for row in candidates
        if (row.get("score") or {}).get("verdict") in {"适合构建", "观察等待"}
    ]
    if not eligible:
        return {}

    def summary(profile: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "profile": profile,
            "candidate_id": row["candidate_id"],
            "strategy": row.get("strategy"),
            "front_expiration_date": row.get("front_expiration_date"),
            "back_expiration_date": row.get("back_expiration_date"),
            "strike": row.get("strike"),
            "abs_net_delta": row.get("abs_net_delta"),
            "front_back_iv_edge_vol_points": row.get("front_back_iv_edge_vol_points"),
            "natural_debit": row.get("natural_debit"),
            "strategy_score": (row.get("score") or {}).get("strategy_score"),
        }

    def value(row: dict[str, Any], key: str, fallback: float) -> float:
        parsed = _num(row.get(key))
        return parsed if parsed is not None else fallback

    selectors = {
        "delta_neutral": lambda row: (-value(row, "abs_net_delta", 99), (row.get("score") or {}).get("strategy_score", 0)),
        "term_edge": lambda row: (value(row, "front_back_iv_edge", -99), (row.get("score") or {}).get("strategy_score", 0)),
        "liquidity": lambda row: ((row.get("score") or {}).get("components", {}).get("chain_liquidity", 0), (row.get("score") or {}).get("strategy_score", 0)),
    }
    profiles = {}
    chosen: set[str] = set()
    for name, key in selectors.items():
        ordered = sorted(eligible, key=key, reverse=True)
        selected = next((row for row in ordered if row["candidate_id"] not in chosen), ordered[0])
        chosen.add(selected["candidate_id"])
        selected.setdefault("optimization_profiles", []).append(name)
        profiles[name] = summary(name, selected)
    return profiles


class CalendarIVStrategy:
    family = "calendar_iv"

    def screen_underlying(
        self, symbol: str, setup: dict[str, Any], cfg: OptionsSellerConfig
    ) -> list[dict[str, Any]]:
        if setup.get("status") != "ok" or not (setup.get("liquidity") or {}).get("eligible"):
            return []
        boll = setup.get("bollinger") or {}
        percent_b, slope = _num(boll.get("percent_b")), _num(boll.get("middle_slope_pct"))
        if percent_b is None or slope is None:
            return []
        center_distance = abs(percent_b - 0.5)
        eligible = (
            center_distance <= cfg.calendar_max_pct_b_deviation
            and abs(slope) <= cfg.calendar_max_middle_slope_pct
        )
        if not eligible:
            return []
        center_score = round(35 * (1 - center_distance / cfg.calendar_max_pct_b_deviation))
        slope_score = round(25 * (1 - abs(slope) / max(cfg.calendar_max_middle_slope_pct, 1e-9)))
        liquidity = setup.get("liquidity") or {}
        dollar_volume = _num(liquidity.get("avg_dollar_volume_20")) or 0.0
        liquidity_score = round(min(
            40.0,
            20 + math.log10(
                max(1.0, dollar_volume / max(1.0, cfg.min_underlying_dollar_volume))
            ) * 10,
        ))
        return [{
            "symbol": symbol,
            "strategy_family": self.family,
            "strategy": "calendar_iv",
            "underlying_score": int(min(100, center_score + slope_score + liquidity_score)),
            "last_close": setup.get("last_close"),
            "last_date": setup.get("last_date"),
            "classification": "bollinger_centered_low_directionality",
            "bollinger": setup.get("bollinger"),
        }]

    def chain_bounds(
        self, setup: dict[str, Any], cfg: OptionsSellerConfig, as_of_date: dt.date
    ) -> dict[str, Any]:
        spot = float(setup["last_close"])
        distance = cfg.calendar_max_moneyness_pct / 100
        return {
            "expiration_gte": as_of_date + dt.timedelta(days=cfg.calendar_front_dte_min),
            "expiration_lte": as_of_date + dt.timedelta(days=cfg.calendar_back_dte_max),
            "strike_gte": max(0.01, spot * (1 - distance)),
            "strike_lte": spot * (1 + distance),
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
        candidates = build_calendar_spreads(
            symbol, chain, setup, cfg, as_of_date=as_of_date, diagnostics=diagnostics
        )
        for candidate in candidates:
            candidate["portfolio_risk"] = evaluate_portfolio_risk(
                candidate, account_context, bars_by_symbol, cfg
            )
            candidate["score"] = _score_calendar(
                candidate,
                setup,
                cfg,
                option_feed=option_feed,
                events=events,
                as_of=as_of,
                market_regime=market_regime,
                account_equity=account_equity,
                portfolio_risk=candidate["portfolio_risk"],
                volatility_surface=volatility_surface,
            )
            candidate["candidate_id"] = candidate_id(candidate)
        candidates.sort(key=lambda row: (
            {"适合构建": 0, "观察等待": 1, "回避": 2, "数据不足": 3}.get(
                (row.get("score") or {}).get("verdict"), 9
            ),
            -int((row.get("score") or {}).get("strategy_score") or 0),
            _num(row.get("abs_net_delta"))
            if _num(row.get("abs_net_delta")) is not None else 99.0,
            row.get("front_expiration_date") or "",
            row.get("back_expiration_date") or "",
        ))
        return {
            "candidates": candidates,
            "diagnostics": {"calendar_iv": diagnostics},
            "profiles": _profiles(candidates),
        }
