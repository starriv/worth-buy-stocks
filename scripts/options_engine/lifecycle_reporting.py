"""Chinese lifecycle alerts and report artifacts derived from review output."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any


_SEVERITY = {
    "立即退出评估": "critical",
    "计划退出": "action",
    "止盈退出": "action",
    "数据不足": "warning",
    "继续持有": "normal",
}

_REASONS = {
    "expiration_reached": "已到到期日",
    "earnings_before_expiration": "到期前存在财报事件",
    "ex_dividend_before_expiration": "到期前存在除息/指派风险",
    "material_event_risk": "到期前存在重大事件风险",
    "short_delta_defense_triggered": "短腿 Delta 触发防守阈值",
    "loss_multiple_defense_triggered": "平仓支出触发亏损倍数阈值",
    "underlying_breached_short_strike": "标的价格已越过短腿执行价",
    "underlying_breached_break_even": "标的价格已越过 Iron Butterfly 盈亏平衡点",
    "expiration_pin_risk": "临近到期且价格接近 Butterfly 中心执行价，存在 pin/指派风险",
    "expiration_window_reached": "已进入计划离场 DTE 窗口",
    "profit_target_reached": "已达到权利金止盈目标",
    "lifecycle_conditions_normal": "生命周期条件正常",
    "unsupported_strategy": "策略类型不受支持",
    "position_terms_invalid": "台账持仓条款无效",
    "account_position_unconfirmed": "账户中无法确认该持仓",
    "account_option_position_unavailable": "账户期权持仓不可用",
    "account_leg_quantity_insufficient": "账户腿数量不足或已被其他台账记录占用",
    "account_position_confirmation_unavailable": "账户持仓核对不可用",
    "ledger_legs_not_confirmed_in_account": "台账各腿或剩余数量与账户不一致",
    "current_option_chain_unavailable": "当前期权链不可用",
    "current_leg_quotes_unavailable": "当前结构腿报价不可用",
    "current_leg_contract_mismatch": "当前合约与台账条款不一致",
    "current_quote_timestamp_invalid": "当前报价时间无效",
    "current_quote_stale": "当前报价已过期",
    "current_quote_timestamp_future": "当前报价时间晚于检查时间",
    "current_quote_or_delta_invalid": "当前报价或 Delta 无效",
    "current_close_debit_invalid": "当前自然平仓支出无效",
    "event_calendar_unavailable": "事件日历不可用",
    "underlying_spot_unavailable": "标的现价不可用",
}


def _number(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}"


def _reason_text(reasons: Any) -> str:
    values = reasons if isinstance(reasons, list) else []
    return "；".join(_REASONS.get(str(reason), str(reason)) for reason in values) or "未提供原因"


def _position_label(row: dict[str, Any]) -> str:
    symbol = str(row.get("underlying_symbol") or "未知标的")
    position_id = str(row.get("position_id") or "无持仓编号").replace("\n", " ").replace("｜", "-")
    expiry = str(row.get("expiration_date") or "未知到期日")[:10]
    quantity = row.get("quantity", "—")
    return f"{symbol}｜{position_id}｜{expiry}｜{_structure_text(row)} ×{quantity}"


def _structure_text(row: dict[str, Any]) -> str:
    legs = row.get("legs")
    if isinstance(legs, list) and len(legs) > 2:
        ordered = sorted(
            (leg for leg in legs if isinstance(leg, dict)),
            key=lambda leg: (float(leg.get("strike") or 0), 0 if leg.get("type") == "put" else 1),
        )
        return "/".join(
            f"{_number(leg.get('strike'), 2)}{'P' if leg.get('type') == 'put' else 'C'}"
            f"{'−' if str(leg.get('side')) in {'short', 'sell_to_open'} else '+'}"
            for leg in ordered
        )
    option_type = "P" if row.get("strategy") == "bull_put" else "C"
    return (
        f"{_number(row.get('short_strike'), 2)}/"
        f"{_number(row.get('long_strike'), 2)}{option_type}"
    )


def _percentage(value: Any) -> str:
    rendered = _number(value)
    return rendered if rendered == "—" else rendered + "%"


def build_lifecycle_alerts(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured alerts, excluding positions whose action is normal."""
    alerts = []
    for row in result.get("positions") or []:
        action = str(row.get("action") or "数据不足")
        severity = _SEVERITY.get(action, "warning")
        if severity == "normal":
            continue
        reason_text = _reason_text(row.get("reasons"))
        warning_text = _reason_text(row.get("data_warnings")) if row.get("data_warnings") else ""
        message = f"{_position_label(row)}｜{action}｜{reason_text}"
        if warning_text:
            message += f"｜数据警告：{warning_text}"
        alerts.append({
            "severity": severity,
            "position_id": row.get("position_id"),
            "underlying_symbol": row.get("underlying_symbol"),
            "action": action,
            "message": message,
            "dte": row.get("dte"),
            "natural_close_debit": row.get("natural_close_debit"),
            "profit_capture_pct": row.get("profit_capture_pct"),
            "short_delta": row.get("short_delta"),
            "short_deltas": row.get("short_deltas"),
            "reasons": list(row.get("reasons") or []),
            "data_warnings": list(row.get("data_warnings") or []),
        })
    priority = {"critical": 0, "action": 1, "warning": 2}
    return sorted(alerts, key=lambda item: (
        priority.get(item["severity"], 9), str(item.get("position_id") or "")
    ))


def render_lifecycle_report(
    result: dict[str, Any], *, performance: dict[str, Any] | None = None
) -> str:
    """Render a compact Chinese Markdown daily report."""
    counts = result.get("counts") or {}
    alerts = build_lifecycle_alerts(result)
    lines = [
        "# 定义风险信用价差生命周期日报",
        "",
        f"检查时间：{result.get('as_of') or '未知'}",
        "",
        (
            f"持仓 {counts.get('positions', 0)} 个；立即退出评估 {counts.get('exit_review', 0)} 个；"
            f"计划/止盈退出 {counts.get('planned_exit', 0)} 个；数据不足 {counts.get('data_insufficient', 0)} 个；"
            f"含数据警告 {counts.get('data_warning_positions', 0)} 个。"
        ),
        "",
        "## 告警",
        "",
    ]
    if alerts:
        marks = {"critical": "🔴", "action": "🟠", "warning": "🟡"}
        lines.extend(
            f"- {marks.get(alert['severity'], '•')} {alert['message']}"
            for alert in alerts
        )
    else:
        lines.append("- 当前没有需要处理或补数据的告警。")
    lines.extend([
        "",
        "## 持仓明细",
        "",
        "| 持仓编号 | 标的 | 结构 | 到期日 | 数量 | 动作 | DTE | 平仓支出 | 捕获 | 短腿 Delta | 原因/警告 |",
        "|---|---|---|---|---:|---|---:|---:|---:|---:|---|",
    ])
    for row in result.get("positions") or []:
        strategy = {
            "bull_put": "Bull Put", "bear_call": "Bear Call",
            "iron_condor": "Iron Condor", "iron_butterfly": "Iron Butterfly",
        }.get(
            row.get("strategy"), str(row.get("strategy") or "—")
        )
        reason = _reason_text(row.get("reasons"))
        if row.get("data_warnings"):
            reason += "；数据警告：" + _reason_text(row.get("data_warnings"))
        reason = reason.replace("|", "\\|").replace("\n", " ")
        capture = _percentage(row.get("profit_capture_pct"))
        structure = _structure_text(row)
        position_id = str(row.get("position_id") or "—").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {position_id} | {row.get('underlying_symbol') or '—'} | "
            f"{strategy} {structure} | {str(row.get('expiration_date') or '—')[:10]} | "
            f"{row.get('quantity', '—')} | {row.get('action') or '—'} | {row.get('dte', '—')} | "
            f"{_number(row.get('natural_close_debit'), 4)} | {capture} | "
            f"{_number(row.get('short_delta'), 4)} | {reason} |"
        )
    if not (result.get("positions") or []):
        lines.append("| — | — | — | — | — | 无开放持仓 | — | — | — | — | — |")

    flags = result.get("data_flags") or []
    lines.extend(["", "## 数据状态", ""])
    lines.append("- 无数据降级标记。" if not flags else "- " + "\n- ".join(map(str, flags)))
    if performance:
        summary = performance.get("summary") or {}
        evidence = performance.get("evidence") or {}
        lines.extend([
            "",
            "## 已实现成交绩效（描述性）",
            "",
            f"- 实际平仓批次：{summary.get('realized_close_records', 0)} 个",
            f"- 完全关闭持仓：{summary.get('closed_positions', 0)} 个",
            f"- 仍有剩余数量的持仓：{summary.get('partial_open_positions', 0)} 个",
            f"- 净损益：{_number(summary.get('net_pnl'))}",
            f"- 胜率：{_percentage(summary.get('win_rate_pct'))}",
            f"- 平均风险回报：{_percentage(summary.get('average_return_on_risk_pct'))}",
            f"- 证据状态：{evidence.get('status', 'insufficient_history')}；"
            f"有检查记录的平仓批次 {evidence.get('realized_close_records_with_reviews', 0)} 个",
        ])
    lines.extend([
        "",
        "> 本报告仅用于只读检查和离场评估，不会提交订单；自然平仓支出不是实际成交价。",
        "",
    ])
    return "\n".join(lines)


def render_telegram_summary(result: dict[str, Any], *, max_positions: int = 20) -> str:
    counts = result.get("counts") or {}
    alerts = build_lifecycle_alerts(result)
    lines = [
        "定义风险期权结构检查",
        f"时间：{result.get('as_of') or '未知'}",
        (
            f"持仓 {counts.get('positions', 0)}｜立即评估 {counts.get('exit_review', 0)}｜"
            f"计划/止盈 {counts.get('planned_exit', 0)}｜数据不足 {counts.get('data_insufficient', 0)}｜"
            f"数据警告 {counts.get('data_warning_positions', 0)}"
        ),
    ]
    if alerts:
        lines.extend(alert["message"] for alert in alerts[:max(1, max_positions)])
        if len(alerts) > max_positions:
            lines.append(f"另有 {len(alerts) - max_positions} 条告警未展开")
    else:
        lines.append("当前无告警。")
    lines.append("只读检查，不会提交订单。")
    return "\n".join(lines)


def write_lifecycle_report(
    result: dict[str, Any], directory: str | Path, *, output_format: str = "markdown",
    performance: dict[str, Any] | None = None,
) -> list[str]:
    """Write explicit report artifacts and return absolute paths."""
    if output_format not in {"markdown", "json", "both"}:
        raise ValueError("output_format must be markdown, json or both")
    target = Path(directory).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z]+", "", str(result.get("as_of") or ""))[:15]
    if not stamp:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = target / f"options-lifecycle-{stamp}Z"
    files: list[str] = []
    if output_format in {"markdown", "both"}:
        path = base.with_suffix(".md")
        path.write_text(render_lifecycle_report(result, performance=performance), encoding="utf-8")
        files.append(str(path))
    if output_format in {"json", "both"}:
        path = base.with_suffix(".json")
        payload = dict(result)
        payload["alerts"] = build_lifecycle_alerts(result)
        if performance is not None:
            payload["performance"] = performance
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files.append(str(path))
    return files
