"""Hard gates and deterministic 100-point seller suitability scoring."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any
from zoneinfo import ZoneInfo

from .config import OptionsSellerConfig
from .events import event_gates, macro_event_gates


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_gate(leg: dict, cfg: OptionsSellerConfig, label: str) -> tuple[list[str], list[str]]:
    quote = leg.get("quote") if isinstance(leg.get("quote"), dict) else {}
    bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
    spread = _num(quote.get("spread_pct"))
    hard, data = [], []
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        data.append(f"{label}_quote_invalid")
    elif spread is None:
        data.append(f"{label}_quote_spread_missing")
    elif spread > cfg.max_quote_spread_pct:
        hard.append(f"{label}_quote_spread_too_wide")
    timestamp = str(quote.get("timestamp") or "")
    if not timestamp:
        data.append(f"{label}_quote_timestamp_missing")
    executable_size = quote.get("bid_size") if label == "short_leg" else quote.get("ask_size")
    if _num(executable_size) is None or float(executable_size) <= 0:
        data.append(f"{label}_executable_size_missing")
    return hard, data


def _oi_gate(leg: dict, cfg: OptionsSellerConfig, label: str) -> tuple[list[str], list[str]]:
    oi = leg.get("open_interest")
    if oi is None:
        return [], [f"{label}_open_interest_missing"]
    if int(oi) < cfg.min_open_interest:
        return [f"{label}_open_interest_below_minimum"], []
    return [], []


_NEW_YORK = ZoneInfo("America/New_York")


def _datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _quote_session_gate(
    leg: dict,
    expected_date: str,
    label: str,
    *,
    as_of: str | None,
    cfg: OptionsSellerConfig,
) -> list[str]:
    timestamp = str((leg.get("quote") or {}).get("timestamp") or "")
    if not timestamp or not expected_date:
        return []
    quoted_at = _datetime(timestamp)
    observed_at = _datetime(as_of)
    if quoted_at is None:
        return [f"{label}_quote_timestamp_invalid"]
    expected = dt.date.fromisoformat(expected_date[:10])
    quote_session = quoted_at.astimezone(_NEW_YORK).date()
    if quote_session < expected:
        return [f"{label}_quote_stale"]
    if observed_at is None:
        return []
    if quoted_at > observed_at + dt.timedelta(minutes=1):
        return [f"{label}_quote_timestamp_future"]
    observed_et = observed_at.astimezone(_NEW_YORK)
    market_open = (
        observed_et.weekday() < 5
        and dt.time(9, 30) <= observed_et.time() <= dt.time(16, 15)
        and observed_et.date() == expected
    )
    age = observed_at - quoted_at
    if market_open and age > dt.timedelta(minutes=cfg.max_quote_age_minutes):
        return [f"{label}_quote_stale"]
    return []


def _premium_score(spread: dict, setup: dict) -> tuple[int, float | None]:
    iv = _num((spread.get("short_leg") or {}).get("implied_volatility"))
    rv_pct = _num(setup.get("realized_vol_20_pct"))
    if iv is None or rv_pct is None or rv_pct <= 0:
        return 0, None
    # Alpaca option-chain impliedVolatility is normalized as a decimal ratio.
    iv_pct = iv * 100
    ratio = iv_pct / rv_pct
    score = round(max(0.0, min(15.0, (ratio - 0.75) / 1.25 * 15.0)))
    return int(score), round(ratio, 3)


def _liquidity_score(spread: dict, cfg: OptionsSellerConfig) -> int:
    short, long = spread["short_leg"], spread["long_leg"]
    oi_values = [_num(short.get("open_interest")), _num(long.get("open_interest"))]
    oi_score = 0
    if all(value is not None for value in oi_values):
        ratio = min(oi_values) / max(1, cfg.min_open_interest)
        oi_score = round(min(8.0, ratio * 4.0))
    spreads = [
        _num((short.get("quote") or {}).get("spread_pct")),
        _num((long.get("quote") or {}).get("spread_pct")),
    ]
    quote_score = 0
    if all(value is not None for value in spreads):
        worst = max(spreads)
        quote_score = round(max(0.0, 7.0 * (1 - worst / cfg.max_quote_spread_pct)))
    return int(oi_score + quote_score)


def _meets_suitable_liquidity(spread: dict, cfg: OptionsSellerConfig) -> bool:
    legs = (spread["short_leg"], spread["long_leg"])
    return all(
        (_num(leg.get("open_interest")) or 0) >= cfg.suitable_min_open_interest
        and (_num((leg.get("quote") or {}).get("spread_pct")) or math.inf)
        <= cfg.suitable_max_quote_spread_pct
        for leg in legs
    )


def score_spread(
    spread: dict[str, Any],
    setup: dict[str, Any],
    cfg: OptionsSellerConfig,
    *,
    option_feed: str,
    events: dict[str, Any] | None = None,
    account_equity: float | None = None,
    as_of: str | None = None,
    market_regime: dict[str, Any] | None = None,
    portfolio_risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply data-integrity gates, risk gates, then compute seller suitability."""
    strategy = spread["strategy"]
    signal = setup["signals"][strategy]
    hard: list[str] = []
    data: list[str] = []
    for label, leg in (("short_leg", spread["short_leg"]), ("long_leg", spread["long_leg"])):
        h, d = _quote_gate(leg, cfg, label)
        hard.extend(h)
        data.extend(d)
        h, d = _oi_gate(leg, cfg, label)
        hard.extend(h)
        data.extend(d)
        data.extend(_quote_session_gate(
            leg,
            setup.get("last_date", ""),
            label,
            as_of=as_of,
            cfg=cfg,
        ))

    delta = _num((spread["short_leg"].get("greeks") or {}).get("delta"))
    if delta is None or delta == 0:
        data.append("short_delta_missing")
    elif not cfg.short_delta_min <= abs(delta) <= cfg.short_delta_max:
        hard.append("short_delta_outside_range")

    trend_checks = signal.get("trend_checks") or {}
    if not trend_checks.get("price_vs_ma60"):
        hard.append("directional_ma60_trend_not_confirmed")

    credit = _num(spread.get("natural_credit"))
    width = _num(spread.get("width"))
    if credit is None or width is None:
        data.append("spread_credit_unavailable")
    elif credit <= 0 or credit >= width:
        hard.append("invalid_natural_credit")
    else:
        economic_credit = _num(spread.get("net_credit"))
        economic_credit = credit if economic_credit is None else economic_credit
        if economic_credit <= 0:
            hard.append("net_profit_after_cost_not_positive")
        elif economic_credit / width * 100 < cfg.min_credit_width_pct:
            hard.append("credit_width_below_minimum")

    if spread.get("band_buffer_pct") is None or spread["band_buffer_pct"] < 0:
        hard.append("short_strike_inside_bollinger_band")
    surface_features = spread.get("volatility_surface") or {}
    expected_move_coverage = _num(surface_features.get("expected_move_coverage"))
    regime = market_regime or {"status": "unavailable", "state": "unknown", "risk_multiplier": 0.0}
    coverage_floor = (
        cfg.cautious_min_expected_move_coverage
        if regime.get("state") == "cautious"
        else cfg.min_expected_move_coverage
    )
    if expected_move_coverage is None:
        data.append("expected_move_coverage_unavailable")
    elif expected_move_coverage < coverage_floor:
        hard.append("short_strike_inside_minimum_expected_move")
    event_ctx = events or {}
    hard.extend(event_gates(
        strategy,
        spread.get("expiration_date"),
        event_ctx,
        as_of_date=setup.get("last_date"),
    ))
    if event_ctx.get("status") != "ok":
        data.append("event_calendar_unavailable")

    macro_hard, macro_data = macro_event_gates(
        event_ctx.get("macro_events"),
        as_of_date=setup.get("last_date", ""),
        blackout_days=cfg.macro_event_blackout_days,
    )
    hard.extend(macro_hard)
    data.extend(macro_data)

    if regime.get("status") != "ok":
        data.append("market_regime_unavailable")
    elif regime.get("state") == "stress":
        hard.append("market_regime_stress")
    if (
        regime.get("state") == "cautious"
        and delta is not None
        and abs(delta) > cfg.cautious_short_delta_max
    ):
        hard.append("cautious_regime_short_delta_too_high")
    risk_multiplier = max(0.0, min(1.0, _num(regime.get("risk_multiplier")) or 0.0))
    portfolio = portfolio_risk or {"status": "unavailable"}
    hard.extend(portfolio.get("hard_gates") or [])
    data.extend(portfolio.get("data_flags") or [])

    max_loss = _num(
        spread.get("net_max_loss")
        if spread.get("net_max_loss") is not None
        else spread.get("max_loss")
    )
    risk_pct = None
    risk_budget = None
    max_spreads_by_risk = None
    if account_equity is not None and account_equity > 0 and regime.get("status") == "ok":
        risk_budget = account_equity * cfg.max_risk_pct / 100 * risk_multiplier
        if max_loss is None:
            data.append("max_loss_unavailable")
        else:
            risk_pct = max_loss / account_equity * 100
            max_spreads_by_risk = math.floor(risk_budget / max_loss) if max_loss > 0 else 0
            if max_loss > risk_budget:
                hard.append("account_risk_above_limit")

    premium_score, iv_rv_ratio = _premium_score(spread, setup)
    if iv_rv_ratio is None:
        data.append("iv_rv_unavailable")
    liquidity_score = _liquidity_score(spread, cfg)
    atr14 = _num(setup.get("atr14"))
    spot = _num(setup.get("last_close"))
    band_buffer_pct = max(0.0, _num(spread.get("band_buffer_pct")) or 0.0)
    atr_pct = atr14 / spot * 100 if atr14 and spot else None
    buffer_score = round(min(5.0, band_buffer_pct / atr_pct * 5.0)) if atr_pct else 0
    credit_width = max(0.0, _num(
        spread.get("net_credit_width_pct")
        if spread.get("net_credit_width_pct") is not None
        else spread.get("credit_width_pct")
    ) or 0.0)
    return_on_risk = max(0.0, _num(
        spread.get("net_return_on_risk_pct")
        if spread.get("net_return_on_risk_pct") is not None
        else spread.get("return_on_risk_pct")
    ) or 0.0)
    net_theta = max(0.0, _num((spread.get("net_greeks_per_spread") or {}).get("theta")) or 0.0)
    economics_score = int(
        round(min(10.0, credit_width / cfg.min_credit_width_pct * 8.0))
        + round(min(6.0, return_on_risk / 30.0 * 6.0))
        + round(min(4.0, net_theta / max(1.0, max_loss or 1.0) * 1000.0))
    )
    surface_premium = _num(surface_features.get("short_iv_premium_vol_points"))
    surface_score = 0
    if surface_premium is not None:
        surface_score += round(max(0.0, min(2.0, 1.0 + surface_premium / 5.0)))
    if expected_move_coverage is not None:
        surface_score += round(max(0.0, min(3.0, expected_move_coverage / 1.5 * 3.0)))
    term_slope = _num(surface_features.get("term_structure_slope_vol_points"))
    if term_slope is not None:
        surface_score += 1 if term_slope >= -3.0 else 0
    historical_rank = _num(surface_features.get("historical_iv_rank"))
    if historical_rank is not None:
        surface_score += round(max(0.0, min(4.0, historical_rank / 100.0 * 4.0)))
    volatility_score = min(25, premium_score + surface_score)
    expected_move_score = (
        round(max(0.0, min(5.0, expected_move_coverage / 1.5 * 5.0)))
        if expected_move_coverage is not None else 0
    )
    tail_event_score = int(min(10, buffer_score + expected_move_score))
    shape_score = round(
        min(9.0, (int(signal.get("boll_score") or 0) / 30.0) * 9.0)
        + min(6.0, (int(signal.get("trend_score") or 0) / 20.0) * 6.0)
    )
    regime_score = int(regime.get("score_points") or 0)
    portfolio_score = int(portfolio.get("portfolio_fit_score") or 0) if portfolio.get("status") == "ok" else 5
    if account_equity is not None and account_equity > 0 and max_loss is not None and risk_budget:
        usage = max_loss / risk_budget
        portfolio_score = round(max(0.0, min(10.0, 10.0 * (1.25 - usage))))
    components = {
        "underlying_shape_and_regime": int(min(20, shape_score + regime_score)),
        "volatility_edge": volatility_score,
        "spread_economics": min(20, economics_score),
        "chain_liquidity": liquidity_score,
        "tail_and_event_buffer": tail_event_score,
        "portfolio_fit": portfolio_score,
    }
    composite = min(100, sum(components.values()))

    # Missing IV is degradable because conservative executable credit still
    # measures economics. Missing quote/OI/delta is not: those rows are data-insufficient.
    critical_data = [
        reason for reason in data
        if not reason.startswith(("iv_rv_", "portfolio_context_", "portfolio_equity_"))
    ]
    suitable_liquidity = _meets_suitable_liquidity(spread, cfg)
    portfolio_confirmed = portfolio.get("status") == "ok"
    portfolio_required = portfolio.get("status") != "not_applicable"
    if hard:
        verdict = "回避"
    elif critical_data:
        verdict = "数据不足"
    elif option_feed == "indicative":
        composite = min(composite, cfg.suitable_score - 1)
        verdict = "观察等待"
    elif not suitable_liquidity or (portfolio_required and not portfolio_confirmed):
        composite = min(composite, cfg.suitable_score - 1)
        verdict = "观察等待" if composite >= cfg.watch_score else "回避"
    elif composite >= cfg.suitable_score:
        verdict = "适合构建"
    elif composite >= cfg.watch_score:
        verdict = "观察等待"
    else:
        verdict = "回避"
    return {
        "verdict": verdict,
        "seller_score": composite,
        "components": components,
        "hard_gate_reasons": sorted(set(hard)),
        "data_flags": sorted(set(data)),
        "iv_rv_ratio": iv_rv_ratio,
        "expected_move_coverage": expected_move_coverage,
        "market_regime": regime.get("state"),
        "risk_budget_multiplier": risk_multiplier,
        "suitable_liquidity": suitable_liquidity,
        "portfolio_confirmed": portfolio_confirmed,
        "portfolio_required": portfolio_required,
        "portfolio_risk": portfolio,
        "account_risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "account_risk_budget": round(risk_budget, 2) if risk_budget is not None else None,
        "max_spreads_by_risk": max_spreads_by_risk,
        "option_feed": option_feed,
    }
