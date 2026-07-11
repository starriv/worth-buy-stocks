"""Deterministic next-open single-symbol simulation.

Two exit families share the same next-open entry rule:

- fixed horizon: hold exactly ``horizon`` sessions, exit at the next open;
- managed: invest the suggested allocation and execute the signal's initial
  stop, 1R break-even protection, delayed close-anchored trailing stop, and
  close-derived next-open exit signals on a daily-bar approximation.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics as py_stats
from typing import Any, Mapping, Sequence

from .universe import BANKRUPT


POLICIES = ("rank_only", "disciplined_verdict")
MANAGED_TRAIL_FALLBACK_PCT = 8.0
STATEFUL_RECOVERY_SESSIONS = 63
STATEFUL_LIFECYCLE_STATUS = "stateful_experimental_rejected"


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _drawdown(values: Sequence[float]) -> list[float]:
    peak = 0.0
    output = []
    for value in values:
        peak = max(peak, value)
        output.append((value / peak - 1) * 100 if peak else 0.0)
    return output


def _curve_metrics(curve: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not curve:
        return {
            "total_return_pct": None, "cagr_pct": None,
            "annualized_volatility_pct": None, "sharpe_rf0": None,
            "max_drawdown_pct": None,
        }
    values = [float(point["equity"]) for point in curve]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values)) if values[index - 1]]
    days = max(
        (dt.date.fromisoformat(str(curve[-1]["date"])) - dt.date.fromisoformat(str(curve[0]["date"]))).days,
        1,
    )
    years = days / 365.25
    total = values[-1] / values[0] - 1 if values[0] else None
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if values[0] and years > 0 else None
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        std = py_stats.stdev(returns)
        annual_vol = std * math.sqrt(252)
        sharpe = mean / std * math.sqrt(252) if std else None
    else:
        annual_vol = sharpe = None
    drawdowns = _drawdown(values)
    return {
        "total_return_pct": _round(total * 100 if total is not None else None, 4),
        "cagr_pct": _round(cagr * 100 if cagr is not None else None, 4),
        "annualized_volatility_pct": _round(annual_vol * 100 if annual_vol is not None else None, 4),
        "sharpe_rf0": _round(sharpe, 4),
        "max_drawdown_pct": _round(min(drawdowns) if drawdowns else None, 4),
    }


def _qualifies(record: Mapping[str, Any], policy: str) -> bool:
    if record.get("status") != "ok":
        return False
    if policy == "rank_only":
        raw = record.get("raw_composite")
        return bool(record.get("core_data_complete") and isinstance(raw, (int, float)) and raw >= 75)
    if policy == "disciplined_verdict":
        return record.get("verdict") == "是"
    raise ValueError(f"unknown policy: {policy}")


def _bankrupt_tail_dates(
    symbol: str,
    calendar: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return post-history sessions that receive the frozen zero terminal mark."""

    if symbol not in BANKRUPT or not rows:
        return []
    last_date = str(rows[-1]["t"])[:10]
    return [date for date in calendar if date > last_date]


def simulate_scenario(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    policy: str,
    horizon: int,
    cost_bps: float,
) -> dict[str, Any]:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    symbol = str(replay["symbol"])
    calendar = list(replay.get("calendar") or [])
    if not calendar:
        raise ValueError("replay calendar is empty")
    rows = [
        dict(row) for row in bars.get(symbol, [])
        if calendar[0] <= str(row["t"])[:10] <= calendar[-1]
    ]
    rows.sort(key=lambda row: str(row["t"]))
    timeline = {str(row["signal_date"]): row for row in replay.get("timeline") or []}
    one_way = cost_bps / 10_000.0
    cash = 1.0
    shares = 0.0
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    total_cost = 0.0
    exposed_sessions = 0

    for index, bar in enumerate(rows):
        date = str(bar["t"])[:10]
        open_price = float(bar["o"])

        if position is not None and index == position["exit_index"]:
            gross_value = shares * open_price
            exit_cost = gross_value * one_way
            total_cost += exit_cost
            cash = gross_value - exit_cost
            gross_return = open_price / position["entry_price"] - 1
            net_return = (open_price / position["entry_price"]) * (1 - one_way) ** 2 - 1
            trades.append({
                **position["trade"],
                "exit_date": date,
                "exit_price": _round(open_price, 4),
                "holding_sessions": horizon,
                "gross_return_pct": _round(gross_return * 100, 4),
                "net_return_pct": _round(net_return * 100, 4),
                "exit_cost_equity": _round(exit_cost),
                "status": "closed",
            })
            shares = 0.0
            position = None

        if pending is not None and position is None and index == pending["entry_index"]:
            entry_cost = cash * one_way
            total_cost += entry_cost
            shares = (cash - entry_cost) / open_price
            cash = 0.0
            position = {
                "entry_price": open_price,
                "exit_index": index + horizon,
                "trade": {
                    "trade_id": f"{policy}_{horizon}_{pending['signal_date']}",
                    "policy": policy,
                    "horizon": horizon,
                    "signal_date": pending["signal_date"],
                    "entry_date": date,
                    "entry_price": _round(open_price, 4),
                    "entry_cost_equity": _round(entry_cost),
                },
            }
            pending = None

        if position is not None:
            exposed_sessions += 1
            equity = shares * float(bar["c"])
        else:
            equity = cash
        curve.append({
            "date": date,
            "equity": _round(equity),
            "in_position": position is not None,
        })

        record = timeline.get(date)
        if record and _qualifies(record, policy):
            if position is not None or pending is not None:
                skipped.append({
                    "signal_date": date,
                    "reason": "position_or_order_active",
                    "policy": policy,
                })
            elif index + 1 >= len(rows):
                skipped.append({
                    "signal_date": date,
                    "reason": "next_open_unavailable",
                    "policy": policy,
                })
            else:
                pending = {"signal_date": date, "entry_index": index + 1}

    if pending is not None:
        skipped.append({
            "signal_date": pending["signal_date"],
            "reason": "next_open_unavailable",
            "policy": policy,
        })
    bankrupt_tail = _bankrupt_tail_dates(symbol, calendar, rows)
    if bankrupt_tail and position is not None:
        entry_index = position["exit_index"] - horizon
        trades.append({
            **position["trade"],
            "exit_date": bankrupt_tail[0],
            "exit_price": 0.0,
            "exit_reason": "bankrupt_terminal",
            "holding_sessions": max(len(rows) - entry_index, 0),
            "gross_return_pct": -100.0,
            "net_return_pct": -100.0,
            "exit_cost_equity": 0.0,
            "status": "closed",
        })
        cash = 0.0
        shares = 0.0
        position = None
    for date in bankrupt_tail:
        curve.append({
            "date": date,
            "equity": _round(cash),
            "in_position": False,
        })
    if position is not None:
        last = rows[-1]
        mark = shares * float(last["c"])
        unrealized = mark / (
            position["entry_price"] * shares / max(1 - one_way, 1e-12)
        ) - 1 if shares else None
        trades.append({
            **position["trade"],
            "exit_date": None,
            "exit_price": None,
            "holding_sessions": max(len(rows) - (position["exit_index"] - horizon), 0),
            "gross_return_pct": None,
            "net_return_pct": _round(unrealized * 100 if unrealized is not None else None, 4),
            "exit_cost_equity": None,
            "status": "open",
            "mark_date": str(last["t"])[:10],
            "mark_price": _round(float(last["c"]), 4),
        })
    return _assemble_scenario(
        scenario=f"{policy}_{horizon}", policy=policy, horizon=horizon,
        cost_bps=cost_bps, trades=trades, skipped=skipped, curve=curve,
        total_cost=total_cost, exposed_sessions=exposed_sessions,
    )


def _assemble_scenario(
    *,
    scenario: str,
    policy: str,
    horizon: int | None,
    cost_bps: float,
    trades: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    curve: list[dict[str, Any]],
    total_cost: float,
    exposed_sessions: int,
    lifecycle: str | None = None,
    state_transitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    drawdowns = _drawdown([float(point["equity"]) for point in curve])
    for point, drawdown in zip(curve, drawdowns):
        point["drawdown_pct"] = _round(drawdown, 4)
    closed = [trade for trade in trades if trade["status"] == "closed"]
    returns = [float(trade["net_return_pct"]) for trade in closed]
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    metrics = {
        **_curve_metrics(curve),
        "trade_count": len(closed),
        "open_trade_count": sum(trade["status"] == "open" for trade in trades),
        "win_rate_pct": _round(len(winners) / len(closed) * 100, 2) if closed else None,
        "profit_factor": _round(sum(winners) / abs(sum(losers)), 4) if losers else None,
        "average_trade_pct": _round(sum(returns) / len(returns), 4) if returns else None,
        "median_trade_pct": _round(float(py_stats.median(returns)), 4) if returns else None,
        "exposure_pct": _round(exposed_sessions / len(curve) * 100, 2) if curve else None,
        "turnover_sides": len(closed) * 2 + sum(trade["status"] == "open" for trade in trades),
        "total_cost_equity": _round(total_cost),
        "skipped_signals": len(skipped),
    }
    result = {
        "scenario": scenario,
        "policy": policy,
        "horizon": horizon,
        "cost_bps_oneway": cost_bps,
        "trades": trades,
        "skipped_signals": skipped,
        "equity_curve": curve,
        "metrics": metrics,
    }
    if lifecycle is not None:
        result["lifecycle"] = lifecycle
    if state_transitions is not None:
        result["state_transitions"] = state_transitions
    return result


def _plan_exit_params(record: Mapping[str, Any]) -> dict[str, float | str] | None:
    """Extract the versioned managed-position plan from a timeline record."""
    plan = record.get("trade_plan")
    if not isinstance(plan, Mapping):
        return None
    stop = plan.get("stop_loss_price")
    if not isinstance(stop, (int, float)) or isinstance(stop, bool):
        return None
    stop = float(stop)
    if not math.isfinite(stop) or stop <= 0:
        return None
    trail = plan.get("trailing_stop_pct")
    if not isinstance(trail, (int, float)) or isinstance(trail, bool) \
            or not math.isfinite(float(trail)) or not 0 < float(trail) < 100:
        trail = MANAGED_TRAIL_FALLBACK_PCT

    def positive(name: str, default: float) -> float:
        value = plan.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(float(value)) and float(value) > 0:
            return float(value)
        return default

    allocation = record.get("suggested_position_pct")
    if not isinstance(allocation, (int, float)) or isinstance(allocation, bool) \
            or not math.isfinite(float(allocation)):
        allocation = 100.0
    allocation = min(max(float(allocation), 0.0), 100.0)
    timing = record.get("entry_timing") or {}
    return {
        "stop_loss": stop,
        "trailing_pct": float(trail),
        "break_even_trigger_r": positive("break_even_trigger_r", 1.0),
        "break_even_buffer_pct": positive("break_even_buffer_pct", 0.25),
        "trailing_activation_r": positive("trailing_activation_r", 1.0),
        "allocation_pct": allocation,
        "entry_timing": str(timing.get("classification") or "unknown"),
    }


def _stateful_reentry_trigger(
    record: Mapping[str, Any], previous_record: Mapping[str, Any] | None
) -> str | None:
    """Return the frozen point-in-time re-entry trigger, if any."""

    if record.get("status") != "ok" or not record.get("core_data_complete"):
        return None
    timing = record.get("entry_timing") or {}
    detail = record.get("entry_timing_detail") or {}
    classification = timing.get("classification") or detail.get("classification")
    if classification == "recovery_reversal":
        return "recovery_reversal"
    volume_pct = (record.get("confirmation") or {}).get("volume_pct")
    volume_ok = isinstance(volume_pct, (int, float)) and volume_pct >= 40
    if classification == "pullback_reversal" and volume_ok:
        return "pullback_reversal"
    previous_timing = (previous_record or {}).get("entry_timing") or {}
    previous_detail = (previous_record or {}).get("entry_timing_detail") or {}
    previous_class = (
        previous_timing.get("classification") or previous_detail.get("classification")
    )
    bounce = detail.get("bounce") or {}
    if (
        previous_class == "trend_broken"
        and classification == "trend_continuation"
        and bounce.get("bounce_day") is True
        and isinstance(bounce.get("signal_count"), (int, float))
        and bounce["signal_count"] >= 2
        and volume_ok
    ):
        return "trend_recovery"
    return None


def _simulate_managed_scenario(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    policy: str,
    cost_bps: float,
    stateful: bool,
) -> dict[str, Any]:
    """Next-open entry, exit by the signal's own trade plan.

    The scenario invests only the suggested position fraction and retains the
    rest as cash. Initial risk is active immediately; break-even and trailing
    protection activate only after the frozen R thresholds are reached by a
    completed close. A close-derived position-exit signal executes at the next
    open. Intraday stops use the previous completed session's protective level.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    symbol = str(replay["symbol"])
    calendar = list(replay.get("calendar") or [])
    if not calendar:
        raise ValueError("replay calendar is empty")
    rows = [
        dict(row) for row in bars.get(symbol, [])
        if calendar[0] <= str(row["t"])[:10] <= calendar[-1]
    ]
    rows.sort(key=lambda row: str(row["t"]))
    timeline = {str(row["signal_date"]): row for row in replay.get("timeline") or []}
    one_way = cost_bps / 10_000.0
    cash = 1.0
    shares = 0.0
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    total_cost = 0.0
    exposed_sessions = 0
    recovery_watch: dict[str, Any] | None = None
    pending_scale: dict[str, Any] | None = None
    state_transitions: list[dict[str, Any]] = []
    previous_record: Mapping[str, Any] | None = None
    previous_exit_class: str | None = None

    def _transition(
        date: str,
        from_state: str,
        to_state: str,
        reason: str,
        **details: Any,
    ) -> None:
        if not stateful:
            return
        state_transitions.append({
            "date": date,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            **details,
        })

    def _close_position(exit_price: float, date: str, index: int, reason: str) -> None:
        nonlocal cash, shares, position, total_cost, recovery_watch, pending_scale
        closed_position = position
        closed_shares = shares
        gross_value = shares * exit_price
        exit_cost = gross_value * one_way
        total_cost += exit_cost
        cash += gross_value - exit_cost
        weighted_entry = (
            closed_position["gross_entry_value"] / closed_shares
            if closed_shares else closed_position["entry_price"]
        )
        gross_return = exit_price / weighted_entry - 1
        net_return = (
            (gross_value - exit_cost) / closed_position["cost_basis_cash"] - 1
            if closed_position["cost_basis_cash"] else None
        )
        trade = {
            **position["trade"],
            "exit_date": date,
            "exit_price": _round(exit_price, 4),
            "exit_reason": reason,
            "holding_sessions": index - position["entry_index"] + 1,
            "gross_return_pct": _round(gross_return * 100, 4),
            "net_return_pct": _round(net_return * 100 if net_return is not None else None, 4),
            "exit_cost_equity": _round(exit_cost),
            "break_even_activated": position["break_even_active"],
            "max_protective_price": _round(position["max_protective_level"], 4),
            "status": "closed",
        }
        if stateful:
            trade.update({
                "weighted_entry_price": _round(weighted_entry, 4),
                "total_entry_cost_equity": _round(position["total_entry_cost"]),
                "position_adjustments": list(position["adjustments"]),
                "max_target_allocation_pct": _round(position["target_allocation_pct"], 2),
            })
        trades.append(trade)
        if stateful and reason != "bankrupt_terminal":
            recovery_watch = {
                "exit_date": date,
                "exit_index": index,
                "expires_index": index + STATEFUL_RECOVERY_SESSIONS,
                "prior_allocation_pct": float(position["target_allocation_pct"]),
                "prior_exit_reason": reason,
                "prior_trade_id": position["trade"]["trade_id"],
            }
            _transition(
                date, "long", "recovery_watch", reason,
                prior_trade_id=position["trade"]["trade_id"],
                target_allocation_pct=_round(position["target_allocation_pct"], 2),
            )
        elif stateful:
            recovery_watch = None
            _transition(date, "long", "flat", reason)
        pending_scale = None
        shares = 0.0
        position = None

    for index, bar in enumerate(rows):
        date = str(bar["t"])[:10]
        open_price = float(bar["o"])
        low_price = float(bar["l"])
        close_price = float(bar["c"])

        if position is not None and position.get("exit_index") == index:
            _close_position(
                open_price, date, index,
                f"signal_{position.get('exit_signal') or 'exit'}",
            )

        active_protection: tuple[str, float] | None = None
        if position is not None and index > position["entry_index"]:
            levels = {"stop_loss": position["stop_loss"]}
            if position["break_even_level"] is not None:
                levels["break_even_stop"] = position["break_even_level"]
            if position["trail_level"] is not None:
                levels["trailing_stop"] = position["trail_level"]
            reason, level = max(levels.items(), key=lambda item: item[1])
            if open_price <= level:
                _close_position(open_price, date, index, f"gap_open_{reason}")
            else:
                active_protection = (reason, level)

        if (
            stateful
            and pending_scale is not None
            and position is not None
            and index == pending_scale["execution_index"]
        ):
            target_pct = float(pending_scale["target_allocation_pct"])
            equity_open = cash + shares * open_price
            current_value = shares * open_price
            desired_value = equity_open * target_pct / 100.0
            allocation_value = min(max(desired_value - current_value, 0.0), cash)
            if allocation_value > 0:
                entry_cost = allocation_value * one_way
                added_shares = (allocation_value - entry_cost) / open_price
                total_cost += entry_cost
                cash -= allocation_value
                shares += added_shares
                position["cost_basis_cash"] += allocation_value
                position["gross_entry_value"] += added_shares * open_price
                position["total_entry_cost"] += entry_cost
                position["target_allocation_pct"] = target_pct
                adjustment = {
                    "signal_date": pending_scale["signal_date"],
                    "execution_date": date,
                    "price": _round(open_price, 4),
                    "target_allocation_pct": _round(target_pct, 2),
                    "added_notional_equity": _round(allocation_value),
                    "cost_equity": _round(entry_cost),
                }
                position["adjustments"].append(adjustment)
                _transition(
                    date, "long", "long", "restore_allocation",
                    target_allocation_pct=_round(target_pct, 2),
                    added_notional_equity=_round(allocation_value),
                )
            pending_scale = None

        if position is not None and active_protection is not None:
            reason, level = active_protection
            if low_price <= level:
                _close_position(level, date, index, reason)

        if pending is not None and position is None and index == pending["entry_index"]:
            plan = pending["plan"]
            stop_loss = float(plan["stop_loss"])
            if open_price <= stop_loss:
                skipped.append({
                    "signal_date": pending["signal_date"],
                    "reason": "open_at_or_below_stop",
                    "policy": policy,
                })
                _transition(
                    date,
                    "reentry_pending" if pending.get("entry_kind") == "reentry" else "entry_pending",
                    "recovery_watch" if recovery_watch is not None else "flat",
                    "open_at_or_below_stop",
                )
            else:
                allocation_value = cash * float(plan["allocation_pct"]) / 100.0
                entry_cost = allocation_value * one_way
                total_cost += entry_cost
                shares = (allocation_value - entry_cost) / open_price
                cash -= allocation_value
                initial_risk = open_price - stop_loss
                trade_id = (
                    f"{policy}_stateful_{pending['signal_date']}"
                    if stateful else f"{policy}_managed_{pending['signal_date']}"
                )
                position = {
                    "entry_price": open_price,
                    "entry_index": index,
                    "stop_loss": stop_loss,
                    "initial_risk": initial_risk,
                    "trailing_pct": float(plan["trailing_pct"]),
                    "break_even_trigger_r": float(plan["break_even_trigger_r"]),
                    "break_even_buffer_pct": float(plan["break_even_buffer_pct"]),
                    "trailing_activation_r": float(plan["trailing_activation_r"]),
                    "break_even_active": False,
                    "break_even_level": None,
                    "peak_close": None,
                    "trail_level": None,
                    "max_protective_level": stop_loss,
                    "trend_qualified_once": plan["entry_timing"] != "recovery_reversal",
                    "exit_index": None,
                    "exit_signal": None,
                    "cost_basis_cash": allocation_value,
                    "gross_entry_value": shares * open_price,
                    "total_entry_cost": entry_cost,
                    "target_allocation_pct": float(plan["allocation_pct"]),
                    "adjustments": [],
                    "trade": {
                        "trade_id": trade_id,
                        "policy": policy,
                        "horizon": None,
                        "signal_date": pending["signal_date"],
                        "entry_date": date,
                        "entry_price": _round(open_price, 4),
                        "entry_cost_equity": _round(entry_cost),
                        "entry_allocation_pct": _round(float(plan["allocation_pct"]), 2),
                        "entry_notional_equity": _round(allocation_value),
                        "entry_timing": plan["entry_timing"],
                        "stop_loss_price": _round(stop_loss, 4),
                        "trailing_stop_pct": _round(float(plan["trailing_pct"]), 4),
                        "break_even_trigger_r": _round(float(plan["break_even_trigger_r"]), 2),
                        "break_even_buffer_pct": _round(float(plan["break_even_buffer_pct"]), 2),
                    },
                }
                if stateful:
                    position["trade"].update({
                        "entry_kind": pending.get("entry_kind", "initial"),
                        "reentry_trigger": pending.get("reentry_trigger"),
                        "prior_exit_reason": (
                            (pending.get("recovery_watch") or {}).get("prior_exit_reason")
                        ),
                        "prior_trade_id": (
                            (pending.get("recovery_watch") or {}).get("prior_trade_id")
                        ),
                        "recovery_wait_sessions": pending.get("recovery_wait_sessions"),
                    })
                    _transition(
                        date,
                        "reentry_pending" if pending.get("entry_kind") == "reentry" else "entry_pending",
                        "long",
                        pending.get("reentry_trigger") or "standard_entry",
                        trade_id=trade_id,
                        target_allocation_pct=_round(float(plan["allocation_pct"]), 2),
                    )
                    recovery_watch = None
                # 入场日仍可能触发初始止损（开盘价已高于止损，仅盘中触及）。
                if low_price <= position["stop_loss"]:
                    _close_position(position["stop_loss"], date, index, "stop_loss")
            pending = None

        if position is not None:
            exposed_sessions += 1
            equity = cash + shares * close_price
            if position["peak_close"] is None or close_price > position["peak_close"]:
                position["peak_close"] = close_price
            gain = position["peak_close"] - position["entry_price"]
            risk = position["initial_risk"]
            if risk > 0 and gain >= risk * position["break_even_trigger_r"]:
                position["break_even_active"] = True
                position["break_even_level"] = position["entry_price"] * (
                    1 + position["break_even_buffer_pct"] / 100.0
                )
            if risk > 0 and gain >= risk * position["trailing_activation_r"]:
                candidate = position["peak_close"] * (
                    1 - position["trailing_pct"] / 100.0
                )
                position["trail_level"] = max(
                    candidate, position["trail_level"] or candidate
                )
            active_levels = [position["stop_loss"]]
            if position["break_even_level"] is not None:
                active_levels.append(position["break_even_level"])
            if position["trail_level"] is not None:
                active_levels.append(position["trail_level"])
            position["max_protective_level"] = max(
                position["max_protective_level"], *active_levels
            )
        else:
            equity = cash
        curve.append({
            "date": date,
            "equity": _round(equity),
            "in_position": position is not None,
        })

        record = timeline.get(date)
        if (
            stateful
            and recovery_watch is not None
            and position is None
            and index > recovery_watch["expires_index"]
        ):
            _transition(
                date, "recovery_watch", "flat", "recovery_window_expired",
                prior_trade_id=recovery_watch["prior_trade_id"],
            )
            recovery_watch = None
        if record and position is not None:
            if not any("MA60" in str(reason) for reason in record.get("risk_gates") or []):
                position["trend_qualified_once"] = True
            exit_signal = record.get("position_exit") or {}
            exit_class = exit_signal.get("classification")
            can_exit = exit_signal.get("action") == "exit_next_open"
            if stateful and exit_class == "trend_break":
                can_exit = False
                if previous_exit_class != "trend_break":
                    _transition(
                        date, "long", "long", "trend_break_risk_watch",
                        trade_id=position["trade"]["trade_id"],
                    )
            elif exit_class == "trend_break" and not position["trend_qualified_once"]:
                can_exit = False
            if can_exit and position.get("exit_index") is None:
                if index + 1 < len(rows):
                    position["exit_index"] = index + 1
                    position["exit_signal"] = exit_class
                else:
                    skipped.append({
                        "signal_date": date,
                        "reason": "next_open_unavailable",
                        "policy": policy,
                    })
            if (
                stateful
                and _qualifies(record, policy)
                and position.get("exit_index") is None
                and pending_scale is None
                and index + 1 < len(rows)
            ):
                target = record.get("suggested_position_pct")
                if (
                    isinstance(target, (int, float))
                    and not isinstance(target, bool)
                    and math.isfinite(float(target))
                    and float(target) > position["target_allocation_pct"]
                ):
                    pending_scale = {
                        "signal_date": date,
                        "execution_index": index + 1,
                        "target_allocation_pct": min(float(target), 100.0),
                    }
                    _transition(
                        date, "long", "long", "restore_allocation_pending",
                        target_allocation_pct=_round(min(float(target), 100.0), 2),
                    )
        qualifies = bool(record and _qualifies(record, policy))
        reentry_trigger = (
            _stateful_reentry_trigger(record, previous_record)
            if stateful and record and recovery_watch is not None else None
        )
        wants_entry = qualifies or reentry_trigger is not None
        if record and wants_entry:
            if position is not None or pending is not None:
                skipped.append({
                    "signal_date": date,
                    "reason": (
                        "position_or_order_active"
                        if not stateful else "stateful_position_or_order_active"
                    ),
                    "policy": policy,
                })
            elif index + 1 >= len(rows):
                skipped.append({
                    "signal_date": date,
                    "reason": "next_open_unavailable",
                    "policy": policy,
                })
            else:
                plan = _plan_exit_params(record)
                if plan is None:
                    skipped.append({
                        "signal_date": date,
                        "reason": "plan_exit_unavailable",
                        "policy": policy,
                    })
                else:
                    entry_kind = "reentry" if stateful and recovery_watch is not None else "initial"
                    watch_snapshot = dict(recovery_watch) if recovery_watch is not None else None
                    if entry_kind == "reentry":
                        plan = dict(plan)
                        plan["allocation_pct"] = float(recovery_watch["prior_allocation_pct"])
                    pending = {
                        "signal_date": date,
                        "entry_index": index + 1,
                        "plan": plan,
                        "entry_kind": entry_kind,
                        "reentry_trigger": (
                            "standard_verdict" if qualifies and entry_kind == "reentry"
                            else reentry_trigger
                        ),
                        "recovery_watch": watch_snapshot,
                        "recovery_wait_sessions": (
                            index - recovery_watch["exit_index"]
                            if entry_kind == "reentry" else None
                        ),
                    }
                    _transition(
                        date,
                        "recovery_watch" if entry_kind == "reentry" else "flat",
                        "reentry_pending" if entry_kind == "reentry" else "entry_pending",
                        pending["reentry_trigger"] or "standard_entry",
                    )
        if record:
            previous_record = record
            previous_exit_class = (record.get("position_exit") or {}).get("classification")
        else:
            previous_exit_class = None

    if pending is not None:
        skipped.append({
            "signal_date": pending["signal_date"],
            "reason": "next_open_unavailable",
            "policy": policy,
        })
    if pending_scale is not None:
        skipped.append({
            "signal_date": pending_scale["signal_date"],
            "reason": "next_open_unavailable_for_allocation_restore",
            "policy": policy,
        })
    bankrupt_tail = _bankrupt_tail_dates(symbol, calendar, rows)
    if bankrupt_tail and position is not None:
        _close_position(
            0.0, bankrupt_tail[0], len(rows), "bankrupt_terminal"
        )
    for date in bankrupt_tail:
        curve.append({
            "date": date,
            "equity": _round(cash),
            "in_position": False,
        })
    if position is not None:
        last = rows[-1]
        mark = shares * float(last["c"])
        unrealized = (
            mark / position["cost_basis_cash"] - 1
            if shares and position["cost_basis_cash"] else None
        )
        trade = {
            **position["trade"],
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
            "holding_sessions": len(rows) - position["entry_index"],
            "gross_return_pct": None,
            "net_return_pct": _round(unrealized * 100 if unrealized is not None else None, 4),
            "exit_cost_equity": None,
            "break_even_activated": position["break_even_active"],
            "max_protective_price": _round(position["max_protective_level"], 4),
            "status": "open",
            "mark_date": str(last["t"])[:10],
            "mark_price": _round(float(last["c"]), 4),
        }
        if stateful:
            weighted_entry = (
                position["gross_entry_value"] / shares if shares else position["entry_price"]
            )
            trade.update({
                "weighted_entry_price": _round(weighted_entry, 4),
                "total_entry_cost_equity": _round(position["total_entry_cost"]),
                "position_adjustments": list(position["adjustments"]),
                "max_target_allocation_pct": _round(position["target_allocation_pct"], 2),
            })
        trades.append(trade)
    scenario_name = f"{policy}_stateful" if stateful else f"{policy}_managed"
    return _assemble_scenario(
        scenario=scenario_name, policy=policy, horizon=None,
        cost_bps=cost_bps, trades=trades, skipped=skipped, curve=curve,
        total_cost=total_cost, exposed_sessions=exposed_sessions,
        lifecycle=STATEFUL_LIFECYCLE_STATUS if stateful else None,
        state_transitions=state_transitions if stateful else None,
    )


def simulate_managed_scenario(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    policy: str,
    cost_bps: float,
) -> dict[str, Any]:
    """Run the frozen contract-1.3 managed baseline."""

    return _simulate_managed_scenario(
        replay, bars, policy=policy, cost_bps=cost_bps, stateful=False
    )


def simulate_stateful_managed_scenario(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    policy: str = "disciplined_verdict",
    cost_bps: float,
) -> dict[str, Any]:
    """Run the pre-registered experimental lifecycle with auditable re-entry."""

    if policy != "disciplined_verdict":
        raise ValueError("stateful managed currently requires disciplined_verdict")
    return _simulate_managed_scenario(
        replay, bars, policy=policy, cost_bps=cost_bps, stateful=True
    )


def build_benchmarks(
    replay: Mapping[str, Any], bars: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    symbol = str(replay["symbol"])
    calendar = list(replay.get("calendar") or [])
    symbol_rows = [
        row for row in bars.get(symbol, [])
        if calendar and calendar[0] <= str(row["t"])[:10] <= calendar[-1]
    ]
    if not symbol_rows:
        return {}
    comparison_start = str(symbol_rows[0]["t"])[:10]
    symbol_last = str(symbol_rows[-1]["t"])[:10]
    bankrupt_terminal = symbol in BANKRUPT and symbol_last < calendar[-1]
    comparison_end = calendar[-1] if bankrupt_terminal else symbol_last
    comparison_calendar = [
        date for date in calendar if comparison_start <= date <= comparison_end
    ]
    output: dict[str, Any] = {}
    for name in (symbol, "SPY"):
        by_date = {
            str(row["t"])[:10]: float(row["c"])
            for row in bars.get(name, [])
            if calendar and calendar[0] <= str(row["t"])[:10] <= calendar[-1]
        }
        dates = [date for date in comparison_calendar if date in by_date]
        if not dates:
            continue
        first = by_date[dates[0]]
        if name == symbol and bankrupt_terminal:
            curve = []
            for date in comparison_calendar:
                if date in by_date:
                    equity = by_date[date] / first
                elif date > symbol_last:
                    equity = 0.0
                else:
                    continue
                curve.append({"date": date, "equity": _round(equity)})
        else:
            curve = [
                {"date": date, "equity": _round(by_date[date] / first)}
                for date in dates
            ]
        drawdowns = _drawdown([float(point["equity"]) for point in curve])
        for point, drawdown in zip(curve, drawdowns):
            point["drawdown_pct"] = _round(drawdown, 4)
        output[name] = {
            "name": f"{name}_buy_and_hold",
            "curve": curve,
            "metrics": _curve_metrics(curve),
        }
    return output


def run_simulations(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    horizons: Sequence[int] = (21, 63),
    cost_bps: float = 10.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    scenarios = {
        f"{policy}_{horizon}": simulate_scenario(
            replay, bars, policy=policy, horizon=int(horizon), cost_bps=cost_bps
        )
        for policy in POLICIES
        for horizon in horizons
    }
    for policy in POLICIES:
        scenario = simulate_managed_scenario(
            replay, bars, policy=policy, cost_bps=cost_bps
        )
        scenarios[scenario["scenario"]] = scenario
    stateful = simulate_stateful_managed_scenario(
        replay, bars, policy="disciplined_verdict", cost_bps=cost_bps
    )
    scenarios[stateful["scenario"]] = stateful
    return scenarios, build_benchmarks(replay, bars)
