# 什么值得买 - 股票版

> **理念：做趋势。** 只参与已被市场验证的方向，不猜底、不博反弹、不和趋势对抗。 抄底会错无数次，追高只会错一次

通过多因子量化分析判断股票是否值得买入，买之前问一问防止冲动金融消费。

## 实现原理

```
ALPHA 加权层 (排名分)  →  风险否决层 (封顶)  →  确认 overlay (不扣分)
     momentum 60            MA200 下行 → 55      技术转弱时 "是" → "观察"
     rel_strength 40        MA60 跌破 → 65
                            周线空头 → 50
```

权重来自逐因子 IC 回测校准：仅**风险调整动量 (IC≈0.10)** 和**相对强度 (IC≈0.06)** 在 3 个月前瞻稳定为正。MACD/RSI/KDJ/量价等经典指标 IC≈0，不进排名分，只做确认。

## 使用示例

值得买 - TSM
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/de130335-2c42-4b3e-8216-f1a0f0d0013e" />
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/b5d3d86f-815e-4cbe-b0d0-ce5f8d279ff0" />

不值得买 - MSFT
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/352ca221-d642-49f7-a8ce-86803e074897" />
<img width="1920" height="1044" alt="image" src="https://github.com/user-attachments/assets/bc3c7252-af06-4882-9fc0-f359d1d3038c" />

## 依赖

仅支持美股，数据来源为 [Alpaca Markets](https://alpaca.markets/)，需先开户并配置 [Alpaca Skill](https://alpaca.markets/blog/alpaca-launches-skills-library-for-ai-agents/)。

## 广告

我为 Alpaca 写了一个精美、简洁的 iOS 客户端，可配合使用：[Vicu](https://github.com/starriv/Vicu#)

## 免责声明

投资有风险,入市需谨慎,本工具仅参考用途,需要自己独立分析根据自己的实际情况使用. 祝愿大家一起发财
