"""Research-only pragmatic staged-entry lifecycle.

The candidate deliberately separates a small, fast probe from later
confirmation.  It consumes only point-in-time replay fields and executes every
decision at the following open.  It is not part of the production scoring
contract unless the pre-registered holdout promotes it.
"""
from __future__ import annotations

import math
import dataclasses
from typing import Any, Mapping, Sequence

from .simulation import _assemble_scenario, _plan_exit_params, _round


@dataclasses.dataclass(frozen=True)
class PragmaticConfig:
    name: str
    probe_allocation_pct: float
    confirmed_allocation_pct: float
    standard_allocation_cap_pct: float | None
    exit_unconfirmed_probe: bool
    defer_trend_break_until_qualified: bool


V0_CONFIG = PragmaticConfig(
    name="pragmatic_staged_entry_v0",
    probe_allocation_pct=10.0,
    confirmed_allocation_pct=30.0,
    standard_allocation_cap_pct=50.0,
    exit_unconfirmed_probe=False,
    defer_trend_break_until_qualified=False,
)
V1_CONFIG = PragmaticConfig(
    name="pragmatic_staged_entry_v1",
    probe_allocation_pct=5.0,
    confirmed_allocation_pct=30.0,
    standard_allocation_cap_pct=None,
    exit_unconfirmed_probe=True,
    defer_trend_break_until_qualified=True,
)
SCENARIO = V0_CONFIG.name
COOLDOWN_SESSIONS = 2
FAILED_PROBE_WINDOW = 63
MAX_FAILED_PROBES = 2


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _only_ma60_gate(record: Mapping[str, Any]) -> bool:
    gates = list(record.get("risk_gates") or [])
    return all(reason == "价格跌破 MA60" for reason in gates)


def _eligible(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("status") == "ok"
        and record.get("core_data_complete")
        and _only_ma60_gate(record)
        and all(
            reason == "价格跌破 MA60"
            for reason in record.get("blocking_reasons") or []
        )
    )


def probe_trigger(record: Mapping[str, Any]) -> bool:
    """Frozen v0 probe rule; exported for deterministic tests and reporting."""

    if not _eligible(record):
        return False
    timing = record.get("entry_timing") or {}
    detail = record.get("entry_timing_detail") or {}
    pullback = detail.get("pullback") or {}
    bounce = detail.get("bounce") or {}
    confirmation = record.get("confirmation") or {}
    depth = _number(pullback.get("depth_from_30d_high_pct"))
    since_low = _number(pullback.get("bars_since_recent_low"))
    signals = _number(bounce.get("signal_count"))
    volume = _number(confirmation.get("volume_pct"))
    extension = _number(detail.get("ext_atr_above_ma20"))
    return bool(
        timing.get("classification") == "pullback_reversal"
        and depth is not None and depth <= -4.0
        and since_low is not None and 1 <= since_low <= 5
        and bounce.get("bounce_day") is True
        and signals is not None and signals >= 2
        and volume is not None and volume >= 40
        and extension is not None and extension <= 0.75
    )


def confirmation_trigger(record: Mapping[str, Any]) -> bool:
    """Frozen v0 follow-through rule.  Age is checked by the state machine."""

    if not _eligible(record):
        return False
    detail = record.get("entry_timing_detail") or {}
    bounce = detail.get("bounce") or {}
    signals = _number(bounce.get("signal_count"))
    volume = _number((record.get("confirmation") or {}).get("volume_pct"))
    return bool(
        bounce.get("up_day") is True
        and signals is not None and signals >= 3
        and volume is not None and volume >= 40
    )


def _standard_trigger(record: Mapping[str, Any]) -> bool:
    timing = record.get("entry_timing") or {}
    return bool(
        record.get("status") == "ok"
        and record.get("core_data_complete")
        and record.get("verdict") == "是"
        and timing.get("classification") != "overextended"
    )


def _entry_plan(
    record: Mapping[str, Any], *, kind: str, config: PragmaticConfig
) -> dict[str, float | str] | None:
    suggested = _number(record.get("suggested_position_pct"))
    chase = _number((record.get("trade_plan") or {}).get("max_chase_price"))
    if suggested is None or suggested <= 0 or chase is None or chase <= 0:
        return None
    base = _plan_exit_params(record)
    if base is None:
        return None
    plan = dict(base)
    if kind == "probe":
        recent_low = _number(
            ((record.get("entry_timing_detail") or {}).get("pullback") or {}).get(
                "recent_low_10d"
            )
        )
        if recent_low is None or recent_low <= 0:
            return None
        plan["stop_loss"] = recent_low * 0.99
        plan["allocation_pct"] = min(config.probe_allocation_pct, suggested)
    elif kind == "standard":
        plan["allocation_pct"] = (
            suggested if config.standard_allocation_cap_pct is None
            else min(config.standard_allocation_cap_pct, suggested)
        )
    else:
        raise ValueError(f"unknown entry kind: {kind}")
    plan["max_chase_price"] = chase
    return plan


def _confirmation_target(
    record: Mapping[str, Any], config: PragmaticConfig
) -> tuple[float, float] | None:
    suggested = _number(record.get("suggested_position_pct"))
    chase = _number((record.get("trade_plan") or {}).get("max_chase_price"))
    if suggested is None or suggested <= 0 or chase is None or chase <= 0:
        return None
    return min(config.confirmed_allocation_pct, suggested), chase


def simulate_pragmatic_scenario(
    replay: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    cost_bps: float,
    config: PragmaticConfig = V0_CONFIG,
) -> dict[str, Any]:
    """Run the pre-registered staged lifecycle with auditable transitions."""

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
    transitions: list[dict[str, Any]] = []
    total_cost = 0.0
    exposed_sessions = 0
    cooldown_until = -1
    failed_probe_indices: list[int] = []
    lifecycle_state = "flat"
    last_exit_had_position = False

    def transition(date: str, to_state: str, reason: str, **details: Any) -> None:
        nonlocal lifecycle_state
        transitions.append({
            "date": date,
            "from_state": lifecycle_state,
            "to_state": to_state,
            "reason": reason,
            **details,
        })
        lifecycle_state = to_state

    def close_position(exit_price: float, date: str, index: int, reason: str) -> None:
        nonlocal cash, shares, position, total_cost, cooldown_until, last_exit_had_position
        assert position is not None
        gross_value = shares * exit_price
        exit_cost = gross_value * one_way
        total_cost += exit_cost
        cash += gross_value - exit_cost
        net_return = (
            (gross_value - exit_cost) / position["cost_basis_cash"] - 1
            if position["cost_basis_cash"] else None
        )
        trades.append({
            **position["trade"],
            "exit_date": date,
            "exit_price": _round(exit_price, 4),
            "exit_reason": reason,
            "holding_sessions": index - position["entry_index"] + 1,
            "net_return_pct": _round(net_return * 100 if net_return is not None else None, 4),
            "gross_return_pct": _round((exit_price / position["entry_price"] - 1) * 100, 4),
            "exit_cost_equity": _round(exit_cost),
            "position_adjustments": list(position["adjustments"]),
            "max_target_allocation_pct": _round(position["target_allocation_pct"], 2),
            "mfe_pct": _round((position["max_high"] / position["entry_price"] - 1) * 100, 4),
            "mae_pct": _round((position["min_low"] / position["entry_price"] - 1) * 100, 4),
            "status": "closed",
        })
        if position["entry_kind"] == "probe" and reason in {
            "stop_loss", "gap_open_stop_loss", "break_even_stop", "gap_open_break_even_stop"
        }:
            failed_probe_indices.append(index)
        transition(
            date, "exit", reason,
            trade_id=position["trade"]["trade_id"],
            exit_price=_round(exit_price, 4),
        )
        cooldown_until = index + COOLDOWN_SESSIONS
        transition(date, "cooldown", "post_exit_cooldown", until_index=cooldown_until)
        last_exit_had_position = True
        shares = 0.0
        position = None

    def active_failed_probes(index: int) -> int:
        failed_probe_indices[:] = [
            item for item in failed_probe_indices if index - item < FAILED_PROBE_WINDOW
        ]
        return len(failed_probe_indices)

    for index, bar in enumerate(rows):
        date = str(bar["t"])[:10]
        open_price = float(bar["o"])
        high_price = float(bar["h"])
        low_price = float(bar["l"])
        close_price = float(bar["c"])

        if position is not None and position.get("exit_index") == index:
            close_position(open_price, date, index, f"signal_{position['exit_signal']}")

        if position is not None and index > position["entry_index"]:
            levels = {"stop_loss": position["stop_loss"]}
            if position["break_even_level"] is not None:
                levels["break_even_stop"] = position["break_even_level"]
            if position["trail_level"] is not None:
                levels["trailing_stop"] = position["trail_level"]
            reason, level = max(levels.items(), key=lambda item: item[1])
            if open_price <= level:
                close_position(open_price, date, index, f"gap_open_{reason}")
            elif low_price <= level:
                close_position(level, date, index, reason)

        if pending is not None and index == pending["execution_index"]:
            action = pending["action"]
            if open_price > pending["max_chase_price"]:
                skipped.append({
                    "signal_date": pending["signal_date"],
                    "reason": "open_above_max_chase",
                    "action": action,
                    "open_price": _round(open_price, 4),
                    "max_chase_price": _round(pending["max_chase_price"], 4),
                })
                transition(
                    date,
                    "probe" if position is not None else (
                        "reentry_eligible" if last_exit_had_position else "flat"
                    ),
                    "max_chase_blocked",
                    action=action,
                )
            elif action == "entry" and position is None:
                plan = pending["plan"]
                stop_loss = float(plan["stop_loss"])
                if open_price <= stop_loss:
                    skipped.append({
                        "signal_date": pending["signal_date"],
                        "reason": "open_at_or_below_stop",
                        "action": action,
                    })
                    transition(date, "flat", "open_at_or_below_stop")
                else:
                    allocation_value = cash * float(plan["allocation_pct"]) / 100.0
                    entry_cost = allocation_value * one_way
                    total_cost += entry_cost
                    shares = (allocation_value - entry_cost) / open_price
                    cash -= allocation_value
                    entry_kind = pending["entry_kind"]
                    trade_id = f"{config.name}_{pending['signal_date']}"
                    position = {
                        "entry_kind": entry_kind,
                        "entry_price": open_price,
                        "entry_index": index,
                        "probe_signal_index": pending["signal_index"],
                        "stop_loss": stop_loss,
                        "initial_risk": open_price - stop_loss,
                        "trailing_pct": float(plan["trailing_pct"]),
                        "break_even_trigger_r": float(plan["break_even_trigger_r"]),
                        "break_even_buffer_pct": float(plan["break_even_buffer_pct"]),
                        "trailing_activation_r": float(plan["trailing_activation_r"]),
                        "break_even_level": None,
                        "trail_level": None,
                        "peak_close": close_price,
                        "max_high": high_price,
                        "min_low": low_price,
                        "exit_index": None,
                        "exit_signal": None,
                        "trend_qualified_once": entry_kind == "standard",
                        "cost_basis_cash": allocation_value,
                        "target_allocation_pct": float(plan["allocation_pct"]),
                        "adjustments": [],
                        "trade": {
                            "trade_id": trade_id,
                            "policy": config.name,
                            "signal_date": pending["signal_date"],
                            "entry_date": date,
                            "entry_price": _round(open_price, 4),
                            "entry_kind": entry_kind,
                            "entry_allocation_pct": _round(float(plan["allocation_pct"]), 2),
                            "entry_cost_equity": _round(entry_cost),
                            "stop_loss_price": _round(stop_loss, 4),
                            "max_chase_price": _round(float(plan["max_chase_price"]), 4),
                        },
                    }
                    transition(
                        date,
                        "probe" if entry_kind == "probe" else "confirmed",
                        f"{entry_kind}_executed",
                        trade_id=trade_id,
                        execution_price=_round(open_price, 4),
                        target_allocation_pct=_round(float(plan["allocation_pct"]), 2),
                        stop_loss_price=_round(stop_loss, 4),
                    )
                    if low_price <= stop_loss:
                        close_position(stop_loss, date, index, "stop_loss")
            elif action == "confirm" and position is not None:
                target_pct = float(pending["target_allocation_pct"])
                equity_open = cash + shares * open_price
                current_value = shares * open_price
                allocation_value = min(max(equity_open * target_pct / 100 - current_value, 0), cash)
                if allocation_value > 0:
                    entry_cost = allocation_value * one_way
                    added_shares = (allocation_value - entry_cost) / open_price
                    total_cost += entry_cost
                    cash -= allocation_value
                    shares += added_shares
                    position["cost_basis_cash"] += allocation_value
                    position["target_allocation_pct"] = target_pct
                    position["entry_kind"] = "confirmed"
                    adjustment = {
                        "signal_date": pending["signal_date"],
                        "execution_date": date,
                        "execution_price": _round(open_price, 4),
                        "max_chase_price": _round(float(pending["max_chase_price"]), 4),
                        "target_allocation_pct": _round(target_pct, 2),
                        "added_notional_equity": _round(allocation_value),
                        "cost_equity": _round(entry_cost),
                    }
                    position["adjustments"].append(adjustment)
                    transition(date, "confirmed", "follow_through_add_executed", **adjustment)
            pending = None

        if position is not None:
            position["max_high"] = max(position["max_high"], high_price)
            position["min_low"] = min(position["min_low"], low_price)
            position["peak_close"] = max(position["peak_close"], close_price)
            risk = position["initial_risk"]
            gain = position["peak_close"] - position["entry_price"]
            if risk > 0 and gain >= risk * position["break_even_trigger_r"]:
                position["break_even_level"] = position["entry_price"] * (
                    1 + position["break_even_buffer_pct"] / 100
                )
                if lifecycle_state == "confirmed":
                    transition(date, "protected", "one_r_protection_activated")
            if risk > 0 and gain >= risk * position["trailing_activation_r"]:
                candidate = position["peak_close"] * (1 - position["trailing_pct"] / 100)
                position["trail_level"] = max(candidate, position["trail_level"] or candidate)
            exposed_sessions += 1
            equity = cash + shares * close_price
        else:
            equity = cash
        curve.append({
            "date": date,
            "equity": _round(equity),
            "in_position": position is not None,
            "state": lifecycle_state,
            "allocation_pct": _round(shares * close_price / equity * 100, 2) if equity else 0.0,
        })

        if position is None and lifecycle_state == "cooldown" and index > cooldown_until:
            transition(date, "reentry_eligible", "cooldown_complete")

        record = timeline.get(date)
        if record and position is not None:
            if not any("MA60" in str(reason) for reason in record.get("risk_gates") or []):
                position["trend_qualified_once"] = True
            exit_signal = record.get("position_exit") or {}
            exit_class = exit_signal.get("classification") or "exit"
            can_exit = exit_signal.get("action") == "exit_next_open"
            if (
                config.defer_trend_break_until_qualified
                and exit_class == "trend_break"
                and not position["trend_qualified_once"]
            ):
                can_exit = False
            if can_exit and position["exit_index"] is None:
                if index + 1 < len(rows):
                    position["exit_index"] = index + 1
                    position["exit_signal"] = exit_class
                    transition(date, lifecycle_state, "exit_next_open_pending", exit_class=position["exit_signal"])
                else:
                    skipped.append({"signal_date": date, "reason": "next_open_unavailable", "action": "exit"})

        if record and pending is None and index + 1 < len(rows):
            if position is None and index > cooldown_until:
                plan = None
                entry_kind = None
                if _standard_trigger(record):
                    plan = _entry_plan(record, kind="standard", config=config)
                    entry_kind = "standard"
                elif probe_trigger(record) and active_failed_probes(index) < MAX_FAILED_PROBES:
                    plan = _entry_plan(record, kind="probe", config=config)
                    entry_kind = "probe"
                if plan is not None and entry_kind is not None:
                    pending = {
                        "action": "entry",
                        "entry_kind": entry_kind,
                        "signal_date": date,
                        "signal_index": index,
                        "execution_index": index + 1,
                        "max_chase_price": float(plan["max_chase_price"]),
                        "plan": plan,
                    }
                    transition(
                        date,
                        "probe_pending" if entry_kind == "probe" else "confirm_pending",
                        f"{entry_kind}_signal",
                        execution_date=str(rows[index + 1]["t"])[:10],
                    )
                elif probe_trigger(record) and active_failed_probes(index) >= MAX_FAILED_PROBES:
                    skipped.append({
                        "signal_date": date,
                        "reason": "failed_probe_budget_exhausted",
                        "action": "entry",
                    })
            elif (
                position is not None
                and lifecycle_state == "probe"
                and 2 <= index - position["probe_signal_index"] <= 5
                and confirmation_trigger(record)
                and position.get("exit_index") is None
            ):
                target = _confirmation_target(record, config)
                if target is not None and target[0] > position["target_allocation_pct"]:
                    pending = {
                        "action": "confirm",
                        "signal_date": date,
                        "execution_index": index + 1,
                        "max_chase_price": target[1],
                        "target_allocation_pct": target[0],
                    }
                    transition(
                        date, "confirm_pending", "follow_through_confirmed",
                        execution_date=str(rows[index + 1]["t"])[:10],
                        target_allocation_pct=_round(target[0], 2),
                    )
            elif (
                position is not None
                and lifecycle_state == "probe"
                and config.exit_unconfirmed_probe
                and index - position["probe_signal_index"] > 5
                and position.get("exit_index") is None
            ):
                position["exit_index"] = index + 1
                position["exit_signal"] = "probe_unconfirmed"
                transition(
                    date, "probe", "probe_confirmation_expired_exit_pending",
                    execution_date=str(rows[index + 1]["t"])[:10],
                )

    if pending is not None:
        skipped.append({
            "signal_date": pending["signal_date"],
            "reason": "next_open_unavailable",
            "action": pending["action"],
        })
    if position is not None:
        last = rows[-1]
        invested_mark = shares * float(last["c"])
        net_return = (
            invested_mark / position["cost_basis_cash"] - 1
            if position["cost_basis_cash"] else None
        )
        trades.append({
            **position["trade"],
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
            "holding_sessions": len(rows) - position["entry_index"],
            "net_return_pct": _round(net_return * 100 if net_return is not None else None, 4),
            "gross_return_pct": None,
            "exit_cost_equity": None,
            "position_adjustments": list(position["adjustments"]),
            "max_target_allocation_pct": _round(position["target_allocation_pct"], 2),
            "mfe_pct": _round((position["max_high"] / position["entry_price"] - 1) * 100, 4),
            "mae_pct": _round((position["min_low"] / position["entry_price"] - 1) * 100, 4),
            "status": "open",
            "mark_date": str(last["t"])[:10],
            "mark_price": _round(float(last["c"]), 4),
        })

    scenario = _assemble_scenario(
        scenario=config.name,
        policy=config.name,
        horizon=None,
        cost_bps=cost_bps,
        trades=trades,
        skipped=skipped,
        curve=curve,
        total_cost=total_cost,
        exposed_sessions=exposed_sessions,
        lifecycle="research_only_preregistered",
        state_transitions=transitions,
    )
    scenario["parameters"] = {
        "probe_allocation_pct": config.probe_allocation_pct,
        "confirmed_allocation_pct": config.confirmed_allocation_pct,
        "standard_allocation_cap_pct": config.standard_allocation_cap_pct,
        "exit_unconfirmed_probe": config.exit_unconfirmed_probe,
        "defer_trend_break_until_qualified": config.defer_trend_break_until_qualified,
        "cooldown_sessions": COOLDOWN_SESSIONS,
        "failed_probe_window_sessions": FAILED_PROBE_WINDOW,
        "max_failed_probes": MAX_FAILED_PROBES,
    }
    scenario["metrics"]["chase_violations"] = sum(
        float(trade.get("entry_price") or 0) > float(trade.get("max_chase_price") or math.inf)
        for trade in trades
    ) + sum(
        float(item.get("execution_price") or 0) > float(item.get("max_chase_price") or math.inf)
        for trade in trades for item in trade.get("position_adjustments") or []
    )
    scenario["metrics"]["transition_count"] = len(transitions)
    return scenario
