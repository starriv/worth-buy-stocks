# 单标的历史回测契约

> 契约版本：`1.4`（`1.0`–`1.3` artifact 保持可加载、可渲染，但不得跨版本合并统计）
> 评分模型版本：由 `scoring.MODEL_VERSION` 唯一提供，artifact 生成时原样记录。

本文冻结单标的历史回放、事件研究、交易仿真和可视化报告的语义。实现不得根据某次回测结果临时改变窗口、样本门槛、成交时点或验证结论。

## Contract 1.4 stateful 实验场景

`task-047` 证明当前 managed 存在“保护退出快、最终买入结论恢复慢”的状态错配。候选 1.4 只新增独立实验场景，不改变 1.3 managed：

- 首次进入仍要求生产最终结论“是”，下一 open 成交。
- long 沿用建议仓位、初始止损、1R 保本和延迟移动保护；高位弱反转仍下一 open 退出，MA60 下行破位在实验场景只记录风险观察。
- 任一保护退出后进入 63-session 恢复观察。最终“是”、高置信超跌反转、量能确认通过的回调反转，或“趋势破坏→趋势延续且反转确认不少于 2 项”可生成下一 open 重新入场。
- 重新入场继承前次目标仓位；持有中后续最终“是”只允许下一 open 向上恢复到更高建议仓位，不按日下调。
- artifact 必须记录状态转换、前次退出、恢复等待、重新入场和仓位恢复事件；场景名固定为 `disciplined_verdict_stateful`，验证前标记 experimental。
- 候选规则在 QQQ 新结果产生前冻结。保留决定只使用 `SPY,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,JPM,XOM,UNH,CAT,WMT,COST,JNJ,KO` 的 2025-04-04～2026-07-09 cache-only holdout；开发证据截止 2025-01-02，中间 purge 63 sessions。
- 通过标准同时约束收益改善占比、收益/Sharpe 中位差、正基准捕获率、最大回撤和换手；任一核心条件失败则拒绝升级，禁止在同一 holdout 调参。

1.4 已实现上述独立实验场景；旧 managed 语义和交易结果保持不变。实验场景能否升级为正式持仓策略只由 task-050 的预注册 holdout 决定。

## 研究目标与边界

回测回答两个不同问题：

1. 当前价量评分在同一标的的历史时间序列上，是否领先未来 21/63 个交易日的绝对及相对收益。
2. 信号在下一交易日成交、承担成本并禁止重叠持仓后，固定研究窗口与计划仓位持仓管理分别如何表现。

首版只回放价量主链路。新闻、Finnhub、账户敞口、snapshot 和 AI opinion 没有可靠的历史 point-in-time 数据，全部关闭。回测不下单，不模拟分钟级止损/止盈，不做自动调参或组合资金分配。

## 数据契约

- 标的集合固定为研究标的、`SPY`、`QQQ`；评分始终调用生产 `pipeline.build_result()`。
- feed 默认 `iex`，adjustment 默认 `split`。收益不含现金股息，不等同于 total return。
- 只允许完成日线。SQLite 模式只读 `daily_bars`，不得读取 `live_daily_bars` 或 snapshot。
- 默认数据源为 `cache-only`：不触网、不创建/迁移/写入数据库。缺库、schema 不兼容或 coverage 不足直接失败。
- 请求结束日若为周末、休市日、未完成的当前 session 或未来日期，只能依据 SQLite 已缓存的 `market_sessions` 截到最近完成 session，并在 provenance notes/coverage 记录 effective end；已完成 session 的真实缺口仍必须失败。
- `auto` 必须由操作者显式选择，复用现有 read-through；`input` 读取 multi-bars JSON，并保持硬离线。
- 评估开始日前至少加载 260 个 SPY 交易 session 作为 warmup（12-1 动量因子需 253 根收盘价；不足会让窗口开头的评分静默缺失最大权重因子）。实现额外加载 30 根 SPY session 缓冲，使零星停牌的个股仍能满足每标的 warmup 门槛。首个评分日必须同时具备研究标的、SPY、QQQ 的可评分数据。
- bar 按 symbol/date 去重并升序排列；OHLC 必须为正且 `low <= open/close <= high`，volume 不得为负。
- SPY/QQQ 必须覆盖相同的已完成交易日；`sync_ranges` 只证明请求范围完成，不能替代逐 session 日线完整性校验。研究标的缺价仍按下条处理。
- 研究日历使用 SPY session。评分日个股缺价的 session 写入 exclusions；停牌/退市尾部不得用未来恢复后的价格回填。

## 点时点评分与防未来函数

交易日 `t` 的信号只使用时间戳日期 `<= t` 的完成日线。评分完成后记录：

- `raw_composite`、封顶后的 `composite`；
- `base_verdict`、最终 `verdict`；
- 因子分解、风险闸门、确认层和入场时机；
- 计划价摘要与数据警告。

若修改或追加 `t` 之后的数据，`t` 及以前的评分、信号和待执行订单必须完全不变。

正式有效性验证固定逐 session 评分，即 `signal_step=1`。大于 1 的下采样只可用于性能或流程测试；artifact 仍可生成，但顶层 validation 必须为 `invalid_run`，不得用其自动结论支持或否定算法。

## 事件研究

- horizon 固定为 21、63 个 SPY 交易日。
- 收益口径为信号日 `t` 收盘到 `t+horizon` 收盘。
- target 日个股缺价时不剔除事件（停牌/退市与坏结果相关，剔除会向上偏置样本）：改用信号日之后、target 日或之前的最后一个收盘价做保守 mark，事件标记 `degraded`（`stale_target_price`：之后仍有数据的临时停牌；`terminal_price_before_horizon`：数据在 horizon 前终止；`bankrupt_terminal`：破产名单按 -100% 结算）。信号日之后完全无价才写入 exclusions（`no_price_after_signal`）。degraded 事件同时进入描述与推断统计。
- 同时计算标的绝对收益、相对 SPY/QQQ 超额收益，以及窗口内 MFE/MAE。
- 全部逐日重叠事件只用于描述统计。
- 推断样本从评估区间第一个有效事件锚定，之后每隔 `horizon` 个 session 取一个非重叠事件。
- 推断统计包括时间序列 Spearman IC、组间均值差和固定 seed `42` 的循环块 bootstrap 95% CI（circular block bootstrap，块长 ≈ n^(1/3)、下限 2；非重叠采样消除机械窗口重叠后，信号仍在 regime 内成簇，块重采样保留该序列相关而非假设 iid）。
- 分组固定为 `<60`、`60-75`、`>=75`，并平行报告最终 verdict、confirmation、risk gate 和 entry timing。

## 交易仿真

运行六个独立场景，两个入场策略 × 三种出场：

- 入场 `rank_only`：核心评分数据完整且 `raw_composite >= 75` 时产生入场信号。
- 入场 `disciplined_verdict`：最终 verdict 为 `是` 时产生入场信号。
- 出场 `_21` / `_63`（固定研究窗口）：持有期从 entry session 开始计完整 21/63 个可交易 session，之后在下一可交易 session open 结束观察；不执行 trade plan。该终点不是系统识别的高位退出，HTML 必须标为“窗口终点”。
- 出场 `_managed`（持仓管理）：执行建议仓位、初始止损、1R 保本、延迟移动止损，以及高位反转/趋势破位收盘信号的下一开盘退出；无固定持有期，无静态止盈。

共同规则：

- 信号在 `t` 收盘后形成，最早在 `t+1` 的个股 open 成交；禁止同日成交。
- 每个场景最多一个 long position；持仓期间重复信号记录为 skipped，不叠加仓位。
- 找不到 next open 的 pending entry/exit 明确标记，不虚构成交价。
- 默认单边成本 10bp；入场与退出各扣一次。
- 单纯“观察/过热”不构成退出；只有冻结的过热弱收盘组合或已取得趋势资格后的 MA60 下行破位才生成下一开盘退出。
- benchmark 为相同可比区间的研究标的 buy-and-hold 与 SPY buy-and-hold。普通终止行情在研究标的最后可得 session 截止，SPY 同步截断；已知破产标的在最后日线后的首个 SPY session 按零终值结算，之后净值保持为零至评估结束。

`_managed` 出场的日线近似口径（无分钟级路径，宁可保守）：

- 入场只投入信号日 `suggested_position_pct` 对应权益，余额保留现金；旧 fixture/缺仓位字段回退 100%，只用于兼容测试与旧 artifact 渲染。
- 初始风险 `R = entry_open − stop_loss_price`。最高完成收盘达到 `entry + 1R` 后，下一 session 起保护位至少提高到入场价上方 0.25%，并从该时点激活 `peak_close × (1 − trailing_stop_pct/100)`；当日收盘不得抬高当日保护位。
- 某 session open ≤ 保护位 → 按 open 出场（跳空）；否则 low ≤ 保护位 → 按保护位出场（盘中触发）。低点与高点同日的日内先后顺序不可知，一律按触发止损处理。
- 每个完成日线的生产评分可以给出 `overextended_reversal` 或 `trend_break` 持仓退出信号；信号形成当日不成交，最早在下一交易日 open 退出。超跌反转仓在首次重新取得 MA60 资格前忽略 `trend_break`，仍由结构止损管理。
- 入场日开盘价 ≤ 初始止损 → 放弃该次入场（skipped，`open_at_or_below_stop`）；入场日盘中仍可触发初始止损。
- trade plan 缺失可用止损价时该信号 skipped（`plan_exit_unavailable`）；`trailing_stop_pct` 缺失时回退 8%。
- 固定持有期与 `_managed` 两族场景的指标不得合并统计。

## 自动验证结论

主观察窗口固定为 63 日，主 benchmark 为 SPY。每个主比较组至少需要 8 个非重叠样本。

- `supports`：“是”组 63 日平均 SPY 超额收益的 95% CI 下界大于 0，且“是”组相对非“是”组均值差的 95% CI 下界大于 0。
- `contradicts`：样本门槛满足，但上述两项方向相反且 CI 上界均小于 0。
- `inconclusive`：样本不足、CI 跨 0，或两项证据不一致。
- `invalid_run`：数据完整性、防未来函数或 artifact 契约失败。该状态不得显示为有效回测。

`inconclusive` 是正常成功结果，不能通过改阈值或窗口将其包装为支持。

## Canonical artifact v1

JSON 顶层固定包含：

| 字段 | 语义 |
|---|---|
| `artifact_version` | 回测 artifact schema，独立于评分版本 |
| `model_version` | `scoring.MODEL_VERSION` |
| `semantic_hash` | 排除生成时间和本机路径后的内容 hash |
| `generated_at` | UTC 生成时间，不参与 semantic hash |
| `config` | symbol、请求区间、horizons、成本、数据模式、feed/adjustment、warmup 与 signal step |
| `provenance` | coverage、数据内容 hash、实际范围、根数与数据源 |
| `bars` | 可视化所需的研究标的完成日线 |
| `timeline` | 每个有效评分时点的生产评分摘要 |
| `event_study` | 事件、exclusions、描述/推断统计和自动结论条件 |
| `simulations` | 六个既有场景与一个 stateful 实验场景的 trades、状态转换、skipped、equity、drawdown 和 metrics |
| `benchmarks` | 标的与 SPY buy-and-hold 曲线及 metrics |
| `warnings` | 复权、overlay、样本与执行语义警告 |
| `validation` | `supports/inconclusive/contradicts/invalid_run` 与逐条件证据 |

HTML 只消费此 artifact，不访问 SQLite、不调用评分、不重算统计。任何页面新增数据必须先进入 artifact schema。

## 版本与变更

- 修改评分权重、阈值或 verdict 语义：提升 `MODEL_VERSION` 并更新 model card。
- 修改数据、事件研究、成交、成本或自动结论语义：提升 artifact/contract version，并保留旧 artifact 解释能力。
- 不同 model version 或 artifact version 的结果不得合并统计。

### 1.4（2026-07-11）

- 新增与旧 managed 并存的 `disciplined_verdict_stateful` 实验场景，记录恢复观察、重新入场、风险观察和仓位恢复状态转换。
- MA60 下行破位在实验场景只记录风险观察；高位弱反转和既有保护价继续执行。
- 保护退出后提供 63-session 恢复窗口，重新入场与仓位恢复全部下一 open 执行并计成本。
- 多标的 holdout 决定在 QQQ 新场景运行前预注册；验证失败不得在同一 holdout 内调参。

### 1.3（2026-07-11）

- managed 从标准化全仓改为执行建议仓位并保留现金；新增 1R 保本、延迟移动保护和保护位审计字段。
- timeline 增加持仓退出信号；高位反转与已取得趋势资格后的 MA60 下行破位在下一 open 执行。
- 固定 21/63 日场景冻结为“研究窗口”，HTML 默认展示 managed，并将固定场景 marker 改为“研究起点/窗口终点”。
- cache-only/input/auto 数据边界新增 SPY/QQQ 逐 session 对齐校验；coverage 完整但基准日线缺行时直接失败，避免研究日历和 21/63 日窗口静默错位。
- managed 移动止损峰值严格从入场后首个存活收盘开始，不再把入场开盘价当作最高收盘价。
- 仿真与 buy-and-hold 基准统一比较起止区间；已知破产标的在终止日线后的首个 SPY session 按零终值结算，固定持有期和 managed 场景均记录 `bankrupt_terminal` 出场。
- 破产事件的 MAE 纳入零终值，固定为 -100%。
- `1.0`–`1.2` artifact 保持可加载、可渲染。

### 1.2（2026-07-11）

- 交易仿真新增 `rank_only_managed` / `disciplined_verdict_managed` 两个场景：按信号自身 trade plan 的初始止损 + 移动止损出场（日线近似、无止盈、无固定持有期）。固定持有期四场景语义不变。
- timeline 的 trade plan 摘要新增 `trailing_stop_pct`。
- 事件研究、自动验证结论与冻结条件完全不变；managed 场景只提供描述性执行证据，不参与 `supports/contradicts` 判定。
- `1.0`/`1.1` artifact 保持可加载、可渲染。

### 1.1（2026-07-10）

- 推断 CI 由 iid percentile bootstrap 改为循环块 bootstrap（保留 regime 内信号聚集的序列相关；seed 与门槛不变）。
- target 日缺价事件改用保守 stale mark 并标记 `degraded`，不再剔除（消除停牌/退市相关的事件级幸存者偏差）。
- warmup 由 230 提升到 260（12-1 动量因子需 253 根收盘价），并额外加载 30 根 SPY session 缓冲以容忍个股零星停牌。
- 数据加载增加只读复权漂移探针，命中写入 provenance `quality_flags` 与 artifact warnings；read-through 缓存检测到重叠修订时改为刷新该 symbol 的全部缓存历史。
- `1.0` artifact 保持可加载、可渲染。
