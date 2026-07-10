# 什么值得买 - 股票版

> **理念：做趋势。** 只参与已被市场验证的方向，不猜底、不博反弹、不和趋势对抗。 抄底会错无数次，追高只会错一次

通过多因子量化分析判断股票是否值得买入，买之前问一问防止冲动金融消费。支持单股评分和全市场扫描（取 NYSE/NASDAQ 股池 → 流动性初筛 → 批量评分 → 新闻复核降级，找出今天符合纪律的候选）。

脚本会在可用时只读 Alpaca 账户持仓，把当前敞口、成本和浮盈亏纳入建议，并输出参考入场价、保护性出场价和止盈价。Finnhub 可作为可选补充数据源，用于 quote/news/profile/earnings 核验和事件风险识别；其中新闻/财报只会生成降级候选，不会把标的加分或升级。Claude AI 意见是评分完成后的独立对照，只展示"量价纪律结论 vs AI 判断"的异同，不参与评分。

## 实现原理

```
数据采集（并行）            评分四层                    展示 overlay              输出
bars / 只读持仓        →   ALPHA 加权层  →  风险否决层   →   账户敞口建议        →   终端 K 线 + 彩色 HTML 图
Finnhub / snapshot         确认 overlay  →  入场时机层       AI 意见对照             交易计划 + 7 段书面决策
各段独立失败降级           排名 + 否决 + 时机               只展示不改分            价格全部来自脚本
```

评分四层：

```
ALPHA 加权层  →  风险否决层  →  确认 overlay  →  入场时机层
  排名分来源       封顶/否决       技术量价确认       过热降级/拐点触发
 momentum 55      MA200/MA60      只挡买入          不改 composite
 rel_strength 35  周线/大盘
 efficiency 10    新闻面 min(cap)
```

权重来自逐因子 IC 回测校准：风险调整动量、相对强度和趋势效率进入排名分。MACD/RSI/KDJ/量价等经典指标不进排名分，只做确认和风控解释。新闻面以 `min(cap)` 封顶方式进入评分——高危红旗封顶到 `否`、中危把 `是` 降为 `观察`，利好只回显不加分。第四层识别过热与强趋势内的有序回调拐点：过热把达标的 `是` 降级为 `观察` 等回踩，趋势资格完好且回调后放量反转的拐点可替代趋势热度确认；量能缺失时不允许拐点升级。

评分完成后输出终端 K 线（叠加入场/止损/止盈价位线）和同款彩色 HTML 图（自包含 SVG，含悬浮 OHLC，路径在回复中给出、自己点开），最终回复是固定 7 段的书面交易决策：结论、关键证据、风控过滤条件（8 行固定表）、评分拆解、账户敞口与交易计划、新闻面风控、建议。

## 使用示例

问一句"值得买吗 NVDA"，得到的完整输出（示意图，数据为演示）：

![skill 输出示例：彩色 K 线 + 7 段书面决策](docs/demo-output.svg)

也可以直接问"扫全市场找今天能买的"，返回按评分降序的最终候选、被新闻面降级的原"是"和市场 regime。

## 依赖

仅支持美股，主价量数据来源为 [Alpaca Markets](https://alpaca.markets/)，需先开户并配置 [Alpaca Skill](https://alpaca.markets/blog/alpaca-launches-skills-library-for-ai-agents/)。可选在本地 `.env` 设置补充凭证，查找顺序：`WORTH_BUY_STOCKS_ENV_FILE` 指定路径 → skill 根目录 `.env` → `~/.config/worth-buy-stocks/.env`。`FINNHUB_API_KEY` 用于读取 Finnhub 补充上下文；`ANTHROPIC_API_KEY`（并安装官方 `anthropic` SDK）用于生成 AI 对照意见；Telegram 推送读取 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`。均未配置时自动跳过，不影响价量评分。

## 广告

我为 Alpaca 写了一个精美、简洁的 iOS 客户端，可配合使用：[Vicu](https://github.com/starriv/Vicu#)

## 免责声明

投资有风险,入市需谨慎,本工具仅参考用途,需要自己独立分析根据自己的实际情况使用. 祝愿大家一起发财
