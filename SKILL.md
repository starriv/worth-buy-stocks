---
name: worth-buy-stocks
description: "Evaluate whether an individual stock is worth buying, holding, reducing, avoiding, or watchlisting under the user's Alpaca-only trend-following and relative-strength framework. Use when the user asks 值得买吗, 股票版, 股票评分, buy/hold/sell/avoid/watchlist, Alpaca market data, 30 天行情, 当日行情, 交易纪律, 趋势跟随, 相对强度, 主升趋势, 修正阶段, 日线 MA60, 周线均线空头排列, momentum leadership, countertrend-entry avoidance, 技术指标, technical analysis, MACD, RSI, KDJ, 随机指标, 量价确认, 量能, 成交量, volume, OBV, 金叉死叉, 顶背离, or 超买超卖."
---

# 值得买 - 股票版

## 核心原则

把用户的交易纪律执行为趋势跟随与相对强度框架。它用于判断买入资格和持仓风险，不用于预测价格，也不下单。

- 默认只使用 Alpaca CLI / Alpaca Market Data API。不要使用其他券商连接器、网页搜索、新闻、社交媒体、分析师观点或模型记忆作为证据。
- 不创建、修改或取消订单。不要打印、保存或暴露 Alpaca 密钥。
- 必须输出明确结论和评分：`是否值得买`、`纪律评分 X/100`、触发的强制排除条件和一句话依据。
- 默认输出决策摘要，不输出思考过程、草稿、工具计划、逐步计算过程或长篇分析。
- 缺少关键数据时，不给买入结论；标记 `无法确认`，并说明被阻断的检查项。
- 优先选择趋势延续和相对强度领先标的；排除估值叙事、弱势修复、均值回归反弹和相对强度落后的标的。

## Alpaca 数据流程

默认快速路径只需要一次最新行情调用和一次历史日线调用；多标的时把所有 ticker 与 `SPY,QQQ` 一起批量请求。

1. 只在需要确认 CLI 存在时运行：

   ```bash
   alpaca version
   ```

   不默认运行 `alpaca doctor`；仅在数据调用失败、凭证或连接需要排障时使用。Trading API 失败不阻断本 skill 的市场数据分析。

2. 选择并标注 feed：

   ```bash
   FEED="${ALPACA_DATA_FEED:-iex}"
   ```

   用户明确要求或订阅支持时可用 `sip`。若 `sip` 返回 403/订阅限制，重试一次 `iex` 并在输出中标注限制。

3. 获取当日行情：

   ```bash
   alpaca data multi-snapshots --symbols {TICKER},SPY,QQQ --feed "$FEED" --quiet
   ```

   提取最新价、成交时间、bid/ask、价差、当日开盘/最高/最低/当前价、前收、日内区间位置、当日涨跌幅、跳空幅度、成交量和数据是否陈旧。只有批量 snapshot 缺字段时才使用单票 snapshot/latest-trade/latest-quote 作为备用调用。

4. 获取历史日线并计算全部量化指标——用本 skill 自带脚本，不要靠手算递归指标：

   ```bash
   python3 "$SKILL_DIR/scripts/indicators.py" \
     --symbols {TICKER},SPY,QQQ \
     --feed "$FEED" \
     --adjustment split
   ```

   `--start`/`--end` 可省略：默认 `--end` 为今天、`--start` 为约两年前（足够 MA60 与周线 MACD 预热）。仅在需要固定回测窗口时显式传日期。

   其中 `$SKILL_DIR` 为本 skill 目录。`indicators.py` 为 CLI 入口，逻辑按职责拆分到同目录模块（`metrics.py` 指标原语、`fetching.py` Alpaca 日线拉取、`analysis.py` 分析与聚合）；调用方式与输出不变。脚本内部调用 `alpaca data multi-bars`（约两年已完成日线，自动处理分页），仅用 Python 标准库确定性计算并输出 JSON：日线 MA10/20/30/60 及 MA60 方向、52 周高点距离、30 日结构（收益率/区间位置）、1/3/6 个月收益率、相对 SPY/QQQ 强度、MACD(12,26,9)、RSI(14/6)、KDJ(9,3,3)、量价（量能比），以及周线 MA5/10/20/30、周线 MACD 和周线空头排列判定。MACD/RSI/KDJ 这类递归指标必须以脚本输出为准，不得手算估计。脚本失败或字段缺失时，相应检查项标记 `无法确认`。

   底层日线接口（脚本内部使用，仅在排障或脚本不可用时手动调用）：

   ```bash
   alpaca data multi-bars --symbols {TICKER},SPY,QQQ \
     --start {YYYY-MM-DD} --end {YYYY-MM-DD} \
     --timeframe 1Day --adjustment split --feed "$FEED" --limit 10000 --quiet
   ```

5. 终端 K 线（可选，便于直观确认趋势）：`chart.py` 自取日线并在终端打印最近约 30 根蜡烛图（涨绿跌红、左侧价格刻度）。**默认自取，无需手动传日期**——脚本按 `--count` 自动回推足够窗口：

   ```bash
   python3 "$SKILL_DIR/scripts/chart.py" --symbol {TICKER} --feed "$FEED" --count 30
   ```

   仅做可视化、不参与评分；脚本失败不阻断结论。`--count` 调根数、`--rows` 调图高、`--no-color` 关颜色（重定向到文件时自动关色）。若想复用已有的 multi-bars JSON，可改用管道并显式 `--input -`：`alpaca data multi-bars --symbols {TICKER} --start ... --end ... ... | python3 "$SKILL_DIR/scripts/chart.py" --symbol {TICKER} --input -`（注意底层 multi-bars 必须带 `--start/--end`，否则只返回最新 1 根）。

6. 只在 ticker 有歧义或需要确认资产状态（交易所、是否可交易）时，懒加载资产元数据：

   ```bash
   alpaca asset get --symbol-or-asset-id {TICKER} --quiet
   ```

   如果该端点因仅有 Market Data 凭证而失败，价格和趋势检查可以继续。

## 技术指标解读

数值一律取自 `scripts/indicators.py` 的输出，脚本基于同一次日线调用确定性计算，不靠模型手算。指标是**趋势确认与时机工具，不是反转预测工具**：主升中 RSI/KDJ 高位属强势特征，不能据此给卖出；仅当指标与趋势/相对强度结构同时转弱时才下调评级。

脚本输出关键字段速查（完整字段见脚本输出 JSON，解读要点）：

| 类别 | 字段路径 | 核心信号 |
|------|---------|---------|
| MACD | `macd.{DIF,DEA,hist,above_zero,bull,recent_cross}` | DIF>DEA 偏多、零轴上方偏多、金叉/死叉 |
| RSI | `rsi.{RSI14,RSI6}` | >50 偏多、<50 偏空；注意顶背离（价新高 RSI 不新高） |
| KDJ | `kdj.{K,D,J,bull,above_50}` | K>D 偏多、>50 偏多；J>100 短期过热 |
| 量价 | `volume.{ratio_vs_ma20,avg_ratio_5d,up_down_vol_ratio,vol_trend_10d,obv}` | 单日量能比+近5日均量比+涨跌日量比+OBV 背离 |
| 周线 | `weekly.{MA5..MA30,macd,bearish_alignment,last_week_partial}` | 空头排列判定、`last_week_partial` 为 true 时周线未收盘需标记 |
| 趋势 | `ma.{MA60,MA200,above_MA60,MA60_rising,above_MA200,MA200_rising}` | null=历史不足/无法确认 |
| 动量 | `momentum.{risk_adj_6m,m12_1_pct}` | 风险调整动量 + 12-1 动量（跳过最近一月） |
| 相对强度 | `relative_strength_pct.{SPY,QQQ}.{r3m_63d,r6m_126d}` | 对标 SPY/QQQ 的超额收益 |
| 趋势质量 | `trend_quality.{efficiency_30,regression_3m,adx}` | 效率比（干净度）+ 回归（平滑度）+ ADX（力度），三者互补 |
| 风险 | `risk.{max_drawdown_6m_pct,atr14}` | 6个月最大回撤+ATR |

ADX 解读要点（脚本字段 `trend_quality.adx.{ADX,plus_DI,minus_DI,trend_strong,bull_trend}`）：
- ADX > 25 = 强趋势市，> 40 = 极强趋势；< 20 = 震荡/无趋势
- +DI > -DI = 多头主导，反之空头主导
- `trend_strong` = ADX ≥ 25；`bull_trend` = ADX ≥ 20 且 +DI > -DI
- ADX 与 efficiency_ratio 互补：efficiency_ratio=0.95 但 ADX=18 → 干净但弱（慢牛）；efficiency_ratio=0.3 但 ADX=35 → 波动大但有力（急涨）

数据完整性：`ma.above_MA60`/`ma.MA60_rising`/`ma.above_MA200`/`ma.MA200_rising` 为 `null` 表示历史不足，记 `无法确认`，不得当作"跌破"。`bars_count` 给出可用日线根数。`weekly.last_week_partial` 为 `true` 时周线空头排列判定按未确认。

## 风控与入场条件

风控分为两层：**否决层**（由脚本 `score.risk_gates` 输出）和**确认层**（由 `score.confirmation` 给出）。模型职责是解读这些输出，不自行判断。

**否决层（由 `score.risk_gates` 给出，触发后按脚本封顶）：**
1. 200 日线：`above_MA200=false` 且 `MA200_rising=false` → risk-off
2. MA60：`above_MA60=false` → 趋势走弱
3. 周线空头排列：`weekly.bearish_alignment=true`

**确认层（由 `score.confirmation` 给出）：**
4. 技术确认：MACD/RSI/KDJ 多数偏多，无明显死叉或转弱。`confirmation.ok=false` 时，原本可能的 `是` 封顶为 `观察`。
5. 量价和趋势质量：输出 `volume_pct` 与 `trend_quality_pct` 作为解释和风险提示；不进入排名分，也不直接改变 `confirmation.ok`。

风险闸门会限制最终评分上限；确认层只降级不加分。30 日结构和当日 snapshot 是入场解释字段，不自行覆盖 `score.verdict`，除非用户明确要求按盘中执行质量做更保守处理。

## 评分规则（确定性引擎）

评分**不靠模型主观判断**：`scripts/scoring.py` 是确定性多因子引擎，分数随 `indicators.py` 输出写在每个 symbol 的 `score` 块里（`build_result` 自动计算）。**直接采用 `score.composite` 与 `score.verdict`，不要自行重算或心算评分**；模型的职责是用关键证据解释该分数。脚本失败 / 字段缺失时该项记 `无法确认`。

权重**经回测校准**（`backtest_factor_ic.py` / `backtest_score.py`）：逐因子 IC 显示只有**风险调整动量(IC≈0.10)** 与**相对强度(≈0.06)** 在 3 个月前瞻稳定为正；trend/trend_quality/technical/volume 的 IC≈0，掺入只会稀释信号。故排名分只用这两个真因子，其余各归其位（trend→否决层、technical/volume→确认）。校准后分桶前瞻收益单调、五分位多空价差约 +4%/3 月（含幸存者偏差，偏乐观）。**重要局限：edge 只在约 3 个月维度，1 个月维度无预测力——本评分是持仓/方向工具，不是择时工具。**

`score` 块字段：

- `composite`（0–100）：ALPHA 因子加权后、再经风险否决封顶的最终分。`raw_composite` 为封顶前原始分。
- `factor_breakdown`：ALPHA 因子的 `score_pct` / `points` / `weight`。权重为 **风险调整动量 60 · 相对强度 40**；缺失因子按可用权重重归一（不会无脑扣到 0）。
- `confirmation`：`{technical_pct, volume_pct, trend_quality_pct, ok}`——技术/量价/趋势质量不进排名分；`ok` 由技术确认决定，`ok=false` 时把本可成立的「是」封顶为「观察」。`volume_pct` 与 `trend_quality_pct` 用于解释风险，不直接改变 verdict。
- `risk_gates`：触发的风险否决原因（空数组表示无）。否决层封顶规则：跌破下行 200 日线 → 封顶 55；仅跌破 200 日线 → 70；周线空头排列 → 50；跌破 MA60 → 65。trend（MA200/MA60/周线）的价值集中在此，而非排名加权。
- `suggested_position_pct`：反波动率仓位建议（`目标年化波动 20% / 实现波动 × 信号强度`，上限 100%）——仅作参考，不下单。
- `data_flags`：数据不足导致的因子缺失/重归一说明。

评级映射（由引擎给出，模型照用）：

- `composite` ≥ 75 且 `risk_gates` 为空且 `confirmation.ok`：`是否值得买: 是`。
- `composite` ≥ 75 但技术未确认（`confirmation.ok=false`）：封顶为 `观察`。
- 60–74：`是否值得买: 观察`。
- < 60，或任一风险否决触发后封顶到该区间：`是否值得买: 否`。
- 已持仓且日线 MA60 失守或周线空头排列：`是否值得买: 持仓需减风险`（持仓风险管理语境，由模型按需判定）。
- `score` 缺失 / 核心数据不足：`是否值得买: 无法评分`。

## 输出格式

默认中文，先结论后证据。不要输出思考过程或工具过程。

**结论**

- 标的: `TICKER`（结论首行必须写明股票代码，便于对话与 Telegram 通知中识别是哪只）
- 是否值得买: `是`, `否`, `观察`, `持仓需减风险`, 或 `无法评分`
- 建议: `可关注/小仓位试探`, `观察等待`, `回避`, `减仓/退出`, 或 `无法评分`
- 纪律评分: `X/100`
- 强制排除条件: 列出触发项；没有则写 `无`
- 一句话: 最关键决策依据

**K 线走势**（可选）

用户希望看走势、或终端环境支持时，在结论后用 `chart.py` 打印最近约 30 根日 K（代码块包裹，避免被 Markdown 折行）。纯可视化辅助，不替代量化字段。

**关键证据**

只列 4-7 条：

- 日线趋势：最新完成日线收盘价相对 MA60，最新价相对 MA60
- 周线趋势：周线 MA5/MA10/MA20/MA30 排列
- 30 日结构：30 日收益率、区间位置、MA10/MA20/MA30、结构分类
- 技术指标：MACD（DIF/DEA/柱状、零轴）、RSI(14)、KDJ(9,3,3) 及是否有背离/金叉死叉
- 量价确认：当前量能比、近 30 日量价配合、是否放量突破或缩量背离
- 当日确认：当日涨跌幅、日内区间位置、买卖价差、确认分类
- 相对强度：30 日、3 个月、6 个月相对 SPY/QQQ 表现

**风控过滤条件**

表格列名固定为：`过滤条件`, `状态`, `关键证据`, `处理建议`。

**评分拆解**

直接引用 `score.factor_breakdown` 的 ALPHA 因子得分（风险调整动量、相对强度）与各自权重，给出 `composite/100` 与 `raw_composite`；若 `risk_gates` 非空，列出否决原因并说明已封顶。再列出 `confirmation.technical_pct/volume_pct/trend_quality_pct` 与 `suggested_position_pct`（反波动率仓位建议，仅参考）。**分数照搬脚本，不自行重算。**

**建议**

给出一个明确动作：新开仓、继续观察、回避、减仓/退出，或补充数据后重评；可结合 `suggested_position_pct` 给出仓位刻度。

只有用户要求详细数据时，才追加 **Alpaca 数据明细**，包括标的名称、交易所、状态、feed、复权方式、备用调用、最新成交/报价、日线/周线/30 日/当日字段、基准收益对比和订阅限制。

## Telegram 通知

需要把结论推送到 Telegram 时（用户要求推送，或多标的批量评估），用**子代理（subagent）完成分析、主流程再推送**：派一个子代理按上面的数据流程与评分跑完整分析，只返回精炼的决策摘要（是否值得买、纪律评分、强制排除条件、一句话依据）；主流程拿到摘要后调用通知脚本发送。这样保持主上下文整洁，分析与推送职责分离。

推送命令（文本从 stdin 或 `--text` 传入，默认纯文本以免 Markdown 转义问题）：

```bash
printf '%s' "$DECISION_SUMMARY" | python3 "$SKILL_DIR/scripts/notify_telegram.py"
```

凭证只从环境变量读取，**绝不**写入脚本或输出：

- `TELEGRAM_BOT_TOKEN`：向 @BotFather 创建 bot 获取。
- `TELEGRAM_CHAT_ID`：给 bot 发条消息后用 `getUpdates` 查 `chat.id`。

行为约定：

- 两者都配置 → 脚本发送并输出 `{"ok": true, "message_id": ...}`。
- 任一缺失 → 脚本退出码 2、输出 `{"ok": false, "error": "telegram_not_configured", "missing": [...]}` 并在 stderr 打印配置指引。此时**把配置指引转达用户**、提示其设置上述两个环境变量后重试；分析结论照常返回，不因通知未配置而中断或改判。
- 发送失败（网络/HTTP）→ 脚本输出 `{"ok": false, "error": "http_..."}`，向用户说明推送失败但分析结论有效。脚本不打印 token、不回显含 token 的 URL。

### 自动推送（Stop hook）

为避免「分析完忘了推送」，`scripts/stop_notify_hook.py` 注册在 Claude Code 全局 `~/.claude/settings.json` 的 `Stop` 事件上，会话结束时自动判定并推送，无需在分析流程里手动调用：

- **内容守卫**：解析 transcript，仅当最后一条回复同时包含「是否值得买」与「纪律评分」时才推送；普通聊天不触发。
- **精简模板（Telegram HTML）**：只抽取**股票代码 + 结论（是否值得买/建议/纪律评分/一句话）+ 关键证据列表**，用 `<b>` 标题 + `•` 列表排版；**丢弃 K 线 ASCII、表格、评分拆解等 Telegram 无法渲染的内容**，并对 `<`/`>` 做 HTML 转义。解析失败时退回纯文本兜底。
- **去重**：按 session 记录已推送的 assistant uuid，Stop 重复触发不重发。
- **非阻塞 / 永不报错**：发送 detach 到后台，hook 立即返回；任何异常都吞掉并 `exit 0`，绝不打断会话。

注意：hook 在 Claude Code 会话**启动时**加载，修改 settings.json 后需重开会话才生效。仅在 Claude Code 运行时有效；若在其他 runtime（如 Codex）里跑分析，请用上面的手动推送命令。
