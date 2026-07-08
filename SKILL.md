---
name: worth-buy-stocks
description: "Evaluate whether a US stock is worth buying, holding, reducing, avoiding, or watchlisting under the user's Alpaca-based trend-following and relative-strength framework, with news/event-risk overlay, read-only account exposure overlay, whole-market scanning, and optional multi-agent evidence collection via stable JSON contracts. Use when the user asks 值得买吗, 股票评分, buy/hold/sell/avoid/watchlist, Alpaca 行情或持仓, account exposure, 全市场扫描/找今天能买的, multi-agent stock analysis, 并行采集, agent contracts, 入场价/出场价/止盈止损, 30 天行情, 当日行情, 新闻面/事件风险, 交易纪律, 趋势跟随, 相对强度, 主升趋势, 修正阶段, momentum leadership, 量价确认, 顶背离, or 超买超卖."
---

# 值得买 - 股票版

## 硬性纪律

全文唯一权威约束，任何步骤、任何 agent 不得违反：

1. **只分析不交易**：不创建/修改/取消订单；账户与持仓只读；不使用其他券商连接器。
2. **脚本是唯一评分来源**：指标、评分、结论、计划价一律以脚本输出为准。不手算 MACD/RSI/KDJ/均线/评分，也不让 sub-agent 手算或多 agent 竞争评分再投票。
3. **新闻面只降级不加分**：利好新闻、催化剂、分析师目标价、估值叙事不改评分，不能把 `观察/否` 升级为 `是`；无来源、过期或不可验证的信息不得触发降级。
4. **数据诚实**：核心价量缺失输出 `无法评分`，其余缺失写 `无法确认`；不编造结论或持仓。
5. **不泄露凭证**：不打印、保存或输出 Alpaca/Finnhub/Telegram 密钥。
6. **feed 固定 `iex`**：只有用户明确说明有 SIP 权限并要求使用 `sip` 时才切换。
7. **最终回复是书面交易结论**：默认只输出结论，不输出思考过程、工具计划或逐步计算；不展示源码、命令参数、JSON 路径、字段名、布尔值、空数组、`null` 或内部状态，全部翻译成自然语言（如空风险项写"未触发大盘风控"）。用户明确要求原始 JSON 或调试信息时例外。
8. **仅支持美股**（Alpaca 覆盖标的）；非美股 ticker 直接说明不支持，不要强行评分。

数据角色：Alpaca 价量是主证据；新闻/公告默认作为事件风险 overlay；Alpaca 持仓默认作为只读敞口 overlay；Finnhub 是可选补充（quote 交叉校验、公司新闻、财报日历、元数据），不得替代 Alpaca 主链路。

框架取向：只参与趋势延续和相对强度领先，回避弱势修复、均值回归反弹和估值叙事。

## 环境检查

若 `alpaca`、Python 或 Alpaca 凭证未配置，不要继续评分，把 `references/setup.md` 的清单给用户在本机执行。降级规则：

- 账户/持仓读取失败：敞口 overlay 记为不可用，价量评分照常执行。
- 新闻检索能力不可用：新闻面写"无法确认，未用于降级"，价量评分照常执行。
- Finnhub 无 key 或限流：自动跳过，不影响评分。

## 数据流程（单股评分）

```bash
# SKILL_DIR = 本 skill 根目录（即本 SKILL.md 所在目录）；下面是默认安装路径，若装在别处按实际路径设置
SKILL_DIR="$HOME/.codex/skills/worth-buy-stocks"
FEED="iex"
```

1. 默认检索最近 30 天新闻/公告/监管披露：只取公司 IR、SEC、交易所公告和主流财经媒体中能影响交易纪律的 3-5 条（保留标题、日期、链接），不做新闻综述。社交媒体、无来源传闻、模型记忆、分析师目标价不能作为降级依据。整理成 `news_context.json`（形状见 `references/agent-contracts.md`）。

2. 一条命令完成价量指标 + 评分 + 账户 overlay + Finnhub 补充 + 当日 snapshot：

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols {TICKER},SPY,QQQ \
  --feed "$FEED" \
  --adjustment split \
  --llm-context-file news_context.json
```

`--start/--end` 默认省略，脚本取约两年已完成日线并计算所有指标、相对强度、市场 regime 和 `score`。多标的一次命令带全部 ticker（外加 SPY,QQQ），逐只输出结论。

脚本内部已对 `bars`、`account/positions`、`finnhub`、`snapshot` 四段网络采集并行（Finnhub 内部再按 symbol 并行），各段独立失败降级、不阻断核心评分。**不要**在脚本外重复拉 snapshot，也不要为提速手动并行或拆 sub-agent——脚本单进程线程池已是最快路径。只有脚本输出的 snapshot 缺失或可疑时，才用 `alpaca data multi-snapshots --symbols {TICKER},SPY,QQQ --feed "$FEED" --quiet` 人工核对。

可选 flag（按需追加）：

| Flag | 默认 | 作用 |
|------|------|------|
| `--snapshot auto\|on\|off` | `auto` | 当日 snapshot 写入补充信息，不参与 score |
| `--account-context auto\|on\|off` | `auto` | 只读账户/持仓生成敞口 overlay |
| `--finnhub-context auto\|on\|off` | `auto` | 有 `FINNHUB_API_KEY` 时读 Finnhub 补充；无 key 不触网 |
| `--llm-context-file path` | 无 | 新闻面风控 JSON，只降级不加分 |
| `--account-context-file` / `--finnhub-context-file` / `--snapshot-context-file` | 无 | 离线复盘：喂对应 overlay JSON |
| `--input -` | 无 | 离线模式：从 stdin 读 multi-bars JSON，不触网 |

3. 可选步骤：

- K 线（仅用户要求或有助直观看趋势）：`python3 "$SKILL_DIR/scripts/chart.py" --symbol {TICKER} --feed "$FEED" --count 30`
- ticker 歧义或需确认资产状态：`alpaca asset get --symbol-or-asset-id {TICKER} --quiet`

## 全市场扫描

用户要"扫全市场找今天能买的"时，用一条命令完成，不要手动拼 asset list → snapshot → indicators 多步：

```bash
python3 "$SKILL_DIR/scripts/scanner.py" --feed "$FEED" --adjustment split --top 20
```

流程：取股池（NYSE+NASDAQ 活跃普通股）→ 流动性初筛（IEX 日成交量 ≥5 万、价 ≥5）→ 精简批量评分 → 提取 `是` 候选 → Finnhub 新闻复核降级。输出含最终候选（按评分降序）、被新闻面降级的原"是"、市场 regime 和各阶段计数。全市场扫描耗时可达数分钟，建议后台运行并告知用户预期等待。

关键 flag：

| Flag | 默认 | 作用 |
|------|------|------|
| `--verify-news auto\|on\|off` | `auto` | auto=有 `FINNHUB_API_KEY` 才对候选做新闻复核降级 |
| `--exchange` | `NYSE,NASDAQ` | 股池来源交易所 |
| `--min-price` / `--min-volume` | `5` / `50000` | 流动性初筛阈值（IEX 量约为全市场 2-3%） |
| `--top` | `20` | 候选列表上限 |
| `--symbols` / `--symbols-file` | 无 | 覆盖股池（跳过 asset list，用于复盘） |
| `--input` | 无 | 离线复盘：读预取 JSON，不触网 |
| `--notify on` | `off` | 把摘要推 Telegram |

新闻复核这步必须做（默认 auto 已开）：漏掉会高估可买数量。扫描结果汇报仍遵循"输出格式"7 段，对每只候选单独展开。

## 新闻面风控

新闻面进入评分只做 `min(cap)` 封顶：

- `severity=high`（会计造假、停牌/退市、going-concern、要约/并购价格锚定等）：封顶 50，结论最多 `否`。
- `severity=medium` 或 `data_trust=suspect`（增发摊薄、重大诉讼、监管调查、同业重大事故传染、坏数据等）：封顶 74，`是` 降为 `观察`。
- `severity=low` 和利好 catalyst：只回显，不影响评分。

Finnhub 自动提炼只识别显式负面关键词（→ `medium/high` 候选）和 7 天内财报（→ `low` 提醒），利好忽略。若同时提供 `--llm-context-file`，手工上下文标量字段优先，来源和红旗追加合并。

## 多 agent 编排

运行时支持 sub-agent 且用户要求多 agent/并行分析时才启用；单 agent 环境按上面"数据流程"执行即可。注意：indicators.py 已自动并行采集 account/finnhub/snapshot，拆 sub-agent 不会更快，只为满足用户显式的多 agent 需求或复用离线 artifact。

主 agent 必须保留：用户意图解析、安全边界（硬性纪律全部条款）、最终评分运行（只运行 `scripts/indicators.py` 或等价 `build_result()`）、artifact 校验与合并、最终回复组织。

可委派角色（详细契约见 `references/agent-contracts.md`）：

- Market data agent → `bars` artifact（snapshot 已由脚本自动拉取，无需单独采集）。
- News/event-risk agent → `news_context` artifact；只识别风险，不写买入结论。
- Account overlay agent → `account_context` artifact；只读，失败返回 unavailable。
- Finnhub context agent → `finnhub_context` artifact；无 key/限流返回结构化状态。
- Chart agent → 仅用户要求时运行 `chart.py`。
- QA agent → 只检查最终回复是否遵守本 skill，不得改写评分或生成替代结论。

每个 artifact 喂入前用 `python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind <kind> <file>` 预检（kind：`news_context`/`account_context`/`finnhub_context`/`bars`/`snapshot_context`/`result`）。`indicators.py` 加载时会自动复检：可选 overlay 校验失败则丢弃并在 stderr 警告，核心 `bars`（`--input`）校验失败则输出 `无法评分` 退出。

## 评分合约

脚本输出的每个 symbol 都含 `score`，直接采用、不重算。核心字段：

- `score.verdict`：`是`、`观察`、`否`、`持仓需减风险` 或 `无法评分`。
- `score.composite`：最终纪律评分（0-100），已应用风险封顶。
- `score.blocking_reasons`：强制排除/降级原因；空则写"无"。
- `score.trade_plan`：入场/止损/止盈（2R/3R）/移动止损/追价上限。风控参考，不是订单。
- `score.account_overlay`：持仓状态、当前/目标仓位、建议动作、保护性出场价。不可用时不得编造持仓。
- `score.llm_overlay`：新闻面封顶回显（cap、降级原因、catalyst、红旗）。
- `score.data_flags`：历史不足、因子缺失、低流动性等提示。

辅助字段（按需引用，完整 schema 见 `scripts/README.md`）：`raw_composite`、`factor_breakdown`（momentum 55 / rel_strength 35 / efficiency 10）、`risk_gates`、`confirmation`、`suggested_position_pct`、`supplemental.finnhub`。

`score` 缺失、关键字段缺失或脚本失败时输出 `无法评分`。字段名只供内部取数，最终答复按硬性纪律第 7 条翻译成自然语言。

## 输出格式

默认中文，先结论后证据。最终回复必须包含 7 个标题，顺序固定：`结论`、`关键证据`、`风控过滤条件`、`评分拆解`、`账户敞口与交易计划`、`新闻面风控`、`建议`，不得省略或合并。所有价格、仓位、止损、止盈、封顶结论必须来自脚本；缺字段写"无法确认"，不要猜。不要用代码样式包裹普通数值或状态（股票代码、命令和用户要求的原始字段例外）。

例外一：`无法评分` 时不硬套 7 段，简短说明缺失的数据和补救步骤（如安装 CLI、检查 ticker、补历史数据）。

例外二（轻量模式）：用户明确要求简短（如"简单说""快速看""一句话"）时，只输出 `结论` 段——内容仍全部来自脚本评分，仍注明不构成下单指令——并在末尾提示可展开完整分析；用户追问时再给完整 7 段。评分流程本身不简化。

**结论**

- 标的: TICKER
- 是否值得买: 是 / 观察 / 否 / 持仓需减风险 / 无法评分
- 建议: 新开仓 / 观察等待 / 回避 / 减仓退出 / 补充数据后重评
- 纪律评分: X/100
- 当前持仓: 无 / 已持仓 X% 账户权益 / 账户持仓无法确认
- 建议入场价: 脚本计划价；不建议新开仓写"不建议入场"
- 建议出场价: 脚本止损价；已持仓优先写保护性出场价
- 止盈参考: 第一/第二止盈价，注明移动止损比例
- 强制排除条件: 自然语言原因；没有写"无"
- 一句话: 最关键依据

**关键证据**

列 4-7 条：日线趋势、周线趋势、30 日结构、相对 SPY/QQQ 强度、技术确认、量价、当日入场质量。

**风控过滤条件**

表格列名固定：`过滤条件`, `状态`, `关键证据`, `处理建议`。固定 8 行，没有触发风险也要写"通过/未触发"，不能省略：

| 过滤条件 | 状态 | 关键证据 | 处理建议 |
|---|---|---|---|
| 大盘 regime | 通过 / 风险 | 大盘趋势与是否触发风险；无风险写"未触发大盘风控" | 大盘风险时不新增或降至观察 |
| 个股趋势闸门 | 通过 / 风险 | MA60、MA200、周线排列和关键趋势位 | 跌破关键均线不新开；已持仓按保护价管理 |
| 相对强度 | 通过 / 偏弱 / 无法确认 | 相对 SPY/QQQ 的 3m/6m 强弱 | 跑输基准则等待重评 |
| 30 日结构与追价 | 通过 / 过热 / 破位 | 30 日位置、回踩价、突破价、追价上限 | 高于追价上限不追；等回踩或突破确认 |
| 技术与量价确认 | 通过 / 未确认 | 技术确认、MACD/RSI/KDJ 和成交量是否达标 | 确认不足时只观察不买 |
| 流动性/数据质量 | 通过 / 风险 / 无法确认 | 历史数据、snapshot 新鲜度、价差和成交量；无风险写"未发现数据质量问题" | 数据异常时补数据后重评 |
| 新闻/事件红旗 | 未触发 / 低 / 中 / 高 / 无法确认 | 红旗等级、可验证来源、是否触发封顶；无红旗写"未发现中高等级红旗" | 中高风险只降级；不可用时不用于升级 |
| 账户敞口 | 无持仓 / 已持仓 / 无法确认 / 风险 | 当前仓位、目标仓位、现金比例、是否超配 | 超目标减仓；无持仓按入场计划，不下单 |

**评分拆解**

至少写明动量、相对强度、趋势效率三项得分/贡献，以及确认状态和建议仓位。若已封顶，用自然语言说明触发原因和封顶后影响。

**账户敞口与交易计划**

读取到持仓时：当前持仓占账户权益比例、目标仓位、差额、成本价、浮盈亏、保护性出场价、移动止损、2R/3R 止盈、追价上限和处理建议。无持仓时写"当前无持仓，不构成下单指令"，仍给出入场/止损/止盈/追价上限。账户读取失败写"账户持仓无法确认，未用于调整建议"。

**新闻面风控**

写新闻检索状态、Finnhub 补充状态、主要 catalyst、红旗、来源日期/链接、是否触发封顶。无红旗写"未发现可验证的重大新闻红旗，未影响评分"；不可用写"新闻面无法确认，未用于降级"。

**建议**

给出一个明确动作并结合账户处理建议：无持仓且计划允许入场可写"按计划价分批新开"；`持仓需减风险` 明确写"减仓至目标敞口 / 跌破保护性出场价退出"；账户 overlay 不可用则动作只基于价量和新闻面。仅在用户要求详细数据时追加 Alpaca 明细、完整 JSON 或 K 线图。

输出前自检（不写进回复）：7 个标题齐全且顺序正确（轻量模式只查 `结论` 段完整）；`结论` 段含入场/出场/止盈/持仓/强制排除条件；风控表 8 行齐全且每行有处理建议；交易计划段明确"不下单"；新闻段明确红旗或不可用状态；全文无字段路径、snake_case、空数组/对象、`true/false/null`、命令参数等源码式文本。

## Telegram

需要推送时：

```bash
printf '%s' "$DECISION_SUMMARY" | python3 "$SKILL_DIR/scripts/notify_telegram.py"
```

凭证从本地 `.env` 读取 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（查找顺序：`WORTH_BUY_STOCKS_ENV_FILE` 指定路径 → skill 根目录 `.env` → `~/.config/worth-buy-stocks/.env`），环境变量作为 fallback。未配置时转达脚本提示，分析结论不受影响。
