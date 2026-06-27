#!/usr/bin/env python3
"""Account-context overlay: practical exposure advice from Alpaca positions.

``_account_overlay`` reads the Alpaca account/position context (already
normalized by ``portfolio``) and returns an overlay dict, an optional
verdict override, and an action string. It never modifies the composite.
"""
from __future__ import annotations

from typing import Any

from utils import to_num as _num

from .trade_plan import _pct, _price


def _account_overlay(
    a: dict[str, Any],
    verdict: str,
    suggested_position_pct: float | None,
    trade_plan: dict[str, Any],
    account_context: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if account_context is None:
        return None, None, None
    if not isinstance(account_context, dict) or account_context.get("status") != "ok":
        reason = "Alpaca 账户上下文不可用"
        if isinstance(account_context, dict):
            reason = account_context.get("reason") or reason
        return {"status": "unavailable", "reason": reason}, None, None

    account = account_context.get("account") or {}
    position = account_context.get("position") if isinstance(account_context.get("position"), dict) else None
    equity = _num(account.get("equity") or account.get("portfolio_value"))
    cash_pct = _num(account.get("cash_pct"))
    long_exposure_pct = _num(account.get("long_exposure_pct"))
    target = _num(suggested_position_pct) or 0.0

    held = bool(position and (_num(position.get("qty")) or _num(position.get("market_value"))))
    market_value = _num(position.get("market_value")) if held else None
    current_exposure_pct = (
        abs(market_value) / equity * 100.0
        if held and equity and market_value is not None else 0.0
    )
    side = str(position.get("side") or "long").strip().lower() if held else "long"
    if held and side == "short":
        account_action = "检测到空头持仓；本框架只评估多头趋势，空头敞口不按多头入场/止损计划管理，优先单独减风险"
        overlay = {
            "status": "ok",
            "as_of": account_context.get("as_of"),
            "holding_status": "short_held",
            "current_position_pct": _pct(current_exposure_pct),
            "target_position_pct": 0.0,
            "delta_position_pct": _pct(-current_exposure_pct),
            "account_cash_pct": _pct(cash_pct),
            "account_long_exposure_pct": _pct(long_exposure_pct),
            "suggested_action": account_action,
            "position": position,
            "position_plan": None,
            "unsupported_position_side": side,
            "verdict_adjustment": {
                "from": verdict,
                "to": "持仓需减风险",
                "reason": "账户当前为空头持仓，本评分框架只覆盖多头趋势纪律",
            },
        }
        return overlay, "持仓需减风险", account_action

    if verdict == "是":
        practical_target = target
    elif verdict == "观察" and held:
        practical_target = min(target, current_exposure_pct)
    else:
        practical_target = 0.0

    threshold = max(2.0, practical_target * 0.25)
    delta = practical_target - current_exposure_pct
    override_verdict: str | None = None

    if not held:
        if verdict == "是" and practical_target > 0:
            account_action = f"无持仓，可分批新开至约 {practical_target:.1f}% 账户权益"
        else:
            account_action = "无持仓，不新开；等待价格和评分重新满足条件"
    elif practical_target == 0:
        account_action = "已有持仓但当前评分/风控不支持持有，按保护性出场价分批减仓或退出"
        override_verdict = "持仓需减风险"
    elif current_exposure_pct > practical_target + threshold:
        account_action = (
            f"当前敞口约 {current_exposure_pct:.1f}% 高于目标 {practical_target:.1f}%，"
            "优先减仓至目标附近"
        )
        override_verdict = "持仓需减风险"
    elif verdict == "是" and current_exposure_pct < practical_target - threshold:
        account_action = (
            f"已有持仓但低于目标 {practical_target:.1f}%，只在计划入场价附近分批加仓"
        )
    else:
        account_action = "已有持仓，维持为主；不追价追加，按出场价和移动止损管理"

    position_plan: dict[str, Any] | None = None
    if held:
        avg_entry = _num(position.get("avg_entry_price"))
        last = _num(a.get("last_close")) or _num(position.get("current_price"))
        protective_exit = _num(trade_plan.get("stop_loss_price"))
        profit_protect: float | None = None
        if avg_entry and last and last > avg_entry * 1.06:
            candidate = avg_entry * 1.01
            if candidate < last:
                profit_protect = candidate
                protective_exit = max(protective_exit or 0, profit_protect)
        pnl_pct = _num(position.get("unrealized_pl_pct"))
        position_plan = {
            "avg_entry_price": _price(avg_entry),
            "protective_exit_price": _price(protective_exit),
            "profit_protect_price": _price(profit_protect),
            "take_profit_price": trade_plan.get("take_profit_price"),
            "take_profit_2_price": trade_plan.get("take_profit_2_price"),
            "unrealized_pl_pct": _pct(pnl_pct),
        }

    overlay: dict[str, Any] = {
        "status": "ok",
        "as_of": account_context.get("as_of"),
        "holding_status": "held" if held else "not_held",
        "current_position_pct": _pct(current_exposure_pct),
        "target_position_pct": _pct(practical_target),
        "delta_position_pct": _pct(delta),
        "account_cash_pct": _pct(cash_pct),
        "account_long_exposure_pct": _pct(long_exposure_pct),
        "suggested_action": account_action,
        "position": position if held else None,
        "position_plan": position_plan,
    }
    if override_verdict:
        overlay["verdict_adjustment"] = {
            "from": verdict,
            "to": override_verdict,
            "reason": "账户当前持仓敞口与评分/风控不匹配",
        }
    return overlay, override_verdict, account_action
