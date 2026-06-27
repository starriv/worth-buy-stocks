#!/usr/bin/env python3
"""Main scoring entry point.

``score`` is the deterministic multi-factor scorer. It assembles ALPHA
sub-scores, applies risk gates and the LLM overlay, computes the
confirmation overlay, derives the verdict and action, then layers in the
trade plan and account overlay.
"""
from __future__ import annotations

from typing import Any

from .account_overlay import _account_overlay
from .constants import ALPHA_WEIGHTS, CONFIRMATION_MIN
from .factors import FACTOR_FNS, _f_technical, _f_volume_exec
from .gates import (
    _confirmation_ok,
    _core_data_reasons,
    _flags,
    _gates,
    _llm_overlay,
)
from .trade_plan import _position_pct, _trade_plan


def score(
    a: dict[str, Any],
    market_risk_off: bool | None = None,
    llm_context: dict[str, Any] | None = None,
    account_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """对单个 analyze_symbol 结果打分。a 含 error 时原样返回。

    market_risk_off：大盘 regime（SPY 是否跌破自身 200 日线）。由 build_result 传入；
    单票直调时默认 None（不启用市场闸门，向后兼容）。
    llm_context：非对称 LLM 风控 overlay 的输入（红旗 / 数据可信度 / 催化剂）。默认 None
    时本层完全退化、分数与回测不受影响；仅能降级封顶、绝不加分（详见 _llm_overlay）。
    account_context：可选的单票 Alpaca 账户/持仓上下文；只调整操作建议和持仓风险结论，
    不参与 composite 计算，默认 None 时完全向后兼容。
    """
    if not isinstance(a, dict) or "error" in a:
        return None

    # ALPHA 因子（进加权排名分）：只算 ALPHA_WEIGHTS 中的因子
    alpha_subs = {k: FACTOR_FNS[k](a) for k in ALPHA_WEIGHTS}
    breakdown, num, den = {}, 0.0, 0.0
    for k, w in ALPHA_WEIGHTS.items():
        s = alpha_subs[k]
        breakdown[k] = {
            "weight": w,
            "score_pct": round(s * 100) if s is not None else None,
            "points": round(w * s, 1) if s is not None else None,
        }
        if s is not None:
            num += w * s
            den += w
    raw = round(num / den * 100, 1) if den else None  # 按可用权重重归一到 0-100

    cap, gate_reasons = _gates(a, market_risk_off)
    llm_cap, llm_reasons, llm_echo = _llm_overlay(llm_context)
    effective_cap = min(cap, llm_cap)  # 价格否决层与 LLM overlay 取更严者；只压不抬
    composite = round(min(raw, effective_cap), 1) if raw is not None else None
    core_data_reasons = _core_data_reasons(alpha_subs)
    blocking_reasons = gate_reasons + llm_reasons + core_data_reasons

    # 确认 overlay（technical / volume / trend_quality，IC≈0：不加分，只在转弱时封顶买入）
    tech_s, vol_s = _f_technical(a), _f_volume_exec(a)
    tq_s = FACTOR_FNS["trend_quality"](a)  # ADX 在此体现
    confirmation_ok = _confirmation_ok(tech_s, vol_s, tq_s)
    confirmation = {
        "technical_pct": round(tech_s * 100) if tech_s is not None else None,
        "volume_pct": round(vol_s * 100) if vol_s is not None else None,
        "trend_quality_pct": round(tq_s * 100) if tq_s is not None else None,
        "ok": confirmation_ok,
        "minimums_pct": {k: int(v * 100) for k, v in CONFIRMATION_MIN.items()},
    }

    if composite is None:
        verdict, action = "无法评分", "补充数据后重评"
    elif core_data_reasons:
        verdict, action = "无法评分", "补齐 SPY/QQQ 相对强度数据后重评"
    elif composite >= 75 and not gate_reasons and confirmation_ok:
        # gate_reasons redundant here: any gate caps to <=70, so composite>=75 implies
        # no gates fired. Kept as defensive guard.
        verdict, action = "是", "可关注 / 小仓位试探"
    elif composite >= 75 and not gate_reasons:
        verdict, action = "观察", "观察等待（技术未确认，买入封顶）"
    elif composite >= 60:
        verdict, action = "观察", "观察等待"
    else:
        verdict, action = "否", "回避"

    suggested_position = _position_pct(a, composite)
    trade_plan = _trade_plan(a, verdict, blocking_reasons, confirmation_ok)
    account_overlay, account_verdict, account_action = _account_overlay(
        a, verdict, suggested_position, trade_plan, account_context
    )
    if account_verdict:
        verdict = account_verdict
        action = account_action
        if isinstance(trade_plan, dict):
            trade_plan = {
                **trade_plan,
                "status": "reduce_risk",
                "suggested_entry_price": None,
                "note": "账户敞口与评分/风控不匹配，先减风险；不建议新增仓位",
            }
    elif account_action:
        action = account_action

    return {
        "composite": composite,
        "raw_composite": raw,           # 未经否决封顶的原始分
        "verdict": verdict,
        "suggested_action": action,
        "factor_breakdown": breakdown,
        "confirmation": confirmation,   # technical/volume：确认 overlay，不进排名分
        "risk_gates": gate_reasons,
        "blocking_reasons": blocking_reasons,  # 面向输出：价格/趋势闸门 + 新闻面降级原因
        "cap_applied": effective_cap if effective_cap < 100 else None,
        "llm_overlay": ({
            "cap": llm_cap if llm_cap < 100 else None,
            "downgrade_reasons": llm_reasons,  # 解释「为何被降级」，对称于 risk_gates
            **llm_echo,                         # catalyst / data_trust / red_flags（仅回显）
        } if llm_context is not None else None),
        "suggested_position_pct": suggested_position,
        "trade_plan": trade_plan,
        "account_overlay": account_overlay,
        "data_flags": _flags(a, alpha_subs),
        "weights": ALPHA_WEIGHTS,
    }
