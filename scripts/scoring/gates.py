#!/usr/bin/env python3
"""Risk gates, LLM overlay, confirmation check, flags, and data reasons.

``_gates`` is the price/trend veto layer (caps the composite from above).
``_llm_overlay`` is the asymmetric news/event risk overlay (only downgrades).
``_confirmation_ok`` checks the technical/volume/trend_quality confirmation
thresholds. ``_flags`` produces data-quality flags; ``_core_data_reasons``
produces blocking reasons when core ranking inputs are missing.
"""
from __future__ import annotations

from typing import Any

from .constants import (
    CONFIRMATION_MIN,
    LLM_CAP_HARD,
    LLM_CAP_SOFT,
    LOW_LIQUIDITY_USD,
)


def _gates(a: dict[str, Any], market_risk_off: bool | None = None) -> tuple[int, list[str]]:
    """风险否决层：返回 (封顶分, [触发原因])。

    个股闸门看自身均线结构；市场闸门看大盘（SPY）regime——
    `market_risk_off=True`（SPY 跌破自身 200 日线）时额外封顶到 65：回测显示该
    regime 下排名分 IC 反转为负（≈−0.10），高分不可信，故禁新开「是」、最高「观察」，
    符合趋势跟随「大盘下行不开新多」。仅 True 触发；None/False（含历史不足）不封。
    """
    ma = a.get("ma", {})
    cap, reasons = 100, []
    above200, rising200 = ma.get("above_MA200"), ma.get("MA200_rising")
    if above200 is False and rising200 is False:
        cap = min(cap, 55)
        reasons.append("价格在 200 日线下方且 200 日线下行（趋势空头 / risk-off）")
    elif above200 is False:
        cap = min(cap, 70)
        reasons.append("价格跌破 200 日线（长期趋势走弱）")
    if a.get("weekly", {}).get("bearish_alignment") is True:
        cap = min(cap, 50)
        reasons.append("周线均线空头排列")
    if ma.get("above_MA60") is False:
        cap = min(cap, 65)
        reasons.append("价格跌破 MA60")
    if market_risk_off is True:
        cap = min(cap, 65)
        reasons.append("大盘 risk-off（SPY 跌破 200 日线）——该 regime 排名分 edge 反转，新开仓封顶为「观察」")
    return cap, reasons


def _llm_overlay(llm_context: dict[str, Any] | None) -> tuple[int, list[str], dict[str, Any]]:
    """非对称 LLM 风控 overlay：只降级不加分、只规避不预测（详见模块 docstring）。

    llm_context 由 agent 从新闻 / 舆情 / 事件提炼后传入（不是 LLM 现算的数字）：
        {
          "as_of": str,
          "sources": [{"id": str, "title": str, "published_at": str, "url": str}, ...],
          "red_flags": [{"type": str, "severity": "high"|"medium"|"low", "note": str, "source_id": str}, ...],
          "data_trust": "ok" | "suspect",   # 拆股未复权 / 停牌 / 疑似坏数据 → suspect
          "catalyst": str | None,           # 「为什么动」，仅回显解释，绝不影响分数
        }
    返回 (cap, reasons, echo)。cap=100 表示不封顶；只会 min()——无法抬分、无法升级。
    输入缺省或结构异常 → (100, [], {})：完全退化，确定性主链路不受影响（向后兼容）。
    """
    if not isinstance(llm_context, dict):
        return 100, [], {}
    cap, reasons = 100, []
    red_flags = llm_context.get("red_flags")
    red_flags = red_flags if isinstance(red_flags, list) else []
    for rf in red_flags:
        if not isinstance(rf, dict):
            continue
        sev = str(rf.get("severity", "")).lower()
        kind = rf.get("type") or "event"
        note = rf.get("note") or rf.get("type") or "未注明"
        if sev in ("high", "critical", "severe"):
            cap = min(cap, LLM_CAP_HARD)
            reasons.append(f"致命红旗（{kind}）：{note} —— 事件破坏趋势前提，封顶为否")
        elif sev in ("medium", "moderate"):
            cap = min(cap, LLM_CAP_SOFT)
            reasons.append(f"软红旗（{kind}）：{note} —— 买入降级为观察")
        # low / info：仅供解释，不封顶（保持非对称：负面才作用）
    if str(llm_context.get("data_trust", "")).lower() in ("suspect", "unverified", "stale", "bad"):
        cap = min(cap, LLM_CAP_SOFT)
        reasons.append("数据存疑（拆股未复权 / 停牌 / 疑似坏数据）—— 技术信号可信度下降，买入降级为观察")
    echo = {
        "as_of": llm_context.get("as_of"),
        "sources": llm_context.get("sources") if isinstance(llm_context.get("sources"), list) else [],
        "catalyst": llm_context.get("catalyst"),
        "data_trust": llm_context.get("data_trust", "ok"),
        "red_flags": red_flags,
    }
    return cap, reasons, echo


def _flags(a: dict[str, Any], alpha_subs: dict[str, float | None]) -> list[str]:
    flags: list[str] = []
    if a.get("ma", {}).get("MA200") is None:
        flags.append("历史不足 200 根，长期趋势 / 200 日线无法计算（趋势因子已按可用权重重归一）")
    for k, s in alpha_subs.items():
        if s is None:
            flags.append(f"因子 {k} 数据不足，未计入加权")
    adv = (a.get("volume") or {}).get("dollar_vol_ma20")
    if adv is not None and adv < LOW_LIQUIDITY_USD:
        flags.append(
            f"20 日均成交额约 ${adv / 1e6:.1f}M，低于 ${LOW_LIQUIDITY_USD / 1e6:.0f}M 流动性门槛——"
            "滑点/冲击成本偏高，仓位与结论需谨慎（评分以大盘股校准，对低流动性标的迁移性弱）"
        )
    return flags


def _core_data_reasons(alpha_subs: dict[str, float | None]) -> list[str]:
    reasons: list[str] = []
    if alpha_subs.get("rel_strength") is None:
        reasons.append("相对 SPY/QQQ 强度缺失——核心排名前提不完整，补齐 benchmark 数据后重评")
    return reasons


def _confirmation_ok(tech_s: float | None, vol_s: float | None, tq_s: float | None) -> bool:
    checks = (
        (tech_s, CONFIRMATION_MIN["technical"]),
        (vol_s, CONFIRMATION_MIN["volume"]),
        (tq_s, CONFIRMATION_MIN["trend_quality"]),
    )
    return all(value is None or value >= threshold for value, threshold in checks)
