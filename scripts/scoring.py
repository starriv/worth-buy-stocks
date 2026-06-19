#!/usr/bin/env python3
"""确定性多因子打分引擎：把 analyze_symbol 的输出映射为 0-100 评分与结论。

设计目标：贴近真实量化基金逻辑——规则化、可复现、风险调整、带否决层与仓位建议。
模型的职责由「主观打分」变为「解释本引擎给出的分数」。纯函数，仅读分析字典。

权重经逐因子 IC 回测校准（见 backtest_factor_ic.py）。3 个月前瞻 IC（2020-07→
2026-06、57 期、含 2022 熊市、篮子纳入已退市票降幸存者偏差，仅看相对排序与符号）：
momentum(≈0.042) ≈ rel_strength(≈0.043) > efficiency(≈0.027) ≫ technical /
volume(负)。三个 ALPHA 因子在 honest 样本里接近且偏弱（早期 24 期牛市样本曾显示
momentum≈0.087，是过拟合）；因高度共线，55/35 的 momentum/rel 拆分对 composite
影响极小，故维持权重不逐样本重调（避免 curve-fit）。据此分工：

  ALPHA 加权（决定排名分 composite，缺失按可用权重重归一）：
    momentum 55 · rel_strength 35 · efficiency 10
    momentum 与 rel_strength 截面相关 ≈0.84（高度共线，实为 ~1.2 因子）；
    efficiency（Kaufman 效率比）与 momentum 相关仅 ≈0.08——近正交且 IC 为正，
    加进 composite 后整体 63 日 IC 0.072→0.076（单调，eff20 至 0.078）。取保守
    权重 10 拿增量、不把权重 curve-fit 到 24 个噪声样本里最优的 20。
  风险否决层（veto / 封顶，模拟基金 risk-off 覆盖；trend 的价值在此而非排名）：
    跌破下行 200 日线 → 55；仅跌破 200 日线 → 70；周线空头 → 50；跌破 MA60 → 65。
  确认 overlay（technical / volume，IC≈0，不进加权）：
    只做二元确认——技术转弱时把本可成立的「是」封顶为「观察」，绝不加分。

仓位建议（反波动率）：目标年化波动 / 实现波动 × 信号强度，上限 100%。

**最关键的实证局限——edge 是 regime 条件的**（backtest_robustness regime 拆分，
63 日非重叠）：risk-on（SPY>MA200）IC ≈+0.099、t≈2.05、五分位多空 +2.35%，真实
且接近显著；risk-off（SPY<MA200）IC ≈−0.097、五分位多空 −0.49%，**edge 反转为负**
（动量在熊市崩溃）。全样本 IC +0.046 是两者混合。这正是 risk_gates 否决层（SPY/
个股跌破 MA200 时封顶）存在的理由——alpha 恰在 risk-off 失效。含 2022 后「≥75 是」
桶不再单调领先（60–75 桶反而更高），说明高分≠高确定性，须配合 regime 与否决层解读。

历史教训：早期等掺入 technical/volume 的六因子 composite，IC 反而低于强因子单打
——弱因子稀释强信号。本版按数据只保留 IC 为正且彼此不过度共线的因子进排名分。
注：risk_adj_6m 当前口径与标准 Sharpe(mean/std×√252) 截面相关 0.999、IC 仅差
0.003，已验证开方口径不影响排名，故不改。
"""
import math

# ALPHA 因子权重（详见模块 docstring）。efficiency 为近正交的第三因子（小权重）。
ALPHA_WEIGHTS = {"momentum": 55, "rel_strength": 35, "efficiency": 10}
TARGET_VOL_PCT = 20.0  # 反波动率仓位的目标年化波动
LOW_LIQUIDITY_USD = 5_000_000  # 20 日均成交额低于此值 → 低流动性警示（仅 flag，不封顶）


def _logistic(x, x0, k):
    """squash 到 (0,1)：x=x0 → 0.5；k 控制陡度。"""
    return 1.0 / (1.0 + math.exp(-k * (x - x0)))


def _weighted(parts):
    """parts: [(score0_1, weight), ...]，按可用权重归一；空则 None。"""
    parts = [(s, w) for s, w in parts if s is not None]
    den = sum(w for _, w in parts)
    return sum(s * w for s, w in parts) / den if den else None


def _bool_score(items):
    """items: [(cond, weight), ...]，cond 为 None 的项从分母剔除（无法确认）。"""
    return _weighted([(1.0 if c else 0.0, w) for c, w in items if c is not None])


def _f_trend(a):
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


def _f_momentum(a):
    m = a.get("momentum", {})
    ra, m121 = m.get("risk_adj_6m"), m.get("m12_1_pct")
    parts = []
    if ra is not None:
        # Sharpe-like：0.5 →0.5 分，1.0 →强，0 →弱
        parts.append((_logistic(ra, 0.5, 3.0), 0.6))
    if m121 is not None:
        parts.append((_logistic(m121, 5.0, 0.08), 0.4))
    return _weighted(parts)


def _f_rel_strength(a):
    rs = a.get("relative_strength_pct", {})
    # 每个视界内对两个基准取较弱者（须同时跑赢 SPY/QQQ，保留「对最差基准负责」），
    # 再对 3m/6m 两个视界取均值。旧版对 4 个读数全局取 min，会把不同视界混在一起、
    # 让单一最差读数把整体压到地板，过度惩罚且丢失视界信息。
    horizon_vals = []
    for k in ("r3m_63d", "r6m_126d"):
        per_bench = [(rs.get(bench) or {}).get(k) for bench in ("SPY", "QQQ")]
        per_bench = [v for v in per_bench if v is not None]
        if per_bench:
            horizon_vals.append(min(per_bench))
    if not horizon_vals:
        return None
    return _logistic(sum(horizon_vals) / len(horizon_vals), 0.0, 0.12)


def _f_efficiency(a):
    """ALPHA 第三因子：Kaufman 效率比（趋势"干净度"），∈[0,1]。

    只用 efficiency_30 单一原语，不含 trend_quality 里 ADX/回归/回撤等噪声成分——
    回测中正是裸 efficiency（IC≈0.063、与 momentum 相关≈0.08）才是有效的正交信号，
    混进 trend_quality 的混合反而被稀释到 IC≈0。缺失返回 None（按可用权重重归一）。
    """
    eff = (a.get("trend_quality") or {}).get("efficiency_30")
    return min(max(eff, 0.0), 1.0) if eff is not None else None


def _f_trend_quality(a):
    tq = a.get("trend_quality", {})
    risk = a.get("risk", {})
    parts = []
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


def _f_technical(a):
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


def _f_volume_exec(a):
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
FACTOR_FNS = {
    "momentum": _f_momentum,
    "rel_strength": _f_rel_strength,
    "efficiency": _f_efficiency,
    "trend": _f_trend,
    "trend_quality": _f_trend_quality,
}


def _gates(a, market_risk_off=None):
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


def _flags(a, alpha_subs):
    flags = []
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


def _position_pct(a, composite):
    """反波动率仓位建议：目标波动/实现波动 × 信号强度。"""
    vol = (a.get("volatility") or {}).get("ann_vol_3m_pct")
    if not vol or composite is None:
        return None
    size = min(TARGET_VOL_PCT / vol, 1.0) * (composite / 100.0)
    return round(size * 100, 1)


def score(a, market_risk_off=None):
    """对单个 analyze_symbol 结果打分。a 含 error 时原样返回。

    market_risk_off：大盘 regime（SPY 是否跌破自身 200 日线）。由 build_result 传入；
    单票直调时默认 None（不启用市场闸门，向后兼容）。
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
    composite = round(min(raw, cap), 1) if raw is not None else None

    # 确认 overlay（technical / volume / trend_quality，IC≈0：不加分，只在转弱时封顶买入）
    tech_s, vol_s = _f_technical(a), _f_volume_exec(a)
    tq_s = FACTOR_FNS["trend_quality"](a)  # ADX 在此体现
    confirmation_ok = tech_s is None or tech_s >= 0.5
    confirmation = {
        "technical_pct": round(tech_s * 100) if tech_s is not None else None,
        "volume_pct": round(vol_s * 100) if vol_s is not None else None,
        "trend_quality_pct": round(tq_s * 100) if tq_s is not None else None,
        "ok": confirmation_ok,
    }

    if composite is None:
        verdict, action = "无法评分", "补充数据后重评"
    elif composite >= 75 and not gate_reasons and confirmation_ok:
        verdict, action = "是", "可关注 / 小仓位试探"
    elif composite >= 75 and not gate_reasons:
        verdict, action = "观察", "观察等待（技术未确认，买入封顶）"
    elif composite >= 60:
        verdict, action = "观察", "观察等待"
    else:
        verdict, action = "否", "回避"

    return {
        "composite": composite,
        "raw_composite": raw,           # 未经否决封顶的原始分
        "verdict": verdict,
        "suggested_action": action,
        "factor_breakdown": breakdown,
        "confirmation": confirmation,   # technical/volume：确认 overlay，不进排名分
        "risk_gates": gate_reasons,
        "cap_applied": cap if cap < 100 else None,
        "suggested_position_pct": _position_pct(a, composite),
        "data_flags": _flags(a, alpha_subs),
        "weights": ALPHA_WEIGHTS,
    }
