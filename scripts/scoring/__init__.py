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
账户 overlay（account_context）只读 Alpaca 持仓，把当前敞口与建议仓位比较，用于给出
更具体的「新开 / 持有 / 加仓 / 减仓 / 退出」动作；不改变 composite 本身。

**最关键的实证局限（2026-07-09 扩篮子对照后修订）——edge 首先是篮子条件的，其次
才是 regime 条件的**：把 45 只手挑大盘票按规则扩到 132 只（11 行业每行业 ≥7，另含
8 只退市票）后，63 日非重叠全样本 IC 从 +0.076（t≈1.57）跌到 +0.013（t≈0.35，
95%CI[-0.064,+0.085] 横跨 0），五分位多空毛价差 +1.85% → −0.64%；行业中性化后同样
无意义（IC +0.015）。同一套修正后代码在旧篮子上完整复现旧结论（risk-on IC +0.103、
t≈2.01、CI[+0.007,+0.200]；行业中性全样本 +0.085、t≈2.36、CI[+0.016,+0.155]）——
唯一变量是篮子。诚实解读：**排名分的横截面 edge 只在手挑的大盘票池内可测得，无法
推广到更宽的大盘股全集**；旧篮子按「今天著名的名字」入选，本身带后见之明。regime
结论（risk-on 有效、risk-off 反转——否决层的依据）在旧篮子内仍成立，在宽篮子上两个
regime 都无 edge。含 2022 后「≥75 是」桶不再单调领先（60–75 桶反而更高）。定位结论：
composite 是纪律工具与风险过滤器（否决层/确认层/事件层的价值不依赖排名 IC），
**不是经过验证的选股 alpha**；全市场 scanner 的排名只应作纪律过滤解读。权重不因此
重调（在噪声样本里重新拟合只会更糟），后续以 forward_log 的样本外证据为准绳。

确认 overlay 的 A/B 实证（2026-07-09，修正口径：破产票终值按 -100%、五分位扣
单边 10bp 成本报净价差）：买入候选（composite≥75 无否决）按 confirmation.ok 拆分，
21 日前瞻 ok 组 +1.78% vs 封顶组 +0.77%（差 +1.01%，n=180/284）；63 日 +4.65% vs
+4.70%（差 -0.05%）。按预注册判定规则（63 日差值 ±1% 内）→ **保留但证据不足**：
gate 对 1 个月内入场时点有帮助、3 个月洗平；维持"只封顶不加分"，待 forward log
积累样本外证据后复评。同轮 21 日五分位净价差扣成本后转负（毛 +0.23% → 净 -0.17%），
63 日净 +1.41% 存活——月度换仓会吃掉短周期 edge，评分只应服务 ≥3 个月持有周期。

历史教训：早期等掺入 technical/volume 的六因子 composite，IC 反而低于强因子单打
——弱因子稀释强信号。本版按数据只保留 IC 为正且彼此不过度共线的因子进排名分。
注：risk_adj_6m 当前口径与标准 Sharpe(mean/std×√252) 截面相关 0.999、IC 仅差
0.003，已验证开方口径不影响排名，故不改。

**LLM overlay（llm_context，非对称风控层，绝不进 composite）**：唯一由模型提供输入的
层，但只做规避、不做预测、只降级、不加分——与 confirmation overlay 同构。动机：价量因子
结构上看不见「为什么动」与事件风险（财报暴雷 / 增发稀释 / 诉讼 / 拆股未复权 / 停牌）。
这些信息无法诚实 backtest（LLM 读历史新闻有前视泄漏、且无 point-in-time 语料），故按
本系统纪律**不够格进加权排名分**；只能作为 min() 封顶层规避已知风险：致命红旗→封顶 50
（否）、软红旗 / 数据存疑→封顶 74（是降级为观察）、利好催化剂仅回显不加分。llm_context
缺省（None）时本层完全退化、确定性主链路与回测分数不受任何影响（向后兼容、可复现）。
"""
from __future__ import annotations

from .account_overlay import _account_overlay
from .constants import (
    ALPHA_WEIGHTS,
    CONFIRMATION_MIN,
    LLM_CAP_HARD,
    LLM_CAP_SOFT,
    LOW_LIQUIDITY_USD,
    TARGET_VOL_PCT,
)
from .engine import score
from .factors import (
    FACTOR_FNS,
    _f_efficiency,
    _f_momentum,
    _f_rel_strength,
    _f_technical,
    _f_trend,
    _f_trend_quality,
    _f_volume_exec,
)
from .gates import (
    _confirmation_ok,
    _core_data_reasons,
    _flags,
    _gates,
    _llm_overlay,
)
from .trade_plan import _position_pct, _trade_plan

__all__ = [
    "ALPHA_WEIGHTS",
    "TARGET_VOL_PCT",
    "LOW_LIQUIDITY_USD",
    "LLM_CAP_HARD",
    "LLM_CAP_SOFT",
    "CONFIRMATION_MIN",
    "FACTOR_FNS",
    "_f_momentum",
    "_f_rel_strength",
    "_f_efficiency",
    "_f_trend",
    "_f_trend_quality",
    "_f_technical",
    "_f_volume_exec",
    "_gates",
    "_llm_overlay",
    "_confirmation_ok",
    "_flags",
    "_core_data_reasons",
    "_trade_plan",
    "_position_pct",
    "_account_overlay",
    "score",
]
