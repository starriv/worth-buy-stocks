# 什么值得买 - 股票版

> **理念：做趋势。** 只参与已被市场验证的方向，不猜底、不博反弹、不和趋势对抗。 抄底会错无数次，追高只会错一次

通过多因子量化分析判断股票是否值得买入，买之前问一问防止冲动金融消费。
脚本会在可用时只读 Alpaca 账户持仓，把当前敞口、成本和浮盈亏纳入建议，并输出参考入场价、保护性出场价和止盈价。Finnhub 可作为可选补充数据源，用于 quote/news/profile/earnings 核验和事件风险识别；其中新闻/财报只会生成降级候选，不会把标的加分或升级。Claude AI 意见是评分完成后的独立对照，只写展示字段。

## 实现原理

```
ALPHA 加权层  →  风险否决层  →  确认 overlay  →  入场时机层
  排名分来源       封顶/否决       技术量价确认       过热降级/拐点触发
 momentum 55      MA200/MA60      只挡买入          不改 composite
 rel_strength 35  周线/大盘
 efficiency 10
```

权重来自逐因子 IC 回测校准：风险调整动量、相对强度和趋势效率进入排名分。MACD/RSI/KDJ/量价等经典指标不进排名分，只做确认和风控解释。第四层识别过热与强趋势内的有序回调反转；量能缺失时不允许拐点升级。

## 使用示例

值得买 - TSM
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/de130335-2c42-4b3e-8216-f1a0f0d0013e" />
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/b5d3d86f-815e-4cbe-b0d0-ce5f8d279ff0" />

不值得买 - MSFT
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/352ca221-d642-49f7-a8ce-86803e074897" />
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/bc3c7252-af06-4882-9fc0-f359d1d3038c" />

## 依赖

仅支持美股，主价量数据来源为 [Alpaca Markets](https://alpaca.markets/)，需先开户并配置 [Alpaca Skill](https://alpaca.markets/blog/alpaca-launches-skills-library-for-ai-agents/)。可选在本地 `.env`（skill 根目录或 `~/.config/worth-buy-stocks/.env`）设置 `FINNHUB_API_KEY` 读取 Finnhub 补充上下文，设置 `ANTHROPIC_API_KEY` 并安装官方 `anthropic` SDK 生成 AI 对照意见；Telegram 推送同样读取该 `.env`。

## 广告

我为 Alpaca 写了一个精美、简洁的 iOS 客户端，可配合使用：[Vicu](https://github.com/starriv/Vicu#)

## 免责声明

投资有风险,入市需谨慎,本工具仅参考用途,需要自己独立分析根据自己的实际情况使用. 祝愿大家一起发财
