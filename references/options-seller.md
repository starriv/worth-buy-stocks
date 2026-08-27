# 模块化期权价差筛选规则

## 目录

- 范围与命令
- 两阶段流程
- BOLL 触发
- Delta 近零 IV 日历价差
- 区间震荡 Iron Condor
- 窄区间 Iron Butterfly
- 合约与价差过滤
- 波动率曲面与市场状态
- 动态结构优化
- IV 历史与组合风控
- 已有价差生命周期
- 评分和结论
- 事件与数据完整性
- 离线输入
- 输出要求

## 范围与命令

只分析美股和 ETF 上的以下定义风险结构：

- `bull_put`：卖出较高行权价 Put，同时买入同到期较低行权价 Put。
- `bear_call`：卖出较低行权价 Call，同时买入同到期较高行权价 Call。
- `calendar_iv`：卖出近月并买入远月的同类型、同行权价长日历价差；按保守自然价支付净借记，筛选初始净 Delta 接近 0、净 Vega 为正的结构。
- `iron_condor`：同到期卖出价外 Put/Call，并在两侧买入等宽保护腿；按四腿自然价收取净权利金，筛选初始净 Delta 接近 0、正 Theta、负 Vega 的区间结构。
- `iron_butterfly`：同到期卖出同一 ATM 行权价 Put/Call，并买入等宽两翼；按四腿自然价收取净权利金，筛选更窄盈利区间的正 Theta、负 Vega 结构。

不分析裸卖、Diagonal、broken-wing Iron Condor/Butterfly、0DTE，不提交或修改订单。现有持仓台账和生命周期规则只支持垂直信用价差，不把日历或四腿候选写入该台账。

单标的：

```bash
python3 scripts/options_seller.py analyze --symbol AAPL
python3 scripts/options_seller.py analyze --symbol AAPL --strategy calendar_iv
python3 scripts/options_seller.py analyze --symbol AAPL --strategy iron_condor
python3 scripts/options_seller.py analyze --symbol AAPL --strategy iron_butterfly
python3 scripts/options_seller.py analyze --symbol AAPL --strategy all
```

全市场或显式股池：

```bash
python3 scripts/options_seller.py scan --chain-top 25 --top 20
python3 scripts/options_seller.py scan --symbols AAPL,MSFT,NVDA
python3 scripts/options_seller.py scan --strategy calendar_iv --chain-top 25 --top 20
python3 scripts/options_seller.py scan --strategy iron_condor --chain-top 25 --top 20
python3 scripts/options_seller.py scan --strategy iron_butterfly --chain-top 25 --top 20
```

已有价差台账与只读管理：

```bash
python3 scripts/options_position_manager.py add --input confirmed-fill.json
python3 scripts/options_position_manager.py list
python3 scripts/options_position_manager.py review --live
python3 scripts/options_position_manager.py performance
```

人工调整参数时优先从默认值做单项变化，不同时改变多项门槛。所有参数和实际 feed 都回显在 JSON。

## 两阶段流程

1. 从 Alpaca `has_options` 活跃资产建立股池，默认覆盖 AMEX、ARCA、BATS、NASDAQ、NYSE 和 NYSEARCA；显式 `--symbols` 时跳过资产发现。
2. 用股票 snapshot 做价格和成交量初筛。
3. 读取已完成日线，计算 BOLL(20,2)、20 日实现波动率、ATR、MA60 和流动性。
4. 各策略模块独立初筛：`vertical_credit` 要求下轨收回或上轨回落；`calendar_iv`、`iron_condor` 与 `iron_butterfly` 要求价格位于 BOLL 中部区域且中轨斜率受控，Butterfly 使用更严格门槛。
5. 按各模块 `underlying_score` 排序，只为前 `--chain-top` 个标的读取所选策略期限和行权价窗口的合约链；`all` 取窗口并集。
6. 用当前链计算 ATM IV、25Δ 偏斜、期限结构和历史 30D IV 上下文。
7. 各模块独立构造候选：垂直模块连接同到期保护腿；日历模块连接同类型、同行权价的近远月合约；Iron Condor 连接两个分离的短行权价；Iron Butterfly 连接同一 ATM 体价；四腿模块均要求等翼宽。
8. 分别应用报价、Greeks、持仓量、事件、市场状态、期限/结构和风险预算硬过滤。
9. 在策略模块内部生成代表候选，再按统一 `verdict` 与兼容排序字段 `seller_score` 合并；不在策略之间共享评分组件或硬门槛。

不得给全股池逐一读取期权链；不得用股票版 `score.verdict` 决定卖方资格。

账户边界按运行模式固定：

- `scan` 是账户中立的策略发现流程。它不调用账户/持仓接口，不读取 `account_context`，不接受 `--account-equity` 改变评分，也不为持仓中的额外标的补拉日线。候选、策略分、结论和排序只由标的、期权链、波动率、市场状态及事件决定。
- `analyze` 才叠加只读账户风险，用于回答单组风险占比、最多完整组数和组合集中度；账户 overlay 不反向改写扫描股池。
- 扫描结果中的“适合构建”表示策略结构通过，与特定账户的购买力和组合适配分开；用户选定标的后再运行 `analyze` 完成账户适配复核。

## BOLL 触发

默认使用已完成日线、20 日窗口和 2 倍总体标准差。

Bull Put 必须同时满足：

- 最近 3 根日线的最低价至少一次触及或跌破当日下轨。
- 最新收盘重新位于下轨之上。
- 最新收盘高于前一日收盘。
- 中轨 5 日斜率不低于 -0.25%。

Bear Call 必须同时满足：

- 最近 3 根日线的最高价至少一次触及或突破当日上轨。
- 最新收盘重新位于上轨之下。
- 最新收盘低于前一日收盘。
- 中轨 5 日斜率不高于 +0.25%。

BOLL 触发只负责确定方向和短腿锚点，不代表期权链一定可交易。

## Delta 近零 IV 日历价差

结构必须满足：

- 同一 underlying、root、style、multiplier、期权类型和行权价。
- 卖出 14–35 DTE 近月，买入 45–90 DTE 远月；到期差默认 21–75 天。
- 行权价距现价默认不超过 5%；第一阶段要求 BOLL `%B` 距 0.5 不超过 0.35，5 日中轨斜率绝对值不超过 1%。
- 初始组合净 Delta = 远月 Delta − 近月 Delta，绝对值默认不高于 0.10。这里只表示快照时点接近 Delta 中性，不表示持有期间持续中性。
- 净 Vega = 远月 Vega − 近月 Vega，必须为正；净 Theta 和 Gamma 必须报告，不把正 Theta 视为持有到近月到期的保证。
- 期限 IV 差 = 近月 IV − 远月 IV；默认允许最低 −2 个波动率点，差值越高评分越有利。缺少任一腿 IV、Delta、Vega 或 Theta 时为数据不足。

保守执行与风险口径：

- 自然借记 = 远月 Ask − 近月 Bid；必须大于 0。
- Mid 借记 = 远月 Mid − 近月 Mid，只用于观察。
- 结构未发生提前指派时，借记 × 100 是每组损失上限；费用已提供时加入借记风险。
- 近月到期时的最大收益和盈亏平衡取决于届时远月剩余时间价值与 IV，保持不可预先确定，不用单一到期折线或当前 Greeks 编造。
- 必须在近月到期前平仓或展期复核。近月美式期权在任何交易日都有指派可能；Call 在除息日前尤其严格回避。

日历评分总计 100：Delta 中性 25、期限 IV 与 Vega 30、Carry/期限结构 15、链流动性 20、事件与组合适配 10。`delta_neutral`、`term_edge`、`liquidity` 是模块内代表候选标签。

## 区间震荡 Iron Condor

首版结构必须满足：

- 同一 underlying、root、style、multiplier 和到期日，乘数为 100 的标准美式合约。
- 行权价严格为 `Long Put < Short Put < 现价 < Short Call < Long Call`。
- 两侧保护翼宽完全相等；不构造 broken-wing，也不把两条独立垂直价差的风险简单相加。
- 21–45 DTE；两条短腿绝对 Delta 各为 0.10–0.25，初始组合净 Delta 绝对值不高于 0.10。
- 第一阶段要求 BOLL `%B` 距 0.5 不超过 0.35，中轨斜率绝对值不超过 0.75%；不要求方向性触轨。
- 每侧短腿离现价至少覆盖同到期一倍标准差预期波动的 0.70 倍。
- 组合净 Theta 必须为正、净 Vega 必须为负；净 Gamma、Rho 同时报告，缺失关键 Greeks 时为数据不足。

四腿自然价与风险口径：

- 自然净权利金 = Short Put Bid + Short Call Bid − Long Put Ask − Long Call Ask。
- Mid 净权利金仅用于观察，不可替代自然价。
- 最大收益 = 自然净权利金 × 100。
- 下侧最大亏损 =（Put 翼宽 − 自然净权利金）× 100；上侧最大亏损按 Call 翼宽同样计算；组合最大亏损取两侧较大者。
- 下盈亏平衡 = Short Put 行权价 − 自然净权利金；上盈亏平衡 = Short Call 行权价 + 自然净权利金。
- 四腿必须作为一个完整定义风险结构评估；提前指派、拆腿或到期 pin risk 会破坏静态风险图。

Iron Condor 独立评分总计 100：标的中性与市场状态 20、波动率优势 25、结构经济性 20、四腿流动性 15、预期波动与事件安全 10、组合适配 10。`balanced`、`safety`、`income` 是模块内代表候选标签。

## 窄区间 Iron Butterfly

结构必须满足：

- 同一 underlying、root、style、multiplier 和到期日，乘数为 100 的标准美式合约。
- Short Put 与 Short Call 使用同一个、且为该到期日最接近现价的 Put/Call 公同行权价；该体价距现价默认不超过 3%。
- Long Put 与 Long Call 分居体价两侧且翼宽完全相等；不构造 broken-wing。
- 21–45 DTE；两条 ATM 短腿绝对 Delta 各为 0.35–0.65，初始组合净 Delta 绝对值不高于 0.10。
- 第一阶段要求 BOLL `%B` 距 0.5 不超过 0.25，中轨斜率绝对值不超过 0.50%；不要求方向性触轨。
- 组合净 Theta 必须为正、净 Vega 必须为负；净 Gamma 必须完整报告。初始 Delta 接近 0 不消除临近体价的高 Gamma 风险。

保守执行与风险口径：

- 自然净权利金 = ATM Short Put Bid + ATM Short Call Bid − Long Put Ask − Long Call Ask。
- 最大收益 = 自然净权利金 × 100，且只有到期时标的恰好位于体价附近才能接近该上限。
- 下侧/上侧最大亏损分别为对应翼宽减自然净权利金后乘以 100；组合最大亏损取两侧较大者。
- 下盈亏平衡 = 体价 − 自然净权利金；上盈亏平衡 = 体价 + 自然净权利金。
- 自然净权利金/翼宽默认至少 50%，并要求现价到两侧盈亏平衡的较小距离至少覆盖同到期一倍标准差预期波动的 0.20 倍。
- 到期接近体价时两条短腿的指派结果具有不确定性；四腿完整性、pin risk 和盘后价格变化必须明确报告。

Iron Butterfly 独立评分总计 100：标的中性与市场状态 20、波动率优势 25、结构经济性 20、四腿流动性 15、盈亏平衡与事件安全 10、组合适配 10。代表候选沿用 `balanced`、`safety`、`income`，其中 safety 额外偏好更宽的盈亏平衡覆盖和更低净 Gamma。

## 合约与价差过滤

垂直信用价差默认合约窗口：

- 到期日：30–45 DTE。
- 短腿绝对 Delta：0.15–0.25；`cautious` 市场状态进一步限制为不高于 0.20。
- 两腿绝对宽度：1–10 美元；实际候选还须位于 ATR14 的 0.25–1.50 倍动态区间内。
- 每腿最低持仓量：100 为可观察硬门槛；达到“适合构建”还要求每腿至少 500。
- 单腿相对 Bid/Ask 价差：20% 为可观察硬门槛；达到“适合构建”还要求每腿不高于 12%。
- 自然价权利金/价差宽度：不低于 20%。
- 短腿必须位于对应 BOLL 轨道之外且为价外合约；Bull Put 要求现价在 MA60 上方，Bear Call 要求现价在 MA60 下方。
- 短腿距现价默认至少覆盖同到期一倍标准差预期波动的 0.90 倍；`cautious` 状态提高到 1.00 倍。
- 只组合标准美式合约：两腿的 underlying、root、style 和 multiplier 必须一致，默认排除调整合约，乘数必须为 100。

自然价是唯一保守执行口径：

- Bull Put / Bear Call 自然价 = 短腿 Bid − 长腿 Ask。
- Mid 权利金 = 短腿 Mid − 长腿 Mid，只用于观察报价区间。
- 最大收益 = 自然价 × 100。
- 最大亏损 =（价差宽度 − 自然价）× 100。
- Bull Put 盈亏平衡 = 短腿行权价 − 自然价。
- Bear Call 盈亏平衡 = 短腿行权价 + 自然价。

若提供 `--account-equity`，单组价差最大亏损不得超过 `--max-risk-pct`；没有账户权益时只报告每组最大亏损，不编造仓位。
提供账户权益时同时报告风险预算最多容纳的完整价差组数；它不替代购买力和组合集中度检查。
可用 `--round-trip-cost` 输入每组价差完整开平仓的费用估计；脚本据此报告费用后最大收益、最大亏损，并用费用后最大亏损计算账户风险。不提供时保持毛额，不猜测券商费率。
垂直与四腿信用结构在提供费用后还必须保持费用后最大收益为正；垂直价差的最低权利金/宽度、结构经济性评分和代表结构排序全部使用费用后口径。

实时模式默认读取 Finnhub 美国经济日历；CPI、PCE、就业报告、FOMC 与 GDP 在发布当日及前 1 个交易日冻结新建信用价差。可用 `--macro-events-file` 提供显式日历；日历不可用时结论为数据不足，而不是假设没有宏观事件。

## 波动率曲面与市场状态

- 对每个到期日从当前链提取最接近现价的 Call/Put IV 中位数作为 ATM IV。
- 预期波动使用 `现价 × ATM IV × sqrt(DTE/365)`，只作为一倍标准差模型代理，不表述为保证区间或精确概率。
- 分别报告 25Δ Put/Call 偏斜、风险逆转和近远月 ATM IV 期限斜率；合约不足时对应字段为空。
- 实时模式默认把固定 30D ATM IV 按纽约交易日写入独立 `data/options-history.sqlite3`；先按总方差在相邻期限间插值，只有单一期限时明确标记为最近到期代理。
- 历史 Rank/Percentile 只读取当前交易日之前的快照，默认至少 60 个历史交易日、最多 252 个观测；样本不足时保持 unavailable，不让当日值进入自身分位数。
- `--iv-history off` 可关闭记录，`--iv-history-db` 可指定独立数据库；`--input` 模式不读写数据库，只接受输入中的显式历史上下文。
- 市场状态使用已完成的 SPY/QQQ 日线、MA20/MA60 方向和 SPY 20 日实现波动率，分为 `normal`、`cautious`、`stress`。
- `cautious` 将单笔及全部组合集中度上限统一乘以 0.5；`stress` 暂停新建信用价差；SPY/QQQ 历史缺失时结论为数据不足。

## 动态结构优化

候选不再只按单一总分展示，而是给出三种用途的代表结构：

- `safety`：优先更高预期波动覆盖、更大 BOLL 安全垫、更低短腿绝对 Delta 和更低最大亏损。
- `balanced`：优先卖方适配总分，其次收益风险比。
- `income`：优先收益风险比、净 Theta/最大亏损和权利金/宽度。

这些标签是在各模块通过定义风险、事件、行情完整性和流动性检查的候选中选择；没有合格候选时不生成替代结构。候选不足三个时允许同一结构承担多个标签。日历价差保留 `delta_neutral`、`term_edge`、`liquidity` 三个独立标签。

## IV 历史与组合风控

- 账户上下文只通过 Alpaca 账户与持仓只读接口或用户提供的离线文件取得。
- 只把能按标准 OCC 合约代码配对成同到期 Bull Put/Bear Call 的现有仓位计入已确认定义风险。
- 现有价差风险采用 `宽度 × 100 × 组数` 的保守上限，不从不完整持仓信息猜测原始权利金。
- 默认单笔费用后最大亏损不高于账户权益 0.5%；新增后单标的期权风险不高于 2%，损失相关组不高于 4%，同到期日不高于 2%，全部定义风险期权不高于 6%。
- 相关性使用最近 60 个共同交易日，阈值默认 0.70；先按 Bull Put `+1`、Bear Call `-1` 调整方向，再计算共同亏损暴露，避免把反向策略的负标的相关误判为分散。
- 存在无法配对的期权腿或所需相关性历史不足时，候选为数据不足；账户明确禁止交易时直接回避。
- 单标的 `analyze` 没有账户上下文时继续报告单组风险，账户适配保持无法确认，也不编造现有敞口；账户中立 `scan` 允许按策略自身数据给出“适合构建”。

## 已有价差生命周期

`options_position_manager.py` 使用独立 `data/options-ledger.sqlite3` 保存用户确认的开平仓事实和每次检查结果。它不从候选自然价或账户成本字段猜测实际成交权利金。

当前生命周期模块只接受 Bull Put/Bear Call 垂直信用价差，不接受 `calendar_iv`、`iron_condor` 或 `iron_butterfly`。这些筛选结果中的候选 ID 不得传给 `options_position_manager.py add`，直到各自的多期限或四腿指派、滚动、拆腿与剩余腿状态机另行实现并验证。

记录实际开仓可以输入持仓 JSON，也可以引用筛选结果中的候选，但必须明确提供实际成交净权利金、组数和开仓时间：

```bash
python3 scripts/options_position_manager.py add \
  --candidate-result options-result.json --candidate-id 'AAPL|bull_put|...' \
  --quantity 1 --initial-credit 0.50 --opened-at 2026-08-21T15:30:00Z
```

实时检查从台账读取未关闭价差，自动完成以下只读步骤：

1. 从 Alpaca 账户持仓逐腿核对方向和数量；同一账户合约数量不会重复分配给多条台账记录。
2. 按台账到期日和行权价读取当前期权链。
3. 用 IEX snapshot 补标的现价，并以最近完成交易日判断周末/盘前报价新鲜度。
4. 补财报、除息和事件红旗，然后运行统一生命周期规则。
5. 把本次检查结果追加到审计历史；不会根据账户差异自动关闭或改写台账。

生命周期纪律：

- 权利金捕获达到 50%：`止盈退出`。
- 剩余 DTE 不高于 14：`计划退出`；不高于 7：`立即退出评估`，避免到期 Gamma 集中。
- 短腿绝对 Delta 达到 0.45、现价穿越短腿、平仓自然价达到初始权利金 2 倍，或到期前出现硬事件：`立即退出评估`。
- 其他完整数据状态：`继续持有`。
- 当前两腿报价、Delta、合约一致性、事件日历或现价缺失：`数据不足`。

DTE、到期日和事件窗口按纽约市场日期计算。纽约正常交易时段内，两腿报价年龄不得超过 15 分钟；平仓自然价要求短腿 Ask size 和长腿 Bid size 为正，单腿市场不得倒挂，跨腿自然平仓支出必须在 0 到价差宽度之间。已经能够确定的短腿穿越、Delta/亏损倍数、DTE 或止盈动作不会被事件/Snapshot 缺口覆盖，缺口作为该动作的并列数据警告；没有任何可确定动作时才返回 `数据不足`。

所有盈亏使用保守平仓自然价：短腿 Ask − 长腿 Bid。生命周期输出是处理计划，不创建、修改或取消订单。

生命周期检查会生成结构化告警：`立即退出评估` 为最高优先级，`计划退出`/`止盈退出` 为待处理，`数据不足` 为数据告警，`继续持有` 不制造告警。需要日报时显式指定目录：

```bash
python3 scripts/options_position_manager.py review --live \
  --report-dir artifacts/options --report-format both --include-performance
```

Markdown 日报包含持仓编号、到期日、两腿执行价、剩余数量、中文告警、持仓动作和数据降级状态；Telegram 摘要使用同一可辨识结构，避免同标的多组价差混淆。JSON 保留原始可审计字段。默认不推送 Telegram；只有显式加入 `--notify alerts`（仅有告警时）或 `--notify on`（始终发送）才读取 Telegram 凭证并尝试通知，通知失败不改变生命周期结果。

实际平仓后由用户记录成交净支出和时间：

```bash
python3 scripts/options_position_manager.py close \
  --position-id spread-... --quantity 1 \
  --close-debit 0.20 --closed-at 2026-08-28T19:45:00Z
```

`--quantity` 缺省时关闭全部剩余组数；小于剩余组数时记录独立平仓成交分录，台账保持开放并用剩余组数继续账户核对。原始开仓数量不会被覆盖。

记录实际平仓后可查看描述性绩效：

```bash
python3 scripts/options_position_manager.py performance
```

绩效只使用台账中的实际初始净权利金以及每个平仓成交分录的净支出、数量和时间，计算已实现损益、权利金捕获、风险回报、持有期和分策略结果；部分平仓的剩余未实现估值不进入统计。胜率以实际平仓批次为口径，同时报告已完全关闭持仓数和仍有剩余数量的持仓数。报告给出平仓前检查覆盖率、首次退出动作及提前时间，用于积累前瞻证据，但不把动作与结果的时间关联解释为因果有效性。手续费或净成交价之外的滑点未单独记录时也不补造。

严格离线复盘使用 `review --input lifecycle.json`；该模式不访问 Alpaca、不读取或写入台账，也不允许 `--include-performance` 或 Telegram 通知。显式 `--report-dir` 仍可把离线输入的确定性检查结果写成报告文件。

## 评分和结论

卖方适配分总计 100：

| 组件 | 上限 |
|---|---:|
| 标的形态与市场状态 | 20 |
| 波动率优势与曲面 | 25 |
| 价差经济性 | 20 |
| 期权链流动性 | 15 |
| 尾部、事件与安全垫 | 10 |
| 组合适配 | 10 |

结论：

- `适合构建`：无策略硬过滤、关键数据完整、OPRA 数据且得分至少 75；在 `scan` 中这是账户中立的结构结论。
- `观察等待`：无硬过滤且得分至少 60，或使用 indicative feed。
- `回避`：触发事件、流动性、报价、权利金、行权价或账户风险硬过滤。
- `数据不足`：报价、时间戳、Delta、持仓量、事件日历等关键字段缺失。

IV 缺失会使垂直价差溢价分归零并保留数据提示，但自然价仍可用于观察；不得把当前 IV 写成 IV Rank。Delta 只作行权价筛选，不得表述为精确胜率。

`calendar_iv` 使用独立的 100 分 `strategy_score`，同时提供 `seller_score` 兼容排序字段。其“适合构建/观察等待/回避/数据不足”阈值与 feed 限制相同，但评分组件和硬闸门不得套用垂直信用价差规则。

`iron_condor` 同样使用独立 `strategy_score` 并提供 `seller_score` 兼容字段；任一侧短腿 Delta、组合净 Delta、正 Theta/负 Vega、四腿自然价、预期波动覆盖、四腿流动性或事件硬闸门失败时，不得由另一侧的高分抵消。

`iron_butterfly` 使用独立 `strategy_score` 与兼容 `seller_score`；ATM 公共体价、体价距离、双短腿 Delta、组合净 Delta、正 Theta/负 Vega、Gamma 完整性、自然价/翼宽、盈亏平衡覆盖、四腿流动性和事件风险分别执行硬闸门。

## 事件与数据完整性

- 垂直信用价差到期前存在财报：回避。
- Bear Call 到期前存在除息日：回避，防止短 Call 提前指派风险。
- 日历价差近月到期前存在财报：默认回避；Call 日历近月到期前存在除息日：回避。
- Iron Condor 到期前存在财报或除息日：回避；除息闸门用于短 Call 提前指派风险。
- Iron Butterfly 到期前存在财报或除息日：回避；体价短 Call 同样具有提前指派风险。
- 近 30 天出现中高等级增发、调查、诉讼、停牌、欺诈等事件红旗：回避。
- 事件日历不可用：数据不足。
- 报价所属纽约交易日早于标的最后完成日线：视为陈旧报价。
- 纽约交易时段内报价年龄默认不得超过 15 分钟；自然价所需的短腿 Bid size 和长腿 Ask size 必须为正。
- OPRA 因明确订阅限制不可用时，`auto` 才回退 indicative。
- 用户显式选择 OPRA 时不自动回退。
- indicative 报价不得产生“适合构建”。

## 离线输入

`--input` 不触网、不读写 SQLite。最小形状：

```json
{
  "as_of": "2026-01-01T21:00:00Z",
  "bars": {
    "AAPL": [{"t":"...","h":1,"l":1,"c":1,"v":1}],
    "SPY": [{"t":"...","h":1,"l":1,"c":1,"v":1}],
    "QQQ": [{"t":"...","h":1,"l":1,"c":1,"v":1}]
  },
  "option_chains": {
    "AAPL": {"feed":"opra","contracts":[{
      "symbol":"AAPL...","underlying_symbol":"AAPL","root_symbol":"AAPL",
      "expiration_date":"2026-02-13","type":"put","style":"american",
      "strike_price":100,"tradable":true,"open_interest":500,"multiplier":100,
      "quote":{"bid":1,"ask":1.1,"bid_size":10,"ask_size":10,"timestamp":"..."},
      "greeks":{"delta":-0.2},"implied_volatility":0.35
    }]}
  },
  "events": {
    "AAPL": {"status":"ok","earnings_dates":[],"ex_dividend_dates":[]}
  },
  "iv_history": {
    "AAPL": {"status":"ok","observations":80,"iv_rank":65,"iv_percentile":70}
  },
  "account_context": {
    "status":"ok","account":{"equity":50000},"positions":{}
  }
}
```

离线 bars 必须按唯一时间戳升序排列，OHLCV 必须为有限数值；SPY/QQQ 各需至少 61 根已完成日线用于市场状态。单标的分析和扫描中实际进入第二阶段的标的必须有同名期权链与事件对象，其余第一阶段 bars 不要求预取期权链。

离线期权行至少保留合约代码、underlying、root、style、乘数、到期日、类型、行权价、可交易状态、持仓量、Bid/Ask 与 size、报价时间、Delta 和 IV。运行 `calendar_iv`、`iron_condor` 或 `iron_butterfly` 时还必须提供 Gamma、Vega 与 Theta；缺失关键 Greeks 时为数据不足。`as_of` 必填，报价时间不得晚于它。

实时模式的 `--end` 只允许今天或未来日期；历史 BOLL 与历史期权链复盘必须使用同一 `as_of` 的 `--input` artifact。

## 输出要求

最终中文回复按以下顺序：

1. `卖方结论`：标的、策略、结论和卖方适配分。
2. `BOLL 触发`：上下中轨、带宽、%B、中轨斜率及触发分类。
3. `波动率与市场状态`：30D IV、历史 Rank/Percentile、偏斜、期限结构、预期波动覆盖和 SPY/QQQ regime。
4. `候选价差`：安全/均衡/收益标签、到期日、DTE、两腿、动态宽度、自然价和 Mid。
5. `收益与最大风险`：最大收益、最大亏损、盈亏平衡、安全垫、单标的/相关组/总期权风险。
6. `流动性与 Greeks`：Delta、IV、持仓量、单腿 Bid/Ask 价差和数据 feed。
7. `事件与指派风险`：财报、除息、数据缺口和硬过滤。

`calendar_iv` 改用以下顺序：

1. `日历 IV 结论`：标的、Call/Put Calendar、结论和策略分。
2. `中性初筛`：BOLL `%B`、中轨斜率、现价与目标行权价距离。
3. `期限配对`：近月/远月到期、DTE、间隔、同执行价和两腿方向。
4. `IV 与 Greeks`：近远月 IV、期限差、初始净 Delta、净 Vega、净 Theta 和净 Gamma。
5. `借记与风险`：自然借记、Mid、每组借记风险、账户风险预算；最大收益和盈亏平衡写“不可预先确定”。
6. `流动性`：两腿 OI、Bid/Ask 价差、可执行 size、报价新鲜度和 feed。
7. `事件与指派风险`：近月前财报/除息、短腿提前指派、近月到期前退出或展期要求。

`iron_condor` 改用以下顺序：

1. `Iron Condor 结论`：标的、结论、策略分和区间形态。
2. `中性初筛`：BOLL `%B`、中轨斜率、两条短腿 Delta 和组合净 Delta。
3. `四腿结构`：到期、DTE、Long Put/Short Put/Short Call/Long Call、对称翼宽和收益区间。
4. `权利金与风险`：四腿自然净权利金、Mid、最大收益、上下侧最大亏损和双盈亏平衡；`scan` 报告每组风险，`analyze` 另报账户风险预算。
5. `波动率与 Greeks`：IV/RV、历史 IV Rank、两侧预期波动覆盖、净 Theta/Vega/Gamma。
6. `四腿流动性`：每腿 OI、Bid/Ask 价差、自然价方向 size、报价新鲜度和 feed。
7. `事件与指派风险`：财报、除息、两条短腿提前指派、到期 pin risk 与四腿完整性。

`iron_butterfly` 改用以下顺序：

1. `Iron Butterfly 结论`：标的、结论、策略分和窄区间形态。
2. `中性初筛`：BOLL `%B`、中轨斜率、ATM 体价距离、双短腿 Delta 和组合净 Delta。
3. `四腿结构`：到期、DTE、同体价 Short Put/Call、两侧 Long 翼、对称翼宽和盈利区间。
4. `权利金与风险`：自然净权利金、Mid、最大收益成立条件、上下侧最大亏损、双盈亏平衡和风险预算。
5. `波动率与 Greeks`：IV/RV、历史 IV Rank、盈亏平衡预期波动覆盖、净 Theta/Vega/Gamma。
6. `四腿流动性`：每腿 OI、Bid/Ask 价差、自然价方向 size、报价新鲜度和 feed。
7. `事件与指派风险`：财报、除息、体价双短腿指派、到期 pin risk、盘后变化与四腿完整性。

没有候选时，从 `spread_diagnostics.rejections` 报告 BOLL 未触发、硬过滤原因或缺失字段；不手工拼出替代价差。
