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
from .constants import ALPHA_WEIGHTS, CONFIRMATION_MIN, RECOVERY_POSITION_CAP_PCT
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
    # plan_blocking 决定交易计划分支；timing 降级原因只进输出（时机过热仍给回踩计划价）
    plan_blocking = gate_reasons + llm_reasons + core_data_reasons

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

    # 入场时机层（analyze_symbol 计算）：过热只降级、拐点可替代趋势热度确认。
    # 不改 composite（保持 IC 校准的排名分），只作用于 verdict/action/交易计划。
    timing = a.get("entry_timing") or {}
    timing_class = timing.get("classification")
    bounce = timing.get("bounce") or {}
    timing_reasons: list[str] = []
    # 拐点入场的替代确认：波动确认仍须达标（反弹要有参与度），趋势热度类确认
    # （technical/trend_quality）在回调转折处定义性偏低，由反转触发信号替代。
    # 数据诚实：量能缺失不是通过。拐点替代 technical/trend_quality，但 volume
    # 必须明确可算且达标，否则维持观察。
    vol_min_ok = vol_s is not None and vol_s >= CONFIRMATION_MIN["volume"]
    pullback_entry_ok = timing_class == "pullback_reversal" and vol_min_ok
    ma_data = a.get("ma") or {}
    weekly = a.get("weekly") or {}
    recovery_entry_ok = bool(
        timing_class == "recovery_reversal"
        and ma_data.get("MA200_rising") is True
        and weekly.get("bearish_alignment") is False
        and not core_data_reasons
        and not llm_reasons
    )
    overextended_reversal = bool(
        timing_class == "overextended"
        and bounce.get("up_day") is False
        and isinstance(bounce.get("close_strength"), (int, float))
        and bounce["close_strength"] <= 0.45
        and bounce.get("macd_hist_rising") is False
        and isinstance(bounce.get("vol_ratio_vs_ma20"), (int, float))
        and bounce["vol_ratio_vs_ma20"] >= 0.8
    )
    trend_break_exit = bool(
        ma_data.get("above_MA60") is False
        and ma_data.get("MA60_rising") is False
    )
    if overextended_reversal:
        position_exit = {
            "action": "exit_next_open",
            "classification": "overextended_reversal",
            "reason": "过热后出现放量弱收盘，下一交易日开盘保护利润",
        }
    elif trend_break_exit:
        position_exit = {
            "action": "exit_next_open",
            "classification": "trend_break",
            "reason": "价格跌破下行 MA60，下一交易日开盘退出",
        }
    else:
        position_exit = {"action": "hold", "classification": "none"}

    # 先计算原三层结论，再让第四层做单向过热降级或受约束的拐点替代。
    # 显式保留 base_verdict，供 forward log 对第四层做 A/B，避免只看到最终结果。
    if composite is None:
        base_verdict, base_action = "无法评分", "补充数据后重评"
    elif core_data_reasons:
        base_verdict, base_action = "无法评分", "补齐 SPY/QQQ 相对强度数据后重评"
    elif composite >= 75 and not gate_reasons and confirmation_ok:
        base_verdict, base_action = "是", "可关注 / 小仓位试探"
    elif composite >= 75 and not gate_reasons:
        base_verdict, base_action = "观察", "观察等待（技术未确认，买入封顶）"
    elif composite >= 60:
        base_verdict, base_action = "观察", "观察等待"
    else:
        base_verdict, base_action = "否", "回避"

    verdict, action = base_verdict, base_action
    if recovery_entry_ok:
        verdict, action = "是", "高置信超跌反转试仓（受限仓位，结构低点止损）"
        timing_reasons.append(
            "高置信超跌反转：深度回撤后出现放量强反转，长期均线仍上行且周线未空头排列——允许受限试仓"
        )
    elif base_verdict == "是" and timing_class == "overextended":
        verdict, action = "观察", "观察等待（入场时机过热，等回踩计划价）"
        ext = timing.get("ext_atr_above_ma20")
        r14 = timing.get("rsi14")
        timing_reasons.append(
            "入场时机过热：价格较 MA20 超出约 %s 倍 ATR、RSI14 %s——追高风险大，"
            "新开降级为观察，等回踩计划价或横盘消化后重评"
            % (ext if ext is not None else "无法确认", r14 if r14 is not None else "无法确认")
        )
    elif base_verdict == "是" and timing_class == "pullback_reversal":
        action = "回调拐点入场（分批，跌破反弹起点低点退出）"
    elif (
        base_verdict == "观察" and composite is not None and composite >= 75
        and not gate_reasons and pullback_entry_ok
    ):
        verdict, action = "是", "回调拐点入场（分批，反转触发替代趋势热度确认）"

    effective_plan_blocking = (
        llm_reasons + core_data_reasons if recovery_entry_ok else plan_blocking
    )
    blocking_reasons = effective_plan_blocking + timing_reasons
    suggested_position = _position_pct(a, composite)
    if recovery_entry_ok and suggested_position is not None:
        suggested_position = min(suggested_position, RECOVERY_POSITION_CAP_PCT)
    trade_plan = _trade_plan(
        a, verdict, effective_plan_blocking, confirmation_ok, timing_class
    )
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
        "verdict_before_entry_timing": base_verdict,
        "suggested_action": action,
        "factor_breakdown": breakdown,
        "confirmation": confirmation,   # technical/volume：确认 overlay，不进排名分
        "entry_timing": ({              # 入场时机层回显：分类与时机分（详情在分析对象）
            "classification": timing_class,
            "timing_score_pct": timing.get("timing_score_pct"),
            "recovery_entry": recovery_entry_ok,
        } if timing else None),
        "risk_gates": gate_reasons,
        "blocking_reasons": blocking_reasons,  # 面向输出：价格/趋势闸门 + 新闻面降级原因
        "cap_applied": effective_cap if effective_cap < 100 else None,
        "llm_overlay": ({
            "cap": llm_cap if llm_cap < 100 else None,
            "downgrade_reasons": llm_reasons,  # 解释「为何被降级」，对称于 risk_gates
            **llm_echo,                         # catalyst / data_trust / red_flags（仅回显）
        } if llm_context is not None else None),
        "suggested_position_pct": suggested_position,
        "position_exit": position_exit,
        "trade_plan": trade_plan,
        "account_overlay": account_overlay,
        "data_flags": _flags(a, alpha_subs),
        "weights": ALPHA_WEIGHTS,
    }
