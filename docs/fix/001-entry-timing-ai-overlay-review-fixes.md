# Fix: 入场时机与 AI overlay 深度 review 缺陷

> **日期**: 2026-07-10

## 问题描述

- 回调拐点没有约束高点、低点和反转的时间顺序，量能缺失仍可能放行“是”。
- AI 强制模式只看 batch 状态，单票失败会被吞掉；离线 agreement 可以与当前评分不一致。
- forward log 没有第四层字段，v1.0/v1.1 语义无法隔离，股票与 SPY 起始日可能错位。
- scanner 测试夹具被新过热规则击穿，入场类型过滤发生在 Finnhub 调用之后。
- `docs/` 被整体忽略，model card 与设计文档无法版本化。

## 根因分析

- 回撤深度直接用无时间顺序的窗口极值计算，历史低点又与当前 MA20 比较。
- 确认层把缺失值视为通过；AI `on` 只校验顶层 artifact。
- 样本外日志只保存最终 verdict/composite，没有信号日、基础结论或时机分类。
- 文档、测试和任务验收没有随第四层语义同步。

## 修复方案

- 回调改为严格“结构高点早于近期低点”，近五日触碰使用同时点 MA20；五种时机分类均有直接测试。
- technical、volume、trend quality 必须可计算且达标；拐点仅替代 technical/trend quality，不能替代量能。
- AI `on` 对任一目标条目失败报错；离线 agreement 在附加时按当前 verdict 重算。
- 评分输出第四层介入前结论；model card 升为 v1.1。新 forward log 从信号日后下一交易日统一使用股票和 SPY 收盘，并按时机分类统计。
- scanner 在新闻复核前应用入场类型过滤；重建不过热的健康趋势夹具。
- 恢复 `docs/` 版本跟踪并同步 README、契约与设计文档。

## 修改文件

| 文件 | 变更 |
|---|---|
| `scripts/metrics.py` | 有序回调、同时点 MA20、完整时机元数据 |
| `scripts/scoring/engine.py`、`scripts/scoring/gates.py` | 缺失确认不放行、保留第四层介入前结论 |
| `scripts/ai_opinion.py`、`scripts/indicators.py`、`scripts/scanner.py` | 单票失败检测、agreement 重算、过滤前移 |
| `scripts/forward_log.py`、`docs/model-card.md` | v1.1 日志与统一起始日对账 |
| `tests/` | 入场五分类、升级/降级、AI 强制失败、日志日期与 scanner 顺序回归 |
| `.gitignore`、`README.md`、`scripts/README.md`、`references/agent-contracts.md` | 权威文档恢复跟踪并与行为同步 |

## 验证方式

- `python3 -m unittest discover -s tests -q`:295 tests passed。
- `pytest -q`:295 passed。
- `python3 -m compileall -q scripts tests`、`git diff --check`:通过。
- 评分反例:量能缺失维持观察；过热前后 composite 相同且“是”降为“观察”。
- AI 契约:顶层成功但单票 unavailable 时 `on` 报错；离线假 agreement 被重算。

*创建于: 2026-07-10*
