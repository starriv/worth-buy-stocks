---
name: worth-buy-stocks
description: "Evaluate whether an individual stock is worth buying, holding, reducing, avoiding, or watchlisting under the user's Alpaca-based trend-following and relative-strength framework with default news/event risk and account-position exposure overlay, including multi-agent evidence collection via stable JSON contracts. Use when the user asks 值得买吗, 股票版, 股票评分, buy/hold/sell/avoid/watchlist, Alpaca market data, Alpaca 持仓, account exposure, multi-agent stock analysis, agent contracts, 并行采集, 多 agent, 入场价, 出场价, 止盈止损, 30 天行情, 当日行情, 新闻面, 事件风险, 交易纪律, 趋势跟随, 相对强度, 主升趋势, 修正阶段, 日线 MA60, 周线均线空头排列, momentum leadership, countertrend-entry avoidance, 量价确认, 顶背离, or 超买超卖."
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

## 多 agent 编排

运行时支持 sub-agent 且用户要求多 agent/并行分析，或同一请求同时需要新闻、账户、Finnhub、图表和复核时，采用主 agent 编排模式。单 agent 环境仍按“数据流程”执行。

主 agent 必须保留：

- 用户意图解析：ticker、feed、是否要账户 overlay、新闻面、K 线图、Telegram。
- 安全边界：不下单、不使用其他券商连接器、不输出密钥、不让 sub-agent 手算指标。
- 最终评分运行：只运行 `scripts/indicators.py` 或等价的 `build_result()`，以脚本 `score` 为唯一结论来源。
- Artifact 合并：把 sub-agent 产物保存为 JSON 文件，校验后通过 `--llm-context-file`、`--account-context-file`、`--finnhub-context-file`、`--snapshot-context-file`、`--input` 传入。
- 最终回复：直接引用 `score.verdict`、`score.composite`、`score.trade_plan`、`score.account_overlay`、`score.llm_overlay`。

允许委派的角色：

- Market data agent：只拉取 Alpaca bars 或执行离线 bars 准备，返回 `bars` artifact 或失败状态。当日 snapshot 由 `indicators.py` 自动并行拉取并写入 `supplemental`，无需 sub-agent 单独采集。
- News/event-risk agent：检索最近 30 天 IR、SEC、交易所公告和主流财经媒体，返回 `news_context` artifact；只识别风险，不写买入结论。
- Account overlay agent：只读 Alpaca account/positions，返回 `account_context` artifact；失败返回 `status=unavailable`。
- Finnhub context agent：可选读取 quote/news/profile/earnings，返回 `finnhub_context` artifact；无 key 或限流返回结构化 unavailable/rate_limited。
- Chart agent：只在用户要求或主 agent 需要视觉确认时运行 `scripts/chart.py`。
- QA agent：只检查最终回复是否遵守本 skill；不得改写脚本评分或生成替代结论。

禁止委派：

- 把 MACD/RSI/KDJ/均线/评分交给 sub-agent 手算。
- 让多个 sub-agent 产生互相竞争的买/卖评级再投票。
- 让新闻、利好催化剂或分析师目标价加分或升级结论。
- 创建、修改或取消订单。

多 agent artifact 的字段契约见 `references/agent-contracts.md`。要求 sub-agent 只返回机器可读 JSON 或清楚说明无法生成；主 agent 可用下面的校验脚本检查产物：

```bash
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind news_context news_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind account_context account_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind finnhub_context finnhub_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind bars bars.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind snapshot_context snapshot_context.json
python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind result result.json
```

`scripts/indicators.py` 在加载每个 `--*-file` artifact 时会自动调用同一套验证器：可选 overlay（news/account/finnhub/snapshot）校验失败时丢弃该 overlay 并在 stderr 警告，核心价量评分照常执行；核心 `bars`（`--input`）校验失败时输出 `无法评分` 并退出。评分结束后还会对 `result` 做一次自检，自检失败仅在 stderr 警告、不阻断输出。主 agent 仍应在喂入前用上面的 CLI 预检，以便在 sub-agent 产物有问题时提前要求重新生成。

## 数据流程

设定路径和 feed：

```bash
SKILL_DIR="$HOME/.codex/skills/worth-buy-stocks"
FEED="iex"
```

1. 当日 snapshot：由 `indicators.py` 自动拉取，无需主 agent 单独运行。

`indicators.py`（见第 3 步）在非离线模式下会自动调用 `alpaca data multi-snapshots`，提取最新价、bid/ask、价差、日内涨跌幅、成交量和最新成交时间，写入 `result.supplemental.snapshots`（顶层 summary）和每个 `symbol.supplemental.snapshot`（明细）。snapshot 只作补充信息，不参与 `score`。失败降级为 `status=unavailable`，不阻断评分。

不要在脚本之外再单独跑一遍 `multi-snapshots`——那只是重复一轮网络往返。只有脚本输出里 snapshot 字段缺失或可疑时，才用 `alpaca data multi-snapshots --symbols {TICKER},SPY,QQQ --feed "$FEED" --quiet` 人工核对。

2. 默认检索最近 30 天新闻/公告/监管披露。

优先公司 IR、SEC、交易所公告和主流财经媒体；保留标题、发布日期、链接。不要把社交媒体、无来源传闻、模型记忆或分析师目标价当作降级依据。高性能要求：只抓能影响交易纪律的 3-5 条材料，不做新闻综述。

3. 运行评分脚本（一条命令完成价量指标 + 评分 + 账户 overlay + Finnhub 补充 + 当日 snapshot）：

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols {TICKER},SPY,QQQ \
  --feed "$FEED" \
  --adjustment split
```

`--start/--end` 默认省略，脚本取约两年已完成日线并计算所有指标、相对强度、市场 regime 和 `score`。默认 feed 是 `iex`；不要主动使用 `sip`，除非用户明确要求。

脚本内部已对四段网络采集做并行：`bars`（两年日线）、`account`/`positions`、`finnhub`、`snapshot` 同时拉取，Finnhub 内部再对多 symbol 并行。主 agent **不要**为提速去手动并行这几段或拆 sub-agent——脚本单进程线程池已是最快路径，sub-agent 的启动/上下文成本只会更慢。各段独立失败降级，不阻断核心价量评分。

可选 flag（按需追加到上面的命令）：

| Flag | 默认 | 作用 |
|------|------|------|
| `--snapshot auto\|on\|off` | `auto` | 非离线模式拉取当日 snapshot（最新价/报价/成交），写入 `supplemental`，不参与 score；失败降级不阻断 |
| `--account-context auto\|on\|off` | `auto` | 非离线模式只读 Alpaca 账户/持仓，生成敞口 overlay；失败不阻断评分 |
| `--finnhub-context auto\|on\|off` | `auto` | 有 `.env`/`FINNHUB_API_KEY` 时读 Finnhub 补充（quote/news/profile/earnings）；无 key 不触网 |
| `--llm-context-file news_context.json` | 无 | 传入新闻面风控 JSON（红旗/数据存疑），只降级不加分 |
| `--account-context-file path` | 无 | 离线复盘：喂账户/持仓 JSON |
| `--finnhub-context-file path` | 无 | 离线复盘：喂 Finnhub 补充 JSON |
| `--snapshot-context-file path` | 无 | 离线复盘：喂当日 snapshot JSON |
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

## 全市场扫描

当用户要"扫全市场找今天能买的"时，用 `scripts/scanner.py` 一条命令完成，不要再手动拼 asset list → snapshot → indicators 多步。扫描器内部复用 `build_result` 做两轮评分：精简轮（无 overlay）对流动性初筛后的活跃池批量评分、提取 `verdict=="是"`（已隐含 `confirmation.ok`）；复核轮仅对候选开 Finnhub 新闻 overlay，把被软红旗降级（是→观察）的剔到 `downgraded`。

```bash
python3 "$SKILL_DIR/scripts/scanner.py" --feed "$FEED" --adjustment split --top 20
```

流程：取股池（`alpaca asset list` NYSE+NASDAQ 活跃普通股）→ 流动性初筛（snapshot 的 IEX 日成交量 ≥ `--min-volume` 默认 5 万、价 ≥ `--min-price` 默认 5）→ 精简批量评分 → 提取 `是` 候选 → 新闻复核降级。输出 JSON 含 `candidates`（最终可买，按 composite 降序）、`downgraded`（被新闻面降级的原"是"，带 `cap_applied` 与 `downgrade_reasons`）、`market_regime`、`counts`。

关键 flag：

| Flag | 默认 | 作用 |
|------|------|------|
| `--verify-news auto\|on\|off` | `auto` | auto=有 `FINNHUB_API_KEY` 才对候选跑新闻复核降级；off=只输出精简轮全部"是"（会漏掉被软红旗降级的） |
| `--exchange NYSE,NASDAQ` | `NYSE,NASDAQ` | 股池来源交易所 |
| `--min-price` / `--min-volume` | `5` / `50000` | 流动性初筛阈值（IEX 量约为全市场 2-3%） |
| `--snapshot-chunk` / `--bars-chunk` | `200` / `80` | snapshot / multi-bars 分块大小 |
| `--top` | `20` | 候选列表上限 |
| `--symbols` / `--symbols-file` | 无 | 覆盖股池（跳过 asset list，用于复盘） |
| `--input` | 无 | 离线复盘：读预取 JSON `{assets,snapshots,bars,finnhub}`，不触网 |
| `--notify on` | `off` | 把摘要推 Telegram |

`--verify-news` 复核这一步必须做（默认 auto 已开）：Finnhub 自动红旗会把标题含"investigation/litigation/dilution"等的"是"候选降级为"观察"，漏掉这步会高估可买数量。扫描结果汇报仍遵循"输出格式"7 段，对每只候选单独展开。

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

默认中文，先结论后证据。不要只给一句买/不买；最终回复必须包含下面 7 个标题，顺序固定为：`结论`、`关键证据`、`风控过滤条件`、`评分拆解`、`账户敞口与交易计划`、`新闻面风控`、`建议`。除非核心数据缺失导致 `无法评分`，否则不得省略或合并这些标题。

硬性完整性规则：

- 所有价格、仓位、止损、止盈和封顶结论都必须来自 `score` 字段；缺字段时写“无法确认”，不要猜。
- `结论` 段必须写清：是否值得买、建议动作、纪律评分、当前持仓、建议入场价、建议出场价、止盈参考、强制排除条件和一句话依据。
- `风控过滤条件` 表至少覆盖：大盘 regime、个股趋势闸门、相对 SPY/QQQ 强度、30 日结构/追价质量、技术与量价确认、流动性/数据质量、新闻/事件红旗、账户敞口。没有触发风险也要写“通过/未触发”，不要省略。
- `账户敞口与交易计划` 必须写保护性出场价、移动止损、2R/3R 止盈、追价上限和仓位处理；无持仓也要写“当前无持仓，不构成下单指令”。
- `新闻面风控` 必须写新闻检索状态和红旗状态；新闻不可用时写“新闻面无法确认，未用于降级”。

**结论**

- 标的: `TICKER`
- 是否值得买: `是` / `观察` / `否` / `持仓需减风险` / `无法评分`
- 建议: 新开仓 / 观察等待 / 回避 / 减仓退出 / 补充数据后重评
- 纪律评分: `X/100`
- 当前持仓: 无 / 已持仓 `X%` 账户权益 / 账户持仓无法确认
- 建议入场价: 使用 `score.trade_plan.suggested_entry_price`；若不建议新开仓写“不建议入场”
- 建议出场价: 使用 `score.trade_plan.stop_loss_price` 或持仓时 `score.account_overlay.position_plan.protective_exit_price`
- 止盈参考: 使用 `score.trade_plan.take_profit_price` / `take_profit_2_price`，并注明 `trailing_stop_pct`
- 强制排除条件: 使用 `score.blocking_reasons`；没有写 `无`
- 一句话: 最关键依据

**关键证据**

列 4-7 条即可：日线趋势、周线趋势、30 日结构、相对 SPY/QQQ 强度、技术确认、量价、当日入场质量。

**风控过滤条件**

用表格，列名固定为：`过滤条件`, `状态`, `关键证据`, `处理建议`。

固定行（按可用字段填写，不能整块省略）：

| 过滤条件 | 状态 | 关键证据 | 处理建议 |
|---|---|---|---|
| 大盘 regime | 通过 / 风险 | `market_risk_off` 或 `score.risk_gates` 中的大盘项 | risk-off 时不新增或降至观察 |
| 个股趋势闸门 | 通过 / 风险 | MA60、MA200、周线排列、`score.risk_gates` | 跌破关键均线时不新开；已持仓按保护价管理 |
| 相对强度 | 通过 / 偏弱 / 无法确认 | 相对 SPY/QQQ 的 3m/6m 强弱 | 跑输基准则等待重评 |
| 30 日结构与追价 | 通过 / 过热 / 破位 | 30 日高低位、`pullback_entry_price`、`breakout_entry_price`、`max_chase_price` | 高于追价上限不追；等回踩或突破确认 |
| 技术与量价确认 | 通过 / 未确认 | `score.confirmation`、MACD/RSI/KDJ/成交量描述 | `confirmation.ok=false` 时只观察不买 |
| 流动性/数据质量 | 通过 / 风险 / 无法确认 | `score.data_flags`、snapshot 新鲜度、价差/成交量 | 数据异常时补数据后重评 |
| 新闻/事件红旗 | 未触发 / 低 / 中 / 高 / 无法确认 | `score.llm_overlay.red_flags`、`downgrade_reasons`、`cap` | 中高风险只降级；不可用时不用于升级 |
| 账户敞口 | 无持仓 / 已持仓 / 无法确认 / 风险 | `score.account_overlay` 当前仓位、目标仓位、差额 | 超目标减仓；无持仓按入场计划，不下单 |

**评分拆解**

引用 `score.factor_breakdown`、`composite/raw_composite`、`confirmation`、`suggested_position_pct`、`trade_plan`。若已封顶，说明触发的 `blocking_reasons` 和 `cap_applied`。至少写明 momentum、relative strength、efficiency 三项的得分/贡献。

**账户敞口与交易计划**

固定放在“评分拆解”后、“新闻面风控”前。读取到 Alpaca 持仓时，写当前持仓市值占账户权益比例、脚本目标仓位、差额、成本价、浮盈亏、保护性出场价、移动止损、2R/3R 止盈参考、追价上限和 `account_overlay.suggested_action`。没有持仓时，写“当前无持仓”，仍要给出 `trade_plan` 的入场/止损/止盈/追价上限；账户读取失败时，写“账户持仓无法确认，未用于调整建议”。任何情况下都不要下单。

**新闻面风控**

固定放在“账户敞口与交易计划”后面。写新闻检索状态、Finnhub 补充状态、主要 catalyst、红旗、来源日期/链接、是否触发 `llm_overlay.cap`。无红旗写“未发现可验证的重大新闻红旗，未影响评分”；新闻不可用写“新闻面无法确认，未用于降级”。Finnhub 新闻/财报只能帮助识别事件风险，不得把利好新闻用于加分。

**建议**

给出一个明确动作，并结合 `account_overlay.suggested_action`。若无持仓且 `trade_plan.status=entry_allowed`，可以写“按计划价分批新开”；若已有持仓且 verdict 为 `持仓需减风险`，明确写“减仓至目标敞口 / 跌破保护性出场价退出”；若账户 overlay 不可用，动作只基于价量和新闻面。只有用户要求详细数据时，才追加 Alpaca 明细、完整 JSON 字段或 K 线图。

输出前自检（不要把自检过程写进最终回复）：

- 7 个标题是否齐全且顺序正确。
- `结论` 段是否包含入场、出场、止盈、持仓和强制排除条件。
- `风控过滤条件` 是否包含固定 8 行，且每行都有处理建议。
- `账户敞口与交易计划` 是否明确“不下单”，并写出保护性出场价、移动止损、2R/3R 止盈和追价上限。
- `新闻面风控` 是否明确红旗/不可用状态，且没有把利好新闻用于升级。

## Telegram

需要推送时，把最终精简结论通过通知脚本发送：

```bash
printf '%s' "$DECISION_SUMMARY" | python3 "$SKILL_DIR/scripts/notify_telegram.py"
```

凭证优先从 skill 根目录 `.env` 读取 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，环境变量作为 fallback。未配置时，转达脚本提示；分析结论不受影响。
