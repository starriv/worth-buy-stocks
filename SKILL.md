---
name: worth-buy-stocks
description: "Evaluate whether a US stock is worth buying, holding, reducing, avoiding, or watchlisting under the user's Alpaca-based trend-following and relative-strength framework, with news/event-risk overlay, read-only account exposure overlay, display-only AI second opinion, whole-market scanning, SQLite market-data caching, a persistent Alpaca asset catalog, safe historical warmup and coverage reconciliation, and optional multi-agent evidence collection via stable JSON contracts. Use when the user asks 值得买吗, 股票评分, buy/hold/sell/avoid/watchlist, Alpaca 行情或持仓, account exposure, 全市场扫描/找今天能买的, 行情缓存/历史数据预热/全市场三年数据/资产目录/查漏补缺, multi-agent stock analysis, 并行采集, agent contracts, 入场价/出场价/止盈止损, 30 天行情, 当日行情, 新闻面/事件风险, AI 意见/AI 分析对比, 交易纪律, 趋势跟随, 相对强度, 主升趋势, 修正阶段, momentum leadership, 量价确认, 顶背离, or 超买超卖."
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
7. **最终回复是书面交易决策**：默认只输出结论，不输出思考过程、工具计划或逐步计算；不展示源码、命令参数、JSON 路径、字段名、布尔值、空数组、`null` 或内部状态，全部翻译成自然语言。每一项都写成正式决策或事实陈述（"通过""维持观察""按 X 价分批"），两类句式禁止出现：(a) 机制性否定——描述评分内部"没发生什么"的句子，如"未影响评分""评分未被新闻压低""不构成临近财报降级""未用于降级/升级/调整建议"；(b) 免责声明与限定语，如"仅为分析参考""仅作重评参照""不构成下单指令"。未触发的环节直接给结论状态（"通过"），不解释机制；触发了的只写事实原因与决策结果。用户明确要求原始 JSON 或调试信息时例外。
8. **仅支持美股**（Alpaca 覆盖标的）；非美股 ticker 直接说明不支持，不要强行评分。

数据角色：Alpaca 价量是主证据；新闻/公告默认作为事件风险 overlay；Alpaca 持仓默认作为只读敞口 overlay；Finnhub 是可选补充（quote 交叉校验、公司新闻、财报日历、元数据），不得替代 Alpaca 主链路。AI 意见（脚本内置调用 Claude API 生成）默认作为对照展示 overlay：只用于让用户对比"量价纪律结论 vs AI 判断"，不参与评分、不触发降级、不可用于升级或改写结论；不可用时如实写"无法确认"，不得编造或自行补写 AI 意见。

框架取向：主路径参与趋势延续、相对强度领先和**强趋势内的回调拐点**；独立恢复路径只处理深度回撤后的高置信放量强反转，保持排名分原值并将试仓封顶 15%。普通弱势修复、过热追高末端和估值叙事继续回避。持仓保护在达到 1R 后转保本与移动止损，高位反转或有效趋势破位按下一开盘退出。

## 环境检查

若 `alpaca`、Python 或 Alpaca 凭证未配置，不要继续评分，把 `references/setup.md` 的清单给用户在本机执行。降级规则：

- 账户/持仓读取失败：敞口 overlay 记为不可用，价量评分照常执行。
- 新闻检索能力不可用：新闻面写"无法确认"，价量评分照常执行。
- Finnhub 无 key 或限流：自动跳过，不影响评分。
- AI 意见无 `ANTHROPIC_API_KEY`、未装 `anthropic` SDK 或调用失败：自动跳过，结论段写"AI 意见: 无法确认"。
- SQLite 缓存异常：先运行 `python3 "$SKILL_DIR/scripts/cache_admin.py" integrity-check`；在线评分可用 `--cache off` 建立无缓存对照。

## 数据流程（单股评分）

```bash
# SKILL_DIR = 本 skill 根目录（即本 SKILL.md 所在目录）；下面是默认安装路径，若装在别处按实际路径设置
SKILL_DIR="$HOME/.codex/skills/worth-buy-stocks"
FEED="iex"
```

首次运行先确认 Python 3.10+ 并安装强制依赖：`python3 -m pip install -r "$SKILL_DIR/requirements.txt"`。所有脚本保持 stdout 为结果/图表通道，运行日志只写 stderr；stderr 被捕获或重定向时默认是 JSONL，可用 `--log-format console|json` 和 `--log-level DEBUG|INFO|WARNING|ERROR` 覆盖。

从 skill 根目录进行人工运维时，先用 `make help` 查看统一入口；常用 target 包括 `analyze`、`scan`、`status`、`sync-assets`、`warm-market-plan`、`reconcile-plan`、回测研究与 `check`。全部回测 Python 命令统一走 `scripts/backtest.py`（`factor-ic`、`score`、`robustness`、`single`、`render`）；Makefile 只做薄转发，保持 stdout、限速和退出码契约。高级子命令参数通过 `ARGS` 传入，全局日志参数通过 `GLOBAL_ARGS` 传入。完整预热和 reconcile target 不得由默认目标或收尾流程自动调用。

1. 默认检索最近 30 天新闻/公告/监管披露：只取公司 IR、SEC、交易所公告和主流财经媒体中能影响交易纪律的 3-5 条（保留标题、日期、链接），不做新闻综述。社交媒体、无来源传闻、模型记忆、分析师目标价不能作为降级依据。整理成 `news_context.json`（形状见 `references/agent-contracts.md`）。

2. 一条命令完成价量指标 + 评分 + 账户 overlay + Finnhub 补充 + 当日 snapshot + AI 意见：

```bash
python3 "$SKILL_DIR/scripts/indicators.py" \
  --symbols {TICKER},SPY,QQQ \
  --feed "$FEED" \
  --adjustment split \
  --llm-context-file news_context.json > result.json
```

`--start/--end` 默认省略，脚本取约两年已完成日线并计算所有指标、相对强度、市场 regime 和 `score`。多标的一次命令带全部 ticker（外加 SPY,QQQ），逐只输出结论。结果落盘为 `result.json`，供读取评分和下一步画图共用。

脚本内部并行取得 `bars`、`account/positions`、`finnhub` 和 `snapshot`（Finnhub 内部再按 symbol 并行），各段独立失败降级、不阻断核心评分。bars/snapshot 默认先经过 skill 根目录 `data/market-data.sqlite3`：完整 warm 覆盖不联网，冷启动、缺口、盘中 snapshot 或显式 refresh 才调用 Alpaca。**不要**直接查询 SQLite、在脚本外重复拉 snapshot，或为提速手动并行；统一使用脚本门面，避免 live/final 混用。

缓存规则是低自由度约束：保持 `--cache auto` 默认；仅在用户要求刷新、复权数据疑似修订时使用 `refresh`，仅在故障对照时使用 `off`。设置 `WORTH_BUY_STOCKS_DB` 或 `--cache-db` 可覆盖数据库路径。`--input` 是硬离线边界：只允许读取 bars 和显式 context 文件，`on` 类在线开关也不得触网或读写 DB。

### 全市场历史预热

用户要求预热/加速全市场历史分析时，先运行 dry-run，确认本次 scanner universe、三年 coverage 与 batch 数；不要自动启动完整长任务：

```bash
python3 "$SKILL_DIR/scripts/cache_admin.py" warm-market --dry-run
```

用户明确要求执行后，再在空闲维护窗口运行；默认写入 skill 根目录 `data/market-data.sqlite3`，可中断并依靠 `sync_ranges` 直接复跑：

```bash
python3 "$SKILL_DIR/scripts/cache_admin.py" warm-market \
  --years 3 --workers 4 --batch-size 50 \
  --max-rpm 120 --max-runtime 3600 \
  --feed iex --adjustment split
```

保持以下边界：预热只拉 final 日线，不拉 snapshot、账户、新闻或 AI；所有 bars pages 共享本进程 120 RPM 均匀预算，Alpaca Basic 200 RPM 的剩余空间留给其他用途，但无法控制同账号的外部客户端；同一 DB 只运行一个实例。小规模验证使用 `--symbols-file` 和不高于 30 RPM，并将 `scope_limited=true` 解释为 canary，不得报告成全市场完成。用 `cache_admin.py status` 查看已跟踪 symbols 的三年覆盖，用 `integrity-check` 验证数据库。

需要以 Alpaca 当前全部可交易资产为基准查漏补缺时，先每日同步一次资产目录，再规划或执行对账：

```bash
python3 "$SKILL_DIR/scripts/cache_admin.py" sync-assets
python3 "$SKILL_DIR/scripts/cache_admin.py" reconcile-market --dry-run
python3 "$SKILL_DIR/scripts/cache_admin.py" reconcile-market \
  --years 3 --workers 4 --batch-size 50 \
  --max-rpm 120 --max-runtime 3600
```

资产目录按 UTC 日期缓存，完整保留 Alpaca 当前 active US equity 及历史消失记录；行情对账只处理 `tradable` 且可由 Basic IEX 支持的主流交易所标的。非交易资产和 OTC 仍写入目录并标明排除原因，不伪装成行情完整。`reconcile-market` 复用相同 coverage、限速、恢复和同库单实例机制；完整长任务仍需用户明确启动。低速验证可追加 `--symbols-file`，其结果只代表 canary 范围。

### 单标的历史回测

用户要求验证评分、回测某只股票或查看历史信号时，使用独立 `backtest.py single`，不要用当前评分的 K 线图冒充历史回测，也不要复制评分公式：

```bash
python3 "$SKILL_DIR/scripts/backtest.py" single \
  --symbol {TICKER} \
  --start 2021-07-01 --end 2026-07-09 \
  --data-source cache-only \
  --feed iex --adjustment split
```

默认 `cache-only` 是严格只读边界，只消费 SQLite 中 TICKER/SPY/QQQ 的 final 日线；coverage 或 260-session 预热不足时原样报告错误，不自动切换 `auto`。只有用户明确同意补数据时，才先用 `cache_admin.py warm` 对这三个 symbol 和明确日期范围预热，然后重新运行 cache-only。离线复现可显式用 `--data-source input --input bars.json`；只有用户明确要求 read-through 时才选择 `auto`。

成功运行会在 `artifacts/backtests/` 生成 canonical JSON 和自包含 HTML。以 JSON 的 validation status、样本量、置信区间和 warning 为准；HTML 只负责展示。解读时同时报告 21/63 日事件研究、非重叠推断 N、rank-only 与 disciplined-verdict 的固定研究窗口及 managed 持仓管理仿真、单边成本、最大回撤以及标的/SPY buy-and-hold。`supports` 才能按冻结条件写“该标的这段历史支持”；`inconclusive` 必须写证据不足；`contradicts` 必须如实写方向相反。任何单标的结果都不得外推为宽市场 alpha。

回测只验证历史价量主链路：不历史回填账户、新闻、Finnhub、Snapshot 或 AI opinion，不调参、不下单。固定 21/63 日场景是标准化研究窗口；managed 场景按日线近似执行建议仓位、初始止损、1R 保本、移动止损与收盘信号的下一开盘退出（不执行静态止盈）。需要重渲染时使用 `backtest.py render --artifact <json> --html <path>`，不得在页面里重新计算指标。

contract 1.4 artifact 还会保留一个明确标为“已拒绝”的 stateful 实验场景，用于审计恢复/重新入场假设。该实验未通过 16 标的 holdout，不得作为正式策略结论或替代 managed 基线。

可选 flag（按需追加）：

| Flag | 默认 | 作用 |
|------|------|------|
| `--snapshot auto\|on\|off` | `auto` | 当日 snapshot 写入补充信息，不参与 score |
| `--account-context auto\|on\|off` | `auto` | 只读账户/持仓生成敞口 overlay |
| `--finnhub-context auto\|on\|off` | `auto` | 有 `FINNHUB_API_KEY` 时读 Finnhub 补充；无 key 不触网 |
| `--llm-context-file path` | 无 | 新闻面风控 JSON，只降级不加分 |
| `--ai-opinion auto\|on\|off` | `auto` | 有 `ANTHROPIC_API_KEY` 时生成 AI 意见对照；只展示不评分；无 key 不触网 |
| `--cache auto\|refresh\|off` | `auto` | final 日线与 snapshot 的 SQLite 读穿策略；盘中 live 不进入评分 |
| `--cache-db path` | `data/market-data.sqlite3` | 覆盖本次运行的数据库路径；环境变量 `WORTH_BUY_STOCKS_DB` 也可覆盖 |
| `--account-context-file` / `--finnhub-context-file` / `--snapshot-context-file` / `--ai-opinion-context-file` | 无 | 离线复盘：喂对应 overlay JSON |
| `--input -` | 无 | 硬离线模式：从 stdin 读 multi-bars JSON，不触网、不读写 DB |

3. K 线图（默认执行）：评分完成后，对每个非基准标的画带计划价位线的终端 K 线，放在最终回复 `结论` 段上方展示：

```bash
python3 "$SKILL_DIR/scripts/chart.py" --symbol {TICKER} --feed "$FEED" --count 30 --plan result.json --html
```

图上的入场/止盈/止损价位线直接取自脚本 trade_plan（入场蓝、回踩入场黄、止损红、止盈绿/亮绿）。原样贴脚本输出，不重画、不手绘、不改价位。`无法评分` 或图表生成失败时省略图表，照常给结论，不写占位说明。

ANSI 颜色经回复转写必然丢失，彩色版由 `--html` 生成（自包含 HTML，含价位线与悬浮 OHLC）。不要加 `--open` 自动打开浏览器；在回复中列出各文件绝对路径，用户想看时自己点开。HTML 生成失败不影响终端图，照常输出。

4. 可选步骤：

- ticker 歧义或需确认资产状态：`alpaca asset get --symbol-or-asset-id {TICKER} --quiet`

## 全市场扫描

用户要"扫全市场找今天能买的"时，用一条命令完成，不要手动拼 asset list → snapshot → indicators 多步：

```bash
python3 "$SKILL_DIR/scripts/scanner.py" --feed "$FEED" --adjustment split --top 20
```

流程：取股池（NYSE+NASDAQ 活跃普通股）→ 流动性初筛（IEX 日成交量 ≥5 万、价 ≥5）→ 精简批量评分 → 提取 `是` 候选 → Finnhub 新闻复核降级 → 对最终候选生成 AI 意见（有 key 时）。输出含最终候选（按评分降序，每只候选带 AI 意见）、被新闻面降级的原"是"、市场 regime 和各阶段计数。全市场扫描耗时可达数分钟（开 AI 意见时每个候选再加数十秒），建议后台运行并告知用户预期等待。

关键 flag：

| Flag | 默认 | 作用 |
|------|------|------|
| `--entry all\|pullback\|trend` | `all` | 按入场时机过滤候选：pullback=只要回调拐点，trend=只要趋势延续 |
| `--verify-news auto\|on\|off` | `auto` | auto=有 `FINNHUB_API_KEY` 才对候选做新闻复核降级 |
| `--ai-opinion auto\|on\|off` | `auto` | 仅对 top-N 最终候选生成 AI 意见对照；只展示不评分、不影响排序 |
| `--exchange` | `NYSE,NASDAQ` | 股池来源交易所 |
| `--min-price` / `--min-volume` | `5` / `50000` | 流动性初筛阈值（IEX 量约为全市场 2-3%） |
| `--top` | `20` | 候选列表上限 |
| `--symbols` / `--symbols-file` | 无 | 覆盖股池（跳过 asset list，用于复盘） |
| `--cache auto\|refresh\|off` / `--cache-db` | `auto` / 默认路径 | scanner chunk 共享同一 SQLite；warm 盘前/休市的 bars 与 snapshot 均零网络 |
| `--input` | 无 | 硬离线复盘：读预取 JSON，不触网、不读写 DB、忽略在线 AI/通知开关 |
| `--notify on` | `off` | 把摘要推 Telegram |

新闻复核这步必须做（默认 auto 已开）：漏掉会高估可买数量。扫描结果汇报仍遵循"输出格式"7 段，对每只候选单独展开；扫描模式候选数多，不默认逐只画 K 线，用户点名某只候选深入时再按单股流程补图。

## 新闻面风控

新闻面进入评分只做 `min(cap)` 封顶：

- `severity=high`（会计造假、停牌/退市、going-concern、要约/并购价格锚定等）：封顶 50，结论最多 `否`。
- `severity=medium` 或 `data_trust=suspect`（增发摊薄、重大诉讼、监管调查、同业重大事故传染、坏数据等）：封顶 74，`是` 降为 `观察`。
- `severity=low` 和利好 catalyst：只回显，不影响评分。

Finnhub 自动提炼只识别显式负面关键词（→ `medium/high` 候选）和临近财报（≤7 天 → `medium`，把可成立的 `是` 降级为 `观察`，理由注明"财报临近，趋势开仓纪律降级"；8–14 天 → `low` 仅提醒），利好忽略。若同时提供 `--llm-context-file`，手工上下文标量字段优先，来源和红旗追加合并。

## 多 agent 编排

运行时支持 sub-agent 且用户要求多 agent/并行分析时才启用；单 agent 环境按上面"数据流程"执行即可。注意：indicators.py 已自动并行采集 account/finnhub/snapshot，拆 sub-agent 不会更快，只为满足用户显式的多 agent 需求或复用离线 artifact。

主 agent 必须保留：用户意图解析、安全边界（硬性纪律全部条款）、最终评分运行（只运行 `scripts/indicators.py` 或等价 `build_result()`）、artifact 校验与合并、最终回复组织。

可委派角色（详细契约见 `references/agent-contracts.md`）：

- Market data agent → `bars` artifact（snapshot 已由脚本自动拉取，无需单独采集）。
- News/event-risk agent → `news_context` artifact；只识别风险，不写买入结论。
- Account overlay agent → `account_context` artifact；只读，失败返回 unavailable。
- Finnhub context agent → `finnhub_context` artifact；无 key/限流返回结构化状态。
- Chart agent → 运行 `chart.py --plan` 生成默认展示的 K 线（评分产出 result.json 后才能画价位线）。
- QA agent → 只检查最终回复是否遵守本 skill，不得改写评分或生成替代结论。

AI 意见没有可委派角色：它由 `indicators.py`/`scanner.py` 内置生成，不得让 sub-agent（或主 agent 自己）代写 AI 意见；离线 `ai_opinion` artifact 仅用于复盘回放。

每个 artifact 喂入前用 `python3 "$SKILL_DIR/scripts/validate_agent_contract.py" --kind <kind> <file>` 预检（kind：`news_context`/`account_context`/`finnhub_context`/`bars`/`snapshot_context`/`ai_opinion`/`result`）。`indicators.py` 加载时会自动复检：可选 overlay 校验失败则丢弃并在 stderr 警告，核心 `bars`（`--input`）校验失败则输出 `无法评分` 退出。

## 评分合约

脚本输出的每个 symbol 都含 `score`，直接采用、不重算。核心字段：

- `score.verdict`：`是`、`观察`、`否`、`持仓需减风险` 或 `无法评分`。
- `score.composite`：最终纪律评分（0-100），已应用风险封顶。
- `score.blocking_reasons`：强制排除/降级原因；空则写"无"。
- `score.trade_plan`：入场/止损/止盈（2R/3R）/移动止损/追价上限。风控参考，不是订单。
- `score.account_overlay`：持仓状态、当前/目标仓位、建议动作、保护性出场价。不可用时不得编造持仓。
- `score.llm_overlay`：新闻面封顶回显（cap、降级原因、catalyst、红旗）。
- `score.entry_timing`：入场时机层回显（分类与时机分）。分类：`pullback_reversal`=强趋势内回调拐点；`recovery_reversal`=深度回撤后的高置信放量强反转，长期趋势资格通过时允许 15% 受限试仓；`trend_continuation`=趋势延续；`pullback_no_trigger`=回调中反转未确认；`overextended`=过热；`trend_broken`=中期趋势破坏。详情（过热度、回调深度、触发信号）在分析对象 `entry_timing` 字段。
- `score.position_exit`：持仓保护信号；高位反转或有效趋势破位给出下一开盘退出，普通持有状态写“继续持有”。
- `score.data_flags`：历史不足、因子缺失、低流动性等提示。

辅助字段（按需引用，完整 schema 见 `scripts/README.md`）：`raw_composite`、`factor_breakdown`（momentum 55 / rel_strength 35 / efficiency 10）、`risk_gates`、`confirmation`、`suggested_position_pct`、`supplemental.finnhub`、`supplemental.ai_opinion`（AI 意见：立场、是否与脚本一致、一句话理由、主要风险；只供结论段对照展示，绝不用于改评分或结论）。

`score` 缺失、关键字段缺失或脚本失败时输出 `无法评分`。字段名只供内部取数，最终答复按硬性纪律第 7 条翻译成自然语言。

## 输出格式

默认中文，先图后结论再证据。最终回复以 K 线图开头，随后必须包含 7 个标题，顺序固定：`结论`、`关键证据`、`风控过滤条件`、`评分拆解`、`账户敞口与交易计划`、`新闻面风控`、`建议`，不得省略或合并。所有价格、仓位、止损、止盈、封顶结论必须来自脚本；缺字段写"无法确认"，不要猜。不要用代码样式包裹普通数值或状态（股票代码、命令和用户要求的原始字段例外）。

例外一：`无法评分` 时不硬套 7 段也不画图，简短说明缺失的数据和补救步骤（如安装 CLI、检查 ticker、补历史数据）。

例外二（轻量模式）：用户明确要求简短（如"简单说""快速看""一句话"）时，只输出 K 线图 + `结论` 段——内容仍全部来自脚本评分——并在末尾提示可展开完整分析；用户追问时再给完整 7 段。评分流程本身不简化。

例外三（历史回测）：用户要求历史回测或评分有效性验证时，不套用当前时点的 7 段交易建议。先给 validation status，再给事件研究样本/IC、仿真与基准、回撤/成本、数据与模型 provenance、限制和 JSON/HTML 路径；所有数字只取 canonical backtest artifact。

**K 线图**（置于 `结论` 上方，不占 7 个标题名额）

`chart.py --plan` 的输出用代码块原样贴出，含入场/止盈/止损价位线；多标的时每只一张（基准 SPY/QQQ 不画）。图表生成失败时直接省略整块，从 `结论` 开始，不写占位说明；用户明确说不要图时也省略。

代码块下方附一行彩色图指引：已生成的 HTML 文件绝对路径（用户自己点开，不自动打开），以及一条可直接复制到终端运行的命令（`$SKILL_DIR` 等变量展开为实际绝对路径），例如：
`彩色图: /path/to/chart_AAPL.html · 终端彩色版: python3 /abs/path/scripts/chart.py --symbol AAPL --plan /abs/path/result.json`

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
- AI 意见: 同意，加一句话理由 / 分歧：AI 倾向买入/观察/回避，加一句话理由 / 无法确认
- 一句话: 最关键依据

**关键证据**

列 4-7 条：日线趋势、周线趋势、30 日结构、相对 SPY/QQQ 强度、技术确认、量价、当日入场质量。

**风控过滤条件**

表格列名固定：`过滤条件`, `状态`, `关键证据`, `处理建议`。固定 8 行，没有触发风险的行也要填写结论状态（"通过"或"未发现"）与事实证据，不能省略：

| 过滤条件 | 状态 | 关键证据 | 处理建议 |
|---|---|---|---|
| 大盘 regime | 通过 / 风险 | 大盘趋势事实（如 SPY 相对 200 日线的位置与方向） | 大盘风险时不新增仓位或降至观察 |
| 个股趋势闸门 | 通过 / 风险 | MA60、MA200、周线排列和关键趋势位 | 跌破关键均线不新开；已持仓按保护价管理 |
| 相对强度 | 通过 / 偏弱 / 无法确认 | 相对 SPY/QQQ 的 3m/6m 强弱 | 跑输基准则等待重评 |
| 30 日结构与追价 | 通过 / 过热 / 破位 | 30 日位置、回踩价、突破价、追价上限 | 高于追价上限不追；等回踩或突破确认 |
| 技术与量价确认 | 通过 / 未确认 | 技术确认、MACD/RSI/KDJ 和成交量是否达标 | 确认不足时维持观察 |
| 流动性/数据质量 | 通过 / 风险 / 无法确认 | 历史数据、snapshot 新鲜度、价差和成交量的核对结果 | 数据异常时补数据后重评 |
| 新闻/事件红旗 | 未发现 / 低 / 中 / 高 / 无法确认 | 红旗等级与可验证来源；无红旗写"近 30 天未发现需要处置的事件风险" | 存在中高红旗时按降级后的结论执行；无法确认时以价量结论为准 |
| 账户敞口 | 无持仓 / 已持仓 / 无法确认 / 风险 | 当前仓位、目标仓位、现金比例、是否超配 | 超目标减仓至目标；无持仓按入场计划执行 |

**评分拆解**

至少写明动量、相对强度、趋势效率三项得分/贡献，以及确认状态和建议仓位。若已封顶，用自然语言说明触发原因和封顶后影响。

**账户敞口与交易计划**

读取到持仓时：当前持仓占账户权益比例、目标仓位、差额、成本价、浮盈亏、保护性出场价、移动止损、2R/3R 止盈、追价上限和处理建议。无持仓时写"当前无持仓"，并给出完整的入场/止损/止盈/追价上限计划价。账户读取失败写"账户持仓无法确认"，处理建议只基于价量与新闻面给出。

**新闻面风控**

写新闻检索状态、Finnhub 补充状态、主要 catalyst、红旗、来源日期/链接；存在降级时写明事件与降级后的结论。无红旗写"近 30 天未发现需要处置的事件风险"；不可用写"新闻面无法确认"。

**建议**

给出一个明确动作并结合账户处理建议：无持仓且计划允许入场可写"按计划价分批新开"；`持仓需减风险` 明确写"减仓至目标敞口 / 跌破保护性出场价退出"；账户 overlay 不可用则动作只基于价量和新闻面。仅在用户要求详细数据时追加 Alpaca 明细或完整 JSON。

结论为 `否` 或 `观察` 时，必须附上重评触发条件，写成正式条件句（"满足以下任一条件时重新评分：……"），不附加"仅作重评参照"之类的限定语；用较快的信号组合而非慢速确认（如"等 3 个月相对强度转正"太滞后，不要用作默认触发）。默认触发组合：1 个月相对 SPY/QQQ 强度转正，且放量收复 60 日均线（或脚本给出的回踩/突破参考价确认）；`观察` 标的若因新闻封顶降级，还需红旗解除或影响明朗。内部约束（不写进回复）：触发条件只决定"什么时候重跑脚本重评"，不是评分输入，升级与否仍完全由脚本评分决定——1 个月强度存在短期反转噪声，禁止据此直接给出买入结论。

输出前自检（不写进回复）：K 线图在 `结论` 上方且为脚本原样输出（生成失败或用户拒绝时可整块省略）；7 个标题齐全且顺序正确（轻量模式只查 K 线图 + `结论` 段完整）；`结论` 段含入场/出场/止盈/持仓/强制排除条件/AI 意见（同意/分歧写明理由，不可用写"无法确认"，且未据此改动任何评分或结论）；风控表 8 行齐全且每行有处理建议；结论为 `否`/`观察` 时建议段含正式条件句的重评触发条件；新闻段明确红旗或不可用状态；全文无字段路径、snake_case、空数组/对象、`true/false/null`、命令参数等源码式文本；全文无机制性否定句与免责声明——逐句检查不得出现"未影响评分""评分未被压低""不构成……降级""未用于降级/升级/调整""仅为分析参考""仅作重评参照""不构成下单指令"及同义变体（硬性纪律第 7 条）。

## Telegram

需要推送时：

```bash
printf '%s' "$DECISION_SUMMARY" | python3 "$SKILL_DIR/scripts/notify_telegram.py"
```

凭证从本地 `.env` 读取 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（查找顺序：`WORTH_BUY_STOCKS_ENV_FILE` 指定路径 → skill 根目录 `.env` → `~/.config/worth-buy-stocks/.env`），环境变量作为 fallback。未配置时转达脚本提示，分析结论不受影响。
