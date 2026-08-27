"""Deterministic lifecycle review for defined-risk credit structures."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo

from .events import event_gates, normalize_events
from .ledger import position_legs, position_width

_NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class LifecycleConfig:
    take_profit_pct: float = 50.0
    exit_dte: int = 14
    hard_exit_dte: int = 7
    defense_delta: float = 0.45
    max_close_debit_multiple: float = 2.0
    max_quote_age_minutes: int = 15
    pin_risk_dte: int = 1
    pin_risk_width_fraction: float = 0.10

    def validate(self) -> "LifecycleConfig":
        if not 0 < self.take_profit_pct < 100:
            raise ValueError("take_profit_pct must be between 0 and 100")
        if self.exit_dte < 0:
            raise ValueError("exit_dte must be >= 0")
        if self.hard_exit_dte < 0 or self.hard_exit_dte > self.exit_dte:
            raise ValueError("hard_exit_dte must be between 0 and exit_dte")
        if not 0 < self.defense_delta < 1:
            raise ValueError("defense_delta must be between 0 and 1")
        if self.max_close_debit_multiple <= 1:
            raise ValueError("max_close_debit_multiple must be > 1")
        if self.max_quote_age_minutes < 1:
            raise ValueError("max_quote_age_minutes must be >= 1")
        if self.pin_risk_dte < 0:
            raise ValueError("pin_risk_dte must be >= 0")
        if not 0 < self.pin_risk_width_fraction <= 1:
            raise ValueError("pin_risk_width_fraction must be between 0 and 1")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _market_date(value: Any) -> dt.date | None:
    """Interpret timestamps in New York while preserving explicit date-only input."""
    text = str(value or "").strip()
    if "T" not in text:
        return _date(text)
    parsed = _datetime(text)
    return parsed.astimezone(_NEW_YORK).date() if parsed is not None else None


def _contract_map(chain: dict[str, Any]) -> dict[str, dict]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in chain.get("contracts") or [] if isinstance(row, dict)
    }


def review_position(
    position: dict[str, Any],
    chain: dict[str, Any],
    events: Any,
    *,
    spot: float | None,
    as_of_date: dt.date,
    cfg: LifecycleConfig,
    observed_at: dt.datetime | None = None,
    expected_quote_date: dt.date | None = None,
) -> dict[str, Any]:
    """Return one action using conservative natural close pricing."""
    cfg.validate()
    strategy = str(position.get("strategy") or "")
    if strategy in {"iron_condor", "iron_butterfly"}:
        return _review_four_leg_position(
            position, chain, events, spot=spot, as_of_date=as_of_date, cfg=cfg,
            observed_at=observed_at, expected_quote_date=expected_quote_date,
        )
    if strategy not in {"bull_put", "bear_call"}:
        return {"action": "数据不足", "reasons": ["unsupported_strategy"]}
    expiry = _date(position.get("expiration_date"))
    initial_credit = _num(position.get("initial_credit"))
    quantity = _num(position.get("quantity"))
    short_strike = _num(position.get("short_strike"))
    long_strike = _num(position.get("long_strike"))
    required = [expiry, initial_credit, quantity, short_strike, long_strike]
    if any(value is None for value in required) or initial_credit <= 0 or quantity <= 0:
        return {"action": "数据不足", "reasons": ["position_terms_invalid"]}
    dte = (expiry - as_of_date).days
    if dte <= 0:
        return {
            "position_id": position.get("position_id"),
            "underlying_symbol": position.get("underlying_symbol"),
            "strategy": strategy,
            "action": "立即退出评估",
            "reasons": ["expiration_reached"],
            "dte": dte,
            "quantity": quantity,
        }
    confirmation = position.get("account_confirmation")
    if confirmation is not None and confirmation.get("status") != "confirmed":
        return {
            "position_id": position.get("position_id"),
            "underlying_symbol": position.get("underlying_symbol"),
            "strategy": strategy,
            "action": "数据不足",
            "reasons": [confirmation.get("reason") or "account_position_unconfirmed"],
            "dte": dte,
            "quantity": quantity,
        }

    known_hard_reasons = []
    if spot is not None and (
        (strategy == "bull_put" and spot <= short_strike)
        or (strategy == "bear_call" and spot >= short_strike)
    ):
        known_hard_reasons.append("underlying_breached_short_strike")
    if dte <= cfg.hard_exit_dte:
        known_hard_reasons.append("expiration_hard_exit_window")
    planned_exit = dte <= cfg.exit_dte

    def incomplete(reason: str, **metrics: Any) -> dict[str, Any]:
        """Preserve deterministic exit actions while attaching missing-data warnings."""
        if known_hard_reasons:
            action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
        elif planned_exit:
            action, reasons = "计划退出", ["expiration_window_reached"]
        else:
            action, reasons = "数据不足", [reason]
        row = {
            "action": action,
            "reasons": reasons,
            "dte": dte,
            **metrics,
        }
        if action != "数据不足":
            row["data_warnings"] = [reason]
        return row

    if not isinstance(chain, dict) or chain.get("status") != "ok":
        return incomplete("current_option_chain_unavailable")
    contracts = _contract_map(chain)
    short = contracts.get(str(position.get("short_leg_symbol") or "").upper())
    long = contracts.get(str(position.get("long_leg_symbol") or "").upper())
    if not short or not long:
        return incomplete("current_leg_quotes_unavailable")
    expected_type = "put" if strategy == "bull_put" else "call"
    for leg, expected_strike in ((short, short_strike), (long, long_strike)):
        if (
            str(leg.get("expiration_date") or "")[:10] != expiry.isoformat()
            or str(leg.get("type") or "").lower() != expected_type
            or _num(leg.get("strike_price")) != expected_strike
            or int(leg.get("multiplier") or 0) != 100
        ):
            return incomplete("current_leg_contract_mismatch")
    quote_times = [
        _datetime((leg.get("quote") or {}).get("timestamp")) for leg in (short, long)
    ]
    if any(value is None for value in quote_times):
        return incomplete("current_quote_timestamp_invalid")
    expected_quote_date = expected_quote_date or as_of_date
    if any(value.astimezone(_NEW_YORK).date() < expected_quote_date for value in quote_times):
        return incomplete("current_quote_stale")
    if observed_at is not None and any(value > observed_at + dt.timedelta(minutes=1) for value in quote_times):
        return incomplete("current_quote_timestamp_future")
    if observed_at is not None:
        observed_et = observed_at.astimezone(_NEW_YORK)
        market_open = (
            observed_et.weekday() < 5
            and dt.time(9, 30) <= observed_et.time() <= dt.time(16, 15)
            and observed_et.date() == expected_quote_date
        )
        if market_open and any(
            observed_at - value > dt.timedelta(minutes=cfg.max_quote_age_minutes)
            for value in quote_times
        ):
            return incomplete("current_quote_stale")
    short_quote, long_quote = short.get("quote") or {}, long.get("quote") or {}
    short_bid = _num(short_quote.get("bid"))
    short_ask = _num(short_quote.get("ask"))
    long_bid = _num(long_quote.get("bid"))
    long_ask = _num(long_quote.get("ask"))
    short_ask_size = _num(short_quote.get("ask_size"))
    long_bid_size = _num(long_quote.get("bid_size"))
    delta = _num((short.get("greeks") or {}).get("delta"))
    quote_values = (short_bid, short_ask, long_bid, long_ask)
    if (
        any(value is None for value in quote_values)
        or short_bid < 0 or short_ask <= 0 or long_bid < 0 or long_ask <= 0
        or short_ask < short_bid or long_ask < long_bid
        or short_ask_size is None or short_ask_size <= 0
        or long_bid_size is None or long_bid_size <= 0
        or delta is None
    ):
        return incomplete("current_quote_or_delta_invalid")
    close_debit = short_ask - long_bid
    width = abs(short_strike - long_strike)
    if close_debit < 0 or close_debit > width:
        return incomplete("current_close_debit_invalid")
    captured = (initial_credit - close_debit) / initial_credit * 100
    normalized_events = normalize_events(
        events, str(position.get("underlying_symbol") or ""), as_of_date=as_of_date.isoformat()
    )
    known_hard_reasons.extend(event_gates(
        strategy, expiry.isoformat(), normalized_events, as_of_date=as_of_date.isoformat()
    ))
    if abs(delta) >= cfg.defense_delta:
        known_hard_reasons.append("short_delta_defense_triggered")
    if close_debit >= initial_credit * cfg.max_close_debit_multiple:
        known_hard_reasons.append("loss_multiple_defense_triggered")
    capture_target = captured >= cfg.take_profit_pct
    if normalized_events.get("status") != "ok":
        if known_hard_reasons:
            action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
        elif planned_exit:
            action, reasons = "计划退出", ["expiration_window_reached"]
        elif capture_target:
            action, reasons = "止盈退出", ["profit_target_reached"]
        else:
            return incomplete(
                "event_calendar_unavailable", natural_close_debit=round(close_debit, 4)
            )
        data_warnings = ["event_calendar_unavailable"]
    else:
        action = ""
        reasons = []
        data_warnings = []
    if spot is None:
        if not action:
            if known_hard_reasons:
                action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
            elif planned_exit:
                action, reasons = "计划退出", ["expiration_window_reached"]
            elif capture_target:
                action, reasons = "止盈退出", ["profit_target_reached"]
            else:
                return incomplete("underlying_spot_unavailable")
        data_warnings.append("underlying_spot_unavailable")
    if not action:
        if known_hard_reasons:
            action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
        elif planned_exit:
            action, reasons = "计划退出", ["expiration_window_reached"]
        elif capture_target:
            action, reasons = "止盈退出", ["profit_target_reached"]
        else:
            action, reasons = "继续持有", ["lifecycle_conditions_normal"]
    multiplier = int(short.get("multiplier") or 100)
    return {
        "position_id": position.get("position_id"),
        "underlying_symbol": position.get("underlying_symbol"),
        "strategy": strategy,
        "action": action,
        "reasons": reasons,
        "data_warnings": sorted(set(data_warnings)),
        "dte": dte,
        "spot": round(float(spot), 4) if spot is not None else None,
        "short_delta": round(delta, 6),
        "initial_credit": round(initial_credit, 4),
        "natural_close_debit": round(close_debit, 4),
        "profit_capture_pct": round(captured, 2),
        "estimated_open_profit": round((initial_credit - close_debit) * multiplier * quantity, 2),
        "quantity": quantity,
        "option_feed": chain.get("feed"),
        "account_confirmation": confirmation,
    }


def _review_four_leg_position(
    position: dict[str, Any],
    chain: dict[str, Any],
    events: Any,
    *,
    spot: float | None,
    as_of_date: dt.date,
    cfg: LifecycleConfig,
    observed_at: dt.datetime | None,
    expected_quote_date: dt.date | None,
) -> dict[str, Any]:
    """Review an Iron Condor or Iron Butterfly as one indivisible structure."""
    strategy = str(position.get("strategy") or "")
    expiry = _date(position.get("expiration_date"))
    initial_credit = _num(position.get("initial_credit"))
    quantity = _num(position.get("quantity"))
    legs = position_legs(position)
    expected_roles = (
        {"long_put_wing", "short_put", "short_call", "long_call_wing"}
        if strategy == "iron_condor" else
        {"long_put_wing", "short_put_body", "short_call_body", "long_call_wing"}
    )
    roles = {str(leg.get("role") or ""): leg for leg in legs}
    expected_leg_terms = {
        "long_put_wing": ("long", "put"),
        "long_call_wing": ("long", "call"),
        short_put_role if 'short_put_role' in locals() else
        ("short_put" if strategy == "iron_condor" else "short_put_body"): ("short", "put"),
        short_call_role if 'short_call_role' in locals() else
        ("short_call" if strategy == "iron_condor" else "short_call_body"): ("short", "call"),
    }
    leg_terms_valid = all(
        str(roles.get(role, {}).get("side") or "").lower()
        in ({"long", "buy", "buy_to_open"} if side == "long" else {"short", "sell", "sell_to_open"})
        and str(roles.get(role, {}).get("type") or "").lower() == option_type
        for role, (side, option_type) in expected_leg_terms.items()
    )
    try:
        width = position_width(position)
    except (TypeError, ValueError, IndexError):
        width = 0.0
    if (
        expiry is None or initial_credit is None or initial_credit <= 0
        or quantity is None or quantity <= 0 or len(legs) != 4
        or set(roles) != expected_roles or not leg_terms_valid
        or width <= 0 or initial_credit >= width
    ):
        return {"action": "数据不足", "reasons": ["position_terms_invalid"]}
    dte = (expiry - as_of_date).days
    base = {
        "position_id": position.get("position_id"),
        "underlying_symbol": position.get("underlying_symbol"),
        "strategy": strategy,
        "expiration_date": expiry.isoformat(),
        "quantity": quantity,
        "legs": legs,
        "structure_width": round(width, 4),
    }
    if dte <= 0:
        return {**base, "action": "立即退出评估", "reasons": ["expiration_reached"], "dte": dte}

    short_put_role = "short_put" if strategy == "iron_condor" else "short_put_body"
    short_call_role = "short_call" if strategy == "iron_condor" else "short_call_body"
    short_put = roles[short_put_role]
    short_call = roles[short_call_role]
    put_strike = _num(short_put.get("strike"))
    call_strike = _num(short_call.get("strike"))
    if put_strike is None or call_strike is None:
        return {**base, "action": "数据不足", "reasons": ["position_terms_invalid"], "dte": dte}

    known_hard_reasons: list[str] = []
    risk_metrics: dict[str, Any] = {}
    if strategy == "iron_condor":
        if spot is not None and (spot <= put_strike or spot >= call_strike):
            known_hard_reasons.append("underlying_breached_short_strike")
    else:
        lower_break_even = put_strike - initial_credit
        upper_break_even = call_strike + initial_credit
        risk_metrics.update({
            "lower_break_even": round(lower_break_even, 4),
            "upper_break_even": round(upper_break_even, 4),
        })
        if spot is not None and (spot <= lower_break_even or spot >= upper_break_even):
            known_hard_reasons.append("underlying_breached_break_even")
        pin_buffer = width * cfg.pin_risk_width_fraction
        risk_metrics["pin_risk_buffer"] = round(pin_buffer, 4)
        if spot is not None and dte <= cfg.pin_risk_dte and abs(spot - put_strike) <= pin_buffer:
            known_hard_reasons.append("expiration_pin_risk")

    normalized_events = normalize_events(
        events, str(position.get("underlying_symbol") or ""), as_of_date=as_of_date.isoformat()
    )
    if normalized_events.get("status") == "ok":
        known_hard_reasons.extend(event_gates(
            strategy, expiry.isoformat(), normalized_events, as_of_date=as_of_date.isoformat()
        ))
    planned_exit = dte <= cfg.exit_dte

    def incomplete(reason: str, **metrics: Any) -> dict[str, Any]:
        if known_hard_reasons:
            action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
        elif planned_exit:
            action, reasons = "计划退出", ["expiration_window_reached"]
        else:
            action, reasons = "数据不足", [reason]
        row = {**base, **risk_metrics, "action": action, "reasons": reasons, "dte": dte, **metrics}
        if action != "数据不足":
            row["data_warnings"] = [reason]
        return row

    confirmation = position.get("account_confirmation")
    if confirmation is not None and confirmation.get("status") != "confirmed":
        return incomplete(confirmation.get("reason") or "account_position_unconfirmed")
    if not isinstance(chain, dict) or chain.get("status") != "ok":
        return incomplete("current_option_chain_unavailable")
    contracts = _contract_map(chain)
    current_legs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for leg in legs:
        current = contracts.get(str(leg.get("symbol") or "").upper())
        if not current:
            return incomplete("current_leg_quotes_unavailable")
        if (
            str(current.get("expiration_date") or "")[:10] != expiry.isoformat()
            or str(current.get("type") or "").lower() != str(leg.get("type") or "").lower()
            or _num(current.get("strike_price")) != _num(leg.get("strike"))
            or int(current.get("multiplier") or 0) != 100
        ):
            return incomplete("current_leg_contract_mismatch")
        current_legs.append((leg, current))

    quote_times = [_datetime((current.get("quote") or {}).get("timestamp")) for _, current in current_legs]
    if any(value is None for value in quote_times):
        return incomplete("current_quote_timestamp_invalid")
    valid_times = [value for value in quote_times if value is not None]
    expected_quote_date = expected_quote_date or as_of_date
    if any(value.astimezone(_NEW_YORK).date() < expected_quote_date for value in valid_times):
        return incomplete("current_quote_stale")
    if observed_at is not None and any(value > observed_at + dt.timedelta(minutes=1) for value in valid_times):
        return incomplete("current_quote_timestamp_future")
    if observed_at is not None:
        observed_et = observed_at.astimezone(_NEW_YORK)
        market_open = (
            observed_et.weekday() < 5
            and dt.time(9, 30) <= observed_et.time() <= dt.time(16, 15)
            and observed_et.date() == expected_quote_date
        )
        if market_open and any(
            observed_at - value > dt.timedelta(minutes=cfg.max_quote_age_minutes)
            for value in valid_times
        ):
            return incomplete("current_quote_stale")

    close_debit = 0.0
    short_deltas: dict[str, float] = {}
    for leg, current in current_legs:
        quote = current.get("quote") or {}
        bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
        if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
            return incomplete("current_quote_or_delta_invalid")
        if str(leg.get("side") or "").lower() in {"short", "sell", "sell_to_open"}:
            ask_size = _num(quote.get("ask_size"))
            delta = _num((current.get("greeks") or {}).get("delta"))
            if ask_size is None or ask_size <= 0 or delta is None:
                return incomplete("current_quote_or_delta_invalid")
            close_debit += ask
            short_deltas[str(leg.get("role"))] = delta
        else:
            bid_size = _num(quote.get("bid_size"))
            if bid_size is None or bid_size <= 0:
                return incomplete("current_quote_or_delta_invalid")
            close_debit -= bid
    if close_debit < 0 or close_debit > width:
        return incomplete("current_close_debit_invalid")
    if any(abs(delta) >= cfg.defense_delta for delta in short_deltas.values()):
        known_hard_reasons.append("short_delta_defense_triggered")
    if close_debit >= initial_credit * cfg.max_close_debit_multiple:
        known_hard_reasons.append("loss_multiple_defense_triggered")
    captured = (initial_credit - close_debit) / initial_credit * 100
    capture_target = captured >= cfg.take_profit_pct
    data_warnings: list[str] = []
    if normalized_events.get("status") != "ok":
        data_warnings.append("event_calendar_unavailable")
    if spot is None:
        data_warnings.append("underlying_spot_unavailable")
    if data_warnings and not (known_hard_reasons or planned_exit or capture_target):
        return incomplete(data_warnings[0], natural_close_debit=round(close_debit, 4))
    if known_hard_reasons:
        action, reasons = "立即退出评估", sorted(set(known_hard_reasons))
    elif planned_exit:
        action, reasons = "计划退出", ["expiration_window_reached"]
    elif capture_target:
        action, reasons = "止盈退出", ["profit_target_reached"]
    else:
        action, reasons = "继续持有", ["lifecycle_conditions_normal"]
    worst_delta = max(short_deltas.values(), key=abs)
    return {
        **base, **risk_metrics,
        "action": action,
        "reasons": reasons,
        "data_warnings": sorted(set(data_warnings)),
        "dte": dte,
        "spot": round(float(spot), 4) if spot is not None else None,
        "short_delta": round(worst_delta, 6),
        "short_deltas": {role: round(delta, 6) for role, delta in sorted(short_deltas.items())},
        "initial_credit": round(initial_credit, 4),
        "natural_close_debit": round(close_debit, 4),
        "profit_capture_pct": round(captured, 2),
        "estimated_open_profit": round((initial_credit - close_debit) * 100 * quantity, 2),
        "option_feed": chain.get("feed"),
        "account_confirmation": confirmation,
    }


def review_portfolio(payload: dict[str, Any], cfg: LifecycleConfig) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("lifecycle input must be an object")
    as_of = _market_date(payload.get("as_of"))
    observed_at = _datetime(payload.get("as_of"))
    positions = payload.get("positions")
    chains = payload.get("option_chains")
    events = payload.get("events")
    spots = payload.get("underlying_spots")
    if as_of is None or not isinstance(positions, list):
        raise ValueError("as_of and positions are required")
    if not isinstance(chains, dict) or not isinstance(events, dict) or not isinstance(spots, dict):
        raise ValueError("option_chains, events and underlying_spots are required")
    rows = []
    expected_quote_date = _date(payload.get("expected_quote_session"))
    expected_quote_sessions = payload.get("expected_quote_sessions") or {}
    as_of_text = str(payload.get("as_of") or "")
    if expected_quote_date is None and "T" not in as_of_text:
        expected_quote_date = as_of
    elif expected_quote_date is None and observed_at is not None:
        observed_et = observed_at.astimezone(_NEW_YORK)
        expected_quote_date = observed_et.date()
        if observed_et.time() < dt.time(9, 30):
            expected_quote_date -= dt.timedelta(days=1)
        while expected_quote_date.weekday() >= 5:
            expected_quote_date -= dt.timedelta(days=1)
    for position in positions:
        symbol = str((position or {}).get("underlying_symbol") or "").upper()
        position_quote_date = (
            _date(expected_quote_sessions.get(symbol))
            if isinstance(expected_quote_sessions, dict) else None
        ) or expected_quote_date
        row = review_position(
            position or {}, chains.get(symbol) or {}, events.get(symbol),
            spot=_num(spots.get(symbol)), as_of_date=as_of, cfg=cfg,
            observed_at=observed_at, expected_quote_date=position_quote_date,
        )
        # Every branch, including data failures, remains attributable and auditable.
        row.setdefault("position_id", (position or {}).get("position_id"))
        row.setdefault("underlying_symbol", (position or {}).get("underlying_symbol"))
        row.setdefault("strategy", (position or {}).get("strategy"))
        row.setdefault("quantity", (position or {}).get("quantity"))
        for key in (
            "expiration_date", "short_strike", "long_strike",
            "short_leg_symbol", "long_leg_symbol", "legs", "structure_width",
        ):
            row.setdefault(key, (position or {}).get(key))
        rows.append(row)
    order = {"立即退出评估": 0, "计划退出": 1, "止盈退出": 2, "数据不足": 3, "继续持有": 4}
    rows.sort(key=lambda row: (order.get(row.get("action"), 9), str(row.get("position_id") or "")))
    return {
        "status": "ok",
        "as_of": payload.get("as_of"),
        "config": cfg.to_dict(),
        "counts": {
            "positions": len(rows),
            "exit_review": sum(row.get("action") == "立即退出评估" for row in rows),
            "planned_exit": sum(row.get("action") in {"计划退出", "止盈退出"} for row in rows),
            "data_insufficient": sum(row.get("action") == "数据不足" for row in rows),
            "data_warning_positions": sum(bool(row.get("data_warnings")) for row in rows),
        },
        "positions": rows,
    }
