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
