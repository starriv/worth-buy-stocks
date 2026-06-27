---
name: worth-buy-stocks
description: "Evaluate whether an individual stock is worth buying, holding, reducing, avoiding, or watchlisting under the user's Alpaca-based trend-following and relative-strength framework with default news/event risk and account-position exposure overlay. Use when the user asks 值得买吗, 股票版, 股票评分, buy/hold/sell/avoid/watchlist, Alpaca market data, Alpaca 持仓, account exposure, 入场价, 出场价, 止盈止损, 30 天行情, 当日行情, 新闻面, 事件风险, 交易纪律, 趋势跟随, 相对强度, 主升趋势, 修正阶段, 日线 MA60, 周线均线空头排列, momentum leadership, countertrend-entry avoidance, 量价确认, 顶背离, or 超买超卖."
---

# 值得买 - 股票版

## 原则

把交易纪律执行为趋势跟随与相对强度框架：只参与趋势延续和相对强度领先，回避弱势修复、均值回归反弹和估值叙事。

- Alpaca Market Data 是价量主证据；新闻/公告默认作为事件风险 overlay；Alpaca 账户持仓默认作为只读敞口 overlay。
- Finnhub 可作为可选补充数据源，用于 quote 交叉校验、公司新闻、财报日历和公司元数据；脚本会把明确负面新闻/临近财报压缩为候选 `llm_context` 风险，但不得替代 Alpaca 主价量链路。
- 不使用其他券商连接器，不下单，不打印或保存 Alpaca/Telegram 密钥。
- 默认输出结论，不输出思考过程、工具计划、逐步计算或长篇解释。
- 指标、评分和结论以脚本输出为准；不要手算 MACD/RSI/KDJ/均线/评分。
- 新闻面只降级或解释，不能加分，不能把 `观察/否` 升级为 `是`。
- 核心数据缺失时输出 `无法评分` 或 `无法确认`，不要编造结论。

## 首次使用

如果 `alpaca`、Python 或 Alpaca 凭证未配置，不要继续评分；先给用户下面的本地设置清单。不要让用户把密钥发到对话里。

必需依赖：

- Python 3.10+：运行本 skill 的本地脚本。
- Alpaca CLI：提供 Market Data snapshot 和日线数据。
- Alpaca API keys：paper 或 live 账户均可，至少需要 Market Data 权限；若要纳入当前持仓/账户敞口，需要同一 profile 可读取 account / positions。

安装 Alpaca CLI：

```bash
# macOS / Homebrew
brew install alpacahq/tap/cli

# 或 Go
go install github.com/alpacahq/cli/cmd/alpaca@latest
```

配置凭证（二选一）：

```bash
# 推荐：让 CLI 管理 profile
alpaca profile login

# 或本地 shell 环境变量；只在用户机器设置，不要写入 skill 或输出
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
# 默认数据源固定使用 iex。没有 SIP 权限时不要设置为 sip。
export ALPACA_DATA_FEED="iex"
```

验证环境：

```bash
python3 --version
alpaca version
alpaca data multi-snapshots --symbols SPY,QQQ --feed iex --quiet
alpaca account get --quiet
alpaca position list --quiet
```

若 `alpaca` 不存在，提示用户先安装 CLI；若认证失败，提示用户运行 `alpaca profile login` 或设置 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`；默认使用 `iex`，只有用户明确说明有 SIP 权限并要求使用 `sip` 时才切换。账户/持仓读取失败时，`score.account_overlay.status` 写 `unavailable`，价量评分照常执行。新闻检索能力不可用时，新闻面写“无法确认，未用于降级”，价量评分照常执行。

可选依赖：

- Finnhub API key：用于补充 quote/news/profile/earnings。优先从 skill 根目录 `.env` 的 `FINNHUB_API_KEY` 读取；无 key 时自动跳过。
- Telegram 推送仅在用户要求通知时需要：优先从 skill 根目录 `.env` 读取 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。

## 数据流程

设定路径和 feed：

```bash
SKILL_DIR="$HOME/.codex/skills/worth-buy-stocks"
FEED="iex"
```

1. 获取当日 snapshot：

```bash
alpaca data multi-snapshots --symbols {TICKER},SPY,QQQ --feed "$FEED" --quiet
```

提取最新价、时间、bid/ask、价差、日内涨跌幅、日内位置、成交量和数据是否陈旧。只有批量 snapshot 缺字段时再用单票备用调用。

2. 默认检索最近 30 天新闻/公告/监管披露。

优先公司 IR、SEC、交易所公告和主流财经媒体；保留标题、发布日期、链接。不要把社交媒体、无来源传闻、模型记忆或分析师目标价当作降级依据。高性能要求：只抓能影响交易纪律的 3-5 条材料，不做新闻综述。

3. 运行评分脚本（一条命令完成价量指标 + 评分 + 账户 overlay + Finnhub 补充）：

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols {TICKER},SPY,QQQ \
  --feed "$FEED" \
  --adjustment split
```

`--start/--end` 默认省略，脚本取约两年已完成日线并计算所有指标、相对强度、市场 regime 和 `score`。默认 feed 是 `iex`；不要主动使用 `sip`，除非用户明确要求。

可选 flag（按需追加到上面的命令）：

| Flag | 默认 | 作用 |
|------|------|------|
| `--account-context auto\|on\|off` | `auto` | 非离线模式只读 Alpaca 账户/持仓，生成敞口 overlay；失败不阻断评分 |
| `--finnhub-context auto\|on\|off` | `auto` | 有 `.env`/`FINNHUB_API_KEY` 时读 Finnhub 补充（quote/news/profile/earnings）；无 key 不触网 |
| `--llm-context-file news_context.json` | 无 | 传入新闻面风控 JSON（红旗/数据存疑），只降级不加分 |
| `--account-context-file path` | 无 | 离线复盘：喂账户/持仓 JSON |
| `--finnhub-context-file path` | 无 | 离线复盘：喂 Finnhub 补充 JSON |
| `--input -` | 无 | 离线模式：从 stdin 读 multi-bars JSON，不触网 |

Finnhub `auto` 模式会自动从 `company-news`/`earnings-calendar` 生成保守的 `llm_context` 候选：增发/调查/诉讼等明确负面关键词可触发 `medium/high` 红旗；7 天内财报只作为 `severity=low` 事件提醒；利好新闻忽略。若同时提供 `--llm-context-file`，手工上下文标量字段优先，来源和红旗追加合并。账户 overlay 失败时 `score.account_overlay.status=unavailable`，价量评分照常执行。

4. 可选 K 线，仅在用户要求或有助于直观看趋势时运行：

```bash
python3 "$SKILL_DIR/scripts/chart.py" --symbol {TICKER} --feed "$FEED" --count 30
```

5. 只在 ticker 有歧义或需要确认资产状态时运行：

```bash
alpaca asset get --symbol-or-asset-id {TICKER} --quiet
```

## 新闻面风控

新闻面是 `score.llm_overlay` 的输入，只做 `min(cap)` 封顶：

- `severity=high`：会计造假、停牌/退市、going-concern、要约/并购导致价格锚定等，封顶 50，结论最多 `否`。
- `severity=medium` 或 `data_trust=suspect`：增发摊薄、重大诉讼、监管调查、同业重大事故传染、拆股/停牌/坏数据等，封顶 74，`是` 降为 `观察`。
- `severity=low` 和利好 catalyst：只回显，不影响评分。
- 无来源、过期或无法验证的信息不能触发 `medium/high`。
- Finnhub 自动提炼只识别显式负面关键词和 7 天内财报；正面新闻不会升级结论。

`news_context.json` 形状保持简短：

```json
{
  "TICKER": {
    "as_of": "2026-06-19T00:00:00Z",
    "sources": [{"id": "s1", "title": "...", "published_at": "YYYY-MM-DD", "url": "https://..."}],
    "red_flags": [{"type": "dilution", "severity": "medium", "note": "...", "source_id": "s1"}],
    "data_trust": "ok",
    "catalyst": "..."
  }
}
```

## 评分合约

`scripts/indicators.py` 输出的每个 symbol 都包含 `score`。直接采用脚本值，不自行重算。LLM 输出必须引用的核心字段：

- `score.verdict`：`是`、`观察`、`否`、`持仓需减风险` 或 `无法评分`。
- `score.composite`：最终纪律评分（0-100），已应用风险封顶。
- `score.blocking_reasons`：强制排除/降级原因（价格闸门 + 新闻面降级）。没有写 `无`。
- `score.trade_plan`：入场/出场计划，含 `suggested_entry_price`、`stop_loss_price`、`take_profit_price`、`take_profit_2_price`、`trailing_stop_pct`。风控参考，不是订单。
- `score.account_overlay`：账户持仓 overlay，含 `holding_status`、`current_position_pct`、`target_position_pct`、`suggested_action`、`position_plan.protective_exit_price`。`status=unavailable` 时不得编造持仓。
- `score.llm_overlay`：新闻面风控回显（`cap`、`downgrade_reasons`、`catalyst`、`red_flags`）。
- `score.data_flags`：历史不足、因子缺失、低流动性等提示。

辅助字段（按需引用，完整 schema 见 `scripts/README.md`）：`raw_composite`、`factor_breakdown`（momentum 55 / rel_strength 35 / efficiency 10）、`risk_gates`、`confirmation`、`suggested_position_pct`、`supplemental.finnhub`。

评级由脚本决定。若 `score` 缺失、关键字段缺失或脚本失败，输出 `无法评分`。

## 输出格式

默认中文，先结论后证据。只保留决策需要的信息。

**结论**

- 标的: `TICKER`
- 是否值得买: `是` / `观察` / `否` / `持仓需减风险` / `无法评分`
- 建议: 新开仓 / 观察等待 / 回避 / 减仓退出 / 补充数据后重评
- 纪律评分: `X/100`
- 当前持仓: 无 / 已持仓 `X%` 账户权益 / 账户持仓无法确认
- 建议入场价: 使用 `score.trade_plan.suggested_entry_price`；若不建议新开仓写“不建议入场”
- 建议出场价: 使用 `score.trade_plan.stop_loss_price` 或持仓时 `score.account_overlay.position_plan.protective_exit_price`
- 止盈参考: 使用 `score.trade_plan.take_profit_price` / `take_profit_2_price`
- 强制排除条件: 使用 `score.blocking_reasons`；没有写 `无`
- 一句话: 最关键依据

**关键证据**

列 4-7 条即可：日线趋势、周线趋势、30 日结构、相对 SPY/QQQ 强度、技术确认、量价、当日入场质量。

**风控过滤条件**

用表格，列名固定为：`过滤条件`, `状态`, `关键证据`, `处理建议`。

**评分拆解**

引用 `score.factor_breakdown`、`composite/raw_composite`、`confirmation`、`suggested_position_pct`、`trade_plan`。若已封顶，说明触发的 `blocking_reasons`。

**账户敞口与交易计划**

固定放在“评分拆解”后、“新闻面风控”前。读取到 Alpaca 持仓时，写当前持仓市值占账户权益比例、脚本目标仓位、差额、成本价、浮盈亏、保护性出场价、止盈参考和 `account_overlay.suggested_action`。没有持仓时，写“当前无持仓”；账户读取失败时，写“账户持仓无法确认，未用于调整建议”。任何情况下都不要下单。

**新闻面风控**

固定放在“评分拆解”后面。写新闻检索状态、Finnhub 补充状态、主要 catalyst、红旗、来源日期/链接、是否触发 `llm_overlay.cap`。无红旗写“未发现可验证的重大新闻红旗，未影响评分”；新闻不可用写“新闻面无法确认，未用于降级”。Finnhub 新闻/财报只能帮助识别事件风险，不得把利好新闻用于加分。

**建议**

给出一个明确动作，并结合 `account_overlay.suggested_action`。若无持仓且 `trade_plan.status=entry_allowed`，可以写“按计划价分批新开”；若已有持仓且 verdict 为 `持仓需减风险`，明确写“减仓至目标敞口 / 跌破保护性出场价退出”；若账户 overlay 不可用，动作只基于价量和新闻面。只有用户要求详细数据时，才追加 Alpaca 明细、完整 JSON 字段或 K 线图。

## Telegram

需要推送时，把最终精简结论通过通知脚本发送：

```bash
printf '%s' "$DECISION_SUMMARY" | python3 "$SKILL_DIR/scripts/notify_telegram.py"
```

凭证优先从 skill 根目录 `.env` 读取 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，环境变量作为 fallback。未配置时，转达脚本提示；分析结论不受影响。

Claude Code Stop hook 会在最后回复同时包含「是否值得买」和「纪律评分」时自动推送精简摘要：股票代码、结论、建议、纪律评分、强制排除条件、一句话和关键证据。Codex 或其他 runtime 中需要手动调用通知脚本。
