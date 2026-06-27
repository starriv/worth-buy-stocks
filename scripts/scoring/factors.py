#!/usr/bin/env python3
"""ALPHA / confirmation factor scoring functions for the scoring engine.

Contains the logistic squashing primitives (``_logistic``, ``_weighted``,
``_bool_score``) and all per-factor scorers (``_f_momentum``,
``_f_rel_strength``, ``_f_efficiency``, ``_f_trend``, ``_f_trend_quality``,
``_f_technical``, ``_f_volume_exec``). ``FACTOR_FNS`` dispatches the ALPHA
factors used in the weighted composite.
"""
from __future__ import annotations

import math
from typing import Any


def _logistic(x: float, x0: float, k: float) -> float:
    """squash 到 (0,1)：x=x0 → 0.5；k 控制陡度。"""
    return 1.0 / (1.0 + math.exp(-k * (x - x0)))


def _weighted(parts: list[tuple[float | None, float]]) -> float | None:
    """parts: [(score0_1, weight), ...]，按可用权重归一；空则 None。"""
    parts = [(s, w) for s, w in parts if s is not None]
    den = sum(w for _, w in parts)
    return sum(s * w for s, w in parts) / den if den else None


def _bool_score(items: list[tuple[bool | None, float]]) -> float | None:
    """items: [(cond, weight), ...]，cond 为 None 的项从分母剔除（无法确认）。"""
    return _weighted([(1.0 if c else 0.0, w) for c, w in items if c is not None])


def _f_trend(a: dict[str, Any]) -> float | None:
    ma = a.get("ma", {})
    m20, m60, m200 = ma.get("MA20"), ma.get("MA60"), ma.get("MA200")
    align = (m20 > m60 > m200) if None not in (m20, m60, m200) else None
    wk_bear = a.get("weekly", {}).get("bearish_alignment")
    not_bear = (wk_bear is False) if wk_bear is not None else None
    return _bool_score([
        (ma.get("above_MA200"), 0.30),
        (ma.get("above_MA60"), 0.20),
        (align, 0.20),
        (ma.get("MA60_rising"), 0.15),
        (not_bear, 0.15),
    ])


def _f_momentum(a: dict[str, Any]) -> float | None:
    m = a.get("momentum", {})
    ra, m121 = m.get("risk_adj_6m"), m.get("m12_1_pct")
    parts: list[tuple[float, float]] = []
    if ra is not None:
        # Sharpe-like：0.5 →0.5 分，1.0 →强，0 →弱
        parts.append((_logistic(ra, 0.5, 3.0), 0.6))
    if m121 is not None:
        parts.append((_logistic(m121, 5.0, 0.08), 0.4))
    return _weighted(parts)


def _f_rel_strength(a: dict[str, Any]) -> float | None:
    rs = a.get("relative_strength_pct", {})
    # 每个视界内对两个基准取较弱者（须同时跑赢 SPY/QQQ，保留「对最差基准负责」），
    # 再对 3m/6m 两个视界取均值。旧版对 4 个读数全局取 min，会把不同视界混在一起、
    # 让单一最差读数把整体压到地板，过度惩罚且丢失视界信息。
    horizon_vals: list[float] = []
    for k in ("r3m_63d", "r6m_126d"):
        per_bench = [(rs.get(bench) or {}).get(k) for bench in ("SPY", "QQQ")]
        if all(v is not None for v in per_bench):
            horizon_vals.append(min(per_bench))
    if not horizon_vals:
        return None
    return _logistic(sum(horizon_vals) / len(horizon_vals), 0.0, 0.12)


def _f_efficiency(a: dict[str, Any]) -> float | None:
    """ALPHA 第三因子：Kaufman 效率比（趋势"干净度"），∈[0,1]。

    只用 efficiency_30 单一原语，不含 trend_quality 里 ADX/回归/回撤等噪声成分——
    回测中正是裸 efficiency（IC≈0.063、与 momentum 相关≈0.08）才是有效的正交信号，
    混进 trend_quality 的混合反而被稀释到 IC≈0。缺失返回 None（按可用权重重归一）。
    """
    eff = (a.get("trend_quality") or {}).get("efficiency_30")
    return min(max(eff, 0.0), 1.0) if eff is not None else None


def _f_trend_quality(a: dict[str, Any]) -> float | None:
    tq = a.get("trend_quality", {})
    risk = a.get("risk", {})
    parts: list[tuple[float, float]] = []
    # Kaufman 效率比：趋势"干净度"
    eff = tq.get("efficiency_30")
    if eff is not None:
        parts.append((min(max(eff, 0.0), 1.0), 0.3))
    # 对数回归 R²：趋势"平滑度"（仅斜率为正时奖励）
    reg = tq.get("regression_3m") or {}
    r2, slope = reg.get("r2"), reg.get("ann_slope_pct")
    if r2 is not None:
        parts.append((r2 if (slope is not None and slope > 0) else 0.0, 0.2))
    # 最大回撤：回撤越小越好
    mdd = risk.get("max_drawdown_6m_pct")
    if mdd is not None:
        parts.append((min(max(1 + mdd / 25.0, 0.0), 1.0), 0.2))
    # ADX：趋势"力度"——与 efficiency_ratio 互补
    adx_data = tq.get("adx") or {}
    adx_val = adx_data.get("ADX")
    if adx_val is not None:
        # ADX > 40 满分，< 20 零分，中间线性
        score_adx = min(max((adx_val - 20) / 20.0, 0.0), 1.0)
        # +DI > -DI 确认方向：方向不对的打对折
        if not adx_data.get("bull_trend"):
            score_adx *= 0.5
        parts.append((score_adx, 0.3))
    return _weighted(parts)


def _f_technical(a: dict[str, Any]) -> float | None:
    macd = a.get("macd") or {}
    rsi = a.get("rsi") or {}
    kdj = a.get("kdj") or {}
    rc = macd.get("recent_cross")
    no_death = (rc is None) or (rc.get("type") != "death")
    r14 = rsi.get("RSI14")
    return _bool_score([
        (macd.get("bull"), 0.25),
        (macd.get("above_zero"), 0.20),
        (no_death, 0.15),
        ((r14 > 50) if r14 is not None else None, 0.20),
        (kdj.get("bull"), 0.10),
        (kdj.get("above_50"), 0.10),
    ])


def _f_volume_exec(a: dict[str, Any]) -> float | None:
    v = a.get("volume") or {}
    r20 = v.get("ratio_vs_ma20")
    if r20 is None:
        return None

    # OBV 顶背离：价格涨、OBV 不跟 → 派发预警
    obv_data = v.get("obv") or {}
    if obv_data.get("divergence") == "bearish":
        return 0.2

    # 基础分（单日量能比，四档）
    if r20 >= 3.0:      # 高位放量，可能派发
        base = 0.3
    elif r20 >= 0.8:    # 健康量能
        base = 1.0
    elif r20 >= 0.5:    # 缩量（回调可接受）
        base = 0.6
    else:               # 极度缩量，参与度低
        base = 0.4

    # 趋势惩罚层（只在异常时减分，新字段缺失时退化为空操作）
    penalty = 0.0
    avg5 = v.get("avg_ratio_5d")
    up_down = v.get("up_down_vol_ratio")
    vol_dir = v.get("vol_trend_10d")

    if avg5 is not None and avg5 < 0.5:
        penalty = max(penalty, 0.2)       # 近 5 日持续缩量
    if up_down is not None and up_down < 0.7:
        penalty = max(penalty, 0.2)       # 跌日放量 > 涨日放量（派发）
    elif up_down is not None and up_down < 0.9:
        penalty = max(penalty, 0.1)       # 轻度偏空
    if vol_dir == "falling":
        penalty = max(penalty, 0.1)       # 量能趋势萎缩

    return round(max(base - penalty, 0.0), 2)


# 因子子分调度：score() 只算 ALPHA_WEIGHTS 里的因子，避免算了不用。
# 定义在各 _f_* 之后，直接引用函数对象（无需 lambda 前向引用）。
FACTOR_FNS: dict[str, Any] = {
    "momentum": _f_momentum,
    "rel_strength": _f_rel_strength,
    "efficiency": _f_efficiency,
    "trend": _f_trend,
    "trend_quality": _f_trend_quality,
}
