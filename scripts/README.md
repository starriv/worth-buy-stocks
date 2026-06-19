# scripts/ 工程说明

`worth-buy-stocks` skill 的脚本层。纯 Python 标准库 + 本机 `alpaca` CLI，无第三方依赖。
确定性计算（同输入同输出）、不下单、不打印凭证。面向接手者，非用户文档（用户文档见 `../SKILL.md`）。

---

## 核心理念

### 我们相信什么

1. **趋势是唯一的 alpha 来源。** 价格本身包含所有信息——新闻、基本面、情绪——都已折现到趋势中。我们不做估值叙事、不猜底部、不博反弹。趋势跟随不是"追涨杀跌"的贬义版本，而是"只参与已被市场验证的方向"。

2. **相对强度比绝对收益重要。** 一只股票涨了 20% 不说明任何问题——如果同期 SPY 涨了 25%，它是弱势股。我们的框架对标 SPY/QQQ，只选跑赢基准的标的。跑输大盘的股票，哪怕绝对收益为正，也不具备趋势领导力。

3. **数据诚实比模型复杂重要。** 回测暴露了一个残酷事实：在 2023–26 的单边牛市中，经过逐因子 IC 分析，只有**风险调整动量（IC≈0.10）**和**相对强度（IC≈0.06）**在 3 个月前瞻维度稳定为正。MACD、RSI、KDJ、量价关系——这些经典指标的 IC 趋近于零。我们**如实记录这个结论**，并据此刻意限制了它们在评分中的角色。

4. **纪律工具，不是盈利预言机。** 本评分的统计显著性不足（t≈1.2），从未在熊市验证。它不是"买入就能赚钱"的保证，而是把一套可复现的交易纪律编码为确定性规则——让每个决策都有据可查、事后可复盘。

### 我们不相信什么

- **估值**：PE、PB、DCF 不能告诉你未来 3 个月的方向。便宜可以更便宜。
- **新闻/情绪**：标题驱动的交易是不可复现的。我们不抓新闻、不读社交媒体。
- **均值回归**：弱势股的反弹是概率游戏，不是趋势交易者的猎物。
- **"这次不一样"**：如果回测说某个因子 IC≈0，我们不会因为直觉或个案而给它加权。

### 三不原则

- **不下单**：本 skill 只做分析，不创建/修改/取消任何订单。
- **不泄露凭证**：Alpaca Key/Secret、Telegram Token/Chat ID 只从环境变量读取，绝不写入输出。
- **不假装确定**：数据不足时返回 `无法确认`，不编造结论。

---

## 技术架构原理

### 分层与依赖

```
运行时（skill 评分链路）
  metrics.py ───────────────┐  纯指标原语，无内部依赖
  scoring.py ──────────────┐│  确定性因子打分引擎，纯读分析字典、无内部依赖
                            ││
  analysis.py ◄── metrics, scoring   单股分析 + 跨股聚合(相对强度) + 附 score 块
                            ▲
  indicators.py ◄── fetching, metrics, analysis, scoring   CLI 入口 + 向后兼容再导出
  fetching.py               Alpaca 日线拉取（分页/去重/feed 回退），无内部依赖

旁路工具（不在评分链路上）
  chart.py ◄── fetching(惰性)         终端 K 线
  notify_telegram.py                  Telegram 推送（读环境变量）
  stop_notify_hook.py ◄── notify_telegram(子进程)   Claude Code Stop hook

研究/回测（离线，不属于 skill 运行时）
  backtest_common.py ◄── fetching, analysis, metrics   篮子常量 + 取数面板 + 切片打分 + 统计原语
  backtest_score.py        ◄── backtest_common   分数分桶 / 截面 IC
  backtest_factor_ic.py    ◄── backtest_common   逐因子单因子 IC
  backtest_robustness.py   ◄── backtest_common   非重叠窗口 + regime 拆分 + 幸存者探针
```

依赖方向单向向上，无环。`indicators.py` 仅做 CLI + 再导出（旧的 `import indicators as I` 仍可用）。

### 设计原则

**纯函数优先。** 每个模块对相同输入产生相同输出。`metrics.py` 不触网、不读文件；`scoring.py` 纯读分析字典。只有 `fetching.py` 和 CLI 入口触网。这让单元测试可以离线运行、不需要 mock Alpaca API。

**模块自描述。** 每个 `.py` 的 docstring 说明自己的职责、输入输出、依赖。接手者不需要通读整个工程就能定位修改点。

**历史不足不抛错。** 所有指标函数在数据不够时返回 `None`（而非抛异常或退化为 0）。下游据此标记 `无法确认`，而不是基于不完整的数据做出错误判断。这个设计选择是刻意为之——在金融领域，沉默比错误答案安全。

---

## 打分引擎原理

`scoring.py` 是本项目的核心——一个确定性的多因子打分引擎，设计目标贴近真实量化基金的规则化决策流程。

### 三层架构

```
                  输入: analyze_symbol 的分析字典
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                     ▼
   ALPHA 加权层           风险否决层              确认 overlay
   (排名分来源)           (封顶/否决)             (不做加分)
         │                    │                     │
         ▼                    ▼                     ▼
   momentum 60            MA200 下行 → 55        技术转弱时
   rel_strength 40        MA60 跌破 → 65        "是"→"观察"
                          周线空头 → 50
         │                    │                     │
         └────────────────────┼─────────────────────┘
                              ▼
                      composite (0–100)
                      最终评分 + 结论
```

### 因子分工的设计哲学

这个架构来自血泪教训。早期版本把 trend、technical、volume 和 momentum、rel_strength 等权混入排名分——结果六因子 composite 的 IC (0.082) **低于** momentum 单因子 (0.102)。弱因子不是在"分散风险"，而是在**稀释信号**。

修正方案——把因子按 IC 分到三个功能层——是本项目最重要的架构决策：

| 层 | 包含因子 | IC 水平 | 角色 |
|---|---------|--------|------|
| ALPHA 加权 | momentum, rel_strength | 0.06–0.10 | 决定排名分，进 composite |
| 风险否决 | trend (MA200/MA60/周线) | ~0.05 | 封顶分数，不进排名 |
| 确认 overlay | technical, volume, trend_quality | ≈0 | 二元确认，只降不升 |

**trend 为什么是"否决层"而非"排名因子"？** trend 的 IC ≈ 0.05，不算完全无用——但它和 momentum/rel_strength 高度共线。一只跌破 MA60 的股票，动量分自然低。把 trend 放入排名加权就是三重计数同一信号。正确做法：让 momentum 和 rel_strength 承载排名，trend 做独立的风险覆盖——跌破关键均线时封顶分数，模拟真实基金的 risk-off 机制。

**technical/volume 为什么只能做确认？** 因为 IC ≈ 0。在回测中，MACD 金叉/死叉、RSI 超买超卖、放量/缩量的 3 个月前瞻预测力与随机无异。但它们能提供**时机信息**——一只趋势强劲的股票如果出现短期技术转弱，可能不是卖出信号，但也不是最佳入场时机。所以我们用"确认 overlay"：技术健康时畅通无阻，技术转弱时把"是"降为"观察"。它不做加分，只做延迟入场。

### 因子打分函数的设计

每个因子打分函数（`_f_momentum`, `_f_rel_strength` 等）采用 logistic squashing：

```
score = 1 / (1 + e^(-k × (x - x0)))
```

其中 x0 是"中性参考点"（50 分对应点），k 控制陡度。这种设计有几个刻意选择：

- **非对称激励**：logistic 在极端区间的边际递减。动量从 +30% 到 +40% 的加分远小于从 -5% 到 +5%。这防止了极端值主导排名。
- **硬边界编码为软过渡**：不用 if/else 断点，避免在阈值附近的剧烈跳变。一只动量 11.9% 和 12.1% 的股票应该相近，而非一个满分一个零分。

### 因子得分组成

单个 ALPHA 因子的 composite 子分数由 3-4 个维度 weighted 合成：

```
momentum 子分 = weighted(
    risk_adj_6m  →  逻辑曲线 squash,
    12-1 动量    →  逻辑曲线 squash,
    6m 收益率    →  逻辑曲线 squash  (可选)
)
```

维度之间按预设权重归一；缺失维度（数据不足）从分母剔除——不会因为历史不够而无脑扣到 0。

### 反波动率仓位建议

```
suggested_position_pct = min(目标波动(20%) / 实现年化波动 × 信号强度, 100%)
```

这是凯利公式的简化变体：高波动标的降仓位，低波动标的加仓位，信号越强仓位越高。上限 100%。**仅作参考，不下单。**

### 设计承诺

- 改打分逻辑：只动 `scoring.py` 的 `ALPHA_WEIGHTS` / 各 `_f_*` 断点 / `_gates()` 封顶规则。`score()` 与三个回测脚本自动跟随。
- 不改引擎结构的情况下新增因子：写新的 `_f_xxx()` 函数，加入 `ALPHA_FACTORS` 或 `CONFIRMATION_FACTORS` 字典。
- 所有评分参数有回测出处：问"为什么 momentum 是 60 不是 50"，答案在 `backtest_factor_ic.py` 的逐因子 IC 表格里。

---

## 指标计算原理

`metrics.py` 实现了所有技术指标和量化因子的确定性计算。几个关键设计选择：

### EMA 播种策略

MACD 计算中的 EMA 使用 **SMA-seeded** 初始化（前 N 根用 SMA 做种子），而非传统"首值做种子"。这个选择将 MACD 的预热需求从 ~78 根（3×slow）降到 ~40 根（slow+signal+5），使得两年日线足以覆盖所有指标，不再需要额外拉更长历史。

### 历史不足返回 None

这是整个工程中最被低估的设计决策。所有指标函数在数据不够时返回 `None`（而非假设为 0、抛错、或退化计算）。

为什么？在金融领域，一个基于不完整数据的 0 分（"我要避开"）和基于不完整数据的标记"无法确认"（"我不知道"）之间有巨大差异。前者可能导致正确的标的被错误排除；后者保留了不确定性，让决策者知道信息的边界。

### 周线聚合的跨市场兼容

周线 MACD 和均线排列按实际交易周聚合，而非简单每 5 根日线切片。当前周未收盘时，`last_week_partial` 标记为 `true`，指示周线判定仅做参考、待周线收盘后确认。

---

## 回测方法论

回测是本项目的"真相部"——所有因子权重、所有分层决策，理论上都应该能回溯到回测证据。

### 三类回测各司其职

| 脚本 | 问题 | 方法 |
|------|------|------|
| `backtest_factor_ic.py` | 哪个因子有预测力？ | 逐因子截面 Spearman IC，3 个月前瞻 |
| `backtest_score.py` | 综合评分有效吗？ | 五分位分桶 + 多空价差 + 截面 IC |
| `backtest_robustness.py` | 结论稳定吗？ | 非重叠窗口 + regime 拆分 + QC 探针 |

### 为什么用 Spearman 而非 Pearson

Spearman 秩相关不对收益的分布形态做假设。单只股票 3 个月的收益可能因为一次财报暴雷而极端负值——Pearson 会被这一个点拉偏；Spearman 只看排序，更稳健。

### 为什么按截面打分而非时序

同一时间截面上比较所有股票的因子值与未来收益的排序关系（cross-sectional IC），消除市场整体涨跌的干扰。时序 IC（单票自身因子 vs 自身未来收益）会受到"牛市所有因子都看起来有效"的严重污染。

### 已知局限（诚实标注）

回测基于**含幸存者偏差**的大盘篮子、**2023–26 单边牛市**样本；非重叠窗口下 3 个月独立样本仅 ~8 期，
**t≈1.2 不显著**，且**从未在熊市验证**。结论：方向上像有微弱 edge，统计上未证实。详见各回测脚本 docstring。
本评分是**纪律/方向工具**，不是被证明的盈利策略。

### 可复现性承诺

- 同日期 + 同篮子 + 同 feed → 同结果。所有随机性来自外部（交易日历、Alpaca 数据），引擎本身确定性。
- 历史日线可离线喂入（`--input -`），不依赖 Alpaca 实时 API。
- 回测参数集中：改篮子/窗口/前瞻期只动 `backtest_common.py` 的常量。

---

## 各文件职责

| 文件 | 职责 | 入口 |
|------|------|------|
| `metrics.py` | EMA/MACD/RSI/KDJ/均线/收益率/周线聚合；量化因子：年化波动、最大回撤、ATR、效率比、对数价格回归、12-1 动量 | 库 |
| `fetching.py` | `alpaca data multi-bars` 拉日线，分页、去重、sip→iex 回退 | 库 |
| `analysis.py` | `analyze_symbol`（单票全字段）+ `build_result`（跨票聚合相对强度、附 `score`） | 库 |
| `scoring.py` | `score(analysis)` 确定性多因子打分：alpha 加权 + 风险否决 + 确认 overlay + 反波动率仓位 | 库 |
| `indicators.py` | CLI：拉数→分析→打分→输出 JSON | `python3 indicators.py` |
| `chart.py` | 终端蜡烛图（默认自取数据，或 `--input -` 喂 JSON） | `python3 chart.py` |
| `notify_telegram.py` | 把文本推到 Telegram，读环境变量，未配置则提示 | `python3 notify_telegram.py` |
| `stop_notify_hook.py` | Stop hook：分析结束自动推送（内容守卫 + 去重 + 非阻塞） | 注册于 settings.json |
| `backtest_common.py` | 回测公共件：`UNIVERSE`/`Panel`/`spearman`/`quintile_spread` 等 | 库 |
| `backtest_*.py` | 三类因子有效性回测 | `python3 backtest_xxx.py` |

---

## 怎么跑

```bash
# 单股评分（默认两年日线，自动预热 MA200/周线 MACD）
python3 scripts/indicators.py --symbols AAPL,SPY,QQQ --feed iex --adjustment split

# 离线/测试：喂 multi-bars JSON，不触网
cat bars.json | python3 scripts/indicators.py --input -

# 终端 K 线（自取，最常用）
python3 scripts/chart.py --symbol AAPL --count 30

# 推送（需先 export TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）
printf '%s' "结论文本" | python3 scripts/notify_telegram.py

# 回测（触网，较慢；输出纯统计，看方向不看绝对收益）
python3 scripts/backtest_score.py        # 分桶 + 截面 IC
python3 scripts/backtest_factor_ic.py    # 逐因子 IC
python3 scripts/backtest_robustness.py   # 非重叠 + regime + 幸存者探针

# 单元测试（纯计算 + 离线路径，不触网）
python3 -m pytest tests/
```

---

## 约定

- 改打分权重：只动 `scoring.py` 的 `ALPHA_WEIGHTS` / 各 `_f_*` 断点，`score()` 与回测自动跟随。
- 改回测篮子/窗口：只动 `backtest_common.py` 的 `UNIVERSE` / `WARMUP` / `HORIZONS`。
- 新增纯函数请配单测（`tests/test_*.py`）；触网的回测脚本不进单测，靠手动跑验证。
- 历史不足时函数返回 `None`（不抛错、不退化为 0），下游据此记「无法确认」。
- 所有结论由 `scoring.py` 输出，不靠模型主观打分。模型的职责是用证据**解释**分数，而非自行评估。
