# AI 意见 Overlay 设计方案

状态:已实施,2026-07-10 review 修正完成
日期:2026-07-08(2026-07-10 更新)
任务系统:`tasks/projects/ai-opinion-overlay.md`

## 1. 背景与目标

worth-buy-stocks 当前是"脚本 + 量价算法"的确定性评分体系:`indicators.py` 是唯一评分权威,外部信息(新闻、账户、Finnhub、snapshot)全部以 overlay JSON + 契约校验 + 独立降级的方式接入。

目标:引入一个 AI 模型对同一份证据做独立分析,在最终回复的结论段新增一行"AI 意见",让用户对比"量价纪律结论 vs AI 判断",**同时不破坏现有模式**——不动评分逻辑、不动硬性纪律实质、不给分析链路增加硬依赖。

## 2. 设计原则

1. **零评分权力**:AI 意见不改评分、不改结论、不触发降级,只作对照展示。权力级别低于新闻 overlay(可封顶)和账户 overlay(可调仓位建议)。硬性纪律第 2 条"脚本是唯一评分来源"不变。
2. **防锚定**:AI 模型只拿到证据层,不拿到脚本的 verdict/composite,独立形成判断;"同意/分歧"由脚本比较得出,不由模型自述。
3. **优雅降级**:无 API key 不触网,`auto` 模式 API 失败/超时/限流不阻断评分,输出行写"AI 意见:无法确认"。显式 `on` 模式要求每个目标标的成功,任一条目不可用时命令明确失败。
4. **契约化**:ai_opinion 是第五个 overlay,有 JSON 契约、`validate_agent_contract.py` 校验、离线复盘 flag、pytest 覆盖。

## 3. 架构

```
indicators.py 流程(现有):
  bars/account/finnhub/snapshot 并行采集 → 指标计算 → 评分 → 组装输出

新增(评分完成后、组装输出前):
  评分结果 ──提取证据(隐去 verdict/composite)──▶ ai_opinion.py
                                                    │ Claude API (structured outputs)
                                                    ▼
  supplemental.ai_opinion ◀──脚本比较 stance vs verdict 得出 agreement──┘
```

注意:AI 意见依赖指标计算结果,不能与 bars 采集并行;它是评分后的一个后置步骤。

## 4. `scripts/ai_opinion.py` 规格

### 4.1 输入(证据载荷)

从单 symbol 的评分结果中提取,**必须隐去** `score.verdict`、`score.composite`、`raw_composite`、`suggested_position_pct`、`trade_plan`(含结论倾向的字段一律不进):

- 日线/周线趋势状态、MA 排列、关键趋势位
- 30 日结构(位置、回踩价、突破价)
- 相对 SPY/QQQ 强度(3m/6m)
- MACD/RSI/KDJ、量价确认状态
- market regime
- `news_context` 的红旗与 catalyst(如有)
- data_flags(历史不足、低流动性等)

### 4.2 API 调用

- SDK:官方 `anthropic` Python SDK(需加入依赖说明,不用 requests 裸调)
- 模型:`claude-opus-4-8`
- 结构化输出:`output_config: {format: {type: "json_schema", schema: ...}}`,保证返回合法 JSON,无需解析容错
- thinking:`{"type": "adaptive"}`;effort:`medium`
- 超时:显式设置(建议 30-60s),失败即降级,不重试超过 SDK 默认
- key 查找:`ANTHROPIC_API_KEY`,复用现有 `.env` 查找链(`WORTH_BUY_STOCKS_ENV_FILE` → skill 根目录 `.env` → `~/.config/worth-buy-stocks/.env` → 环境变量),实现在 `local_env.py` 基础上扩展
- 提示词:系统提示声明角色(独立技术分析视角)、纪律框架(趋势跟随/相对强度取向)、只基于给定证据不引入外部知识、不输出投资建议措辞

### 4.3 模型返回 schema

```json
{
  "stance": "buy | watch | avoid",
  "confidence": "low | medium | high",
  "view": "一句话核心理由(中文,≤60字)",
  "key_risks": ["...", "..."]
}
```

### 4.4 脚本输出(ai_opinion 契约)

顶层为 `symbols` 映射包装(与 finnhub_context/snapshot_context 同构),以支持多 symbol 离线注入;每个 symbol 的条目即写入该 symbol `supplemental.ai_opinion` 的内容:

```json
{
  "contract_version": "worth-buy-stocks.agent.v1",
  "kind": "ai_opinion",
  "status": "ok | unavailable",
  "reason": "非 ok 时必填",
  "symbols": {
    "AAPL": {
      "status": "ok | unavailable",
      "reason": "非 ok 时必填",
      "stance": "buy | watch | avoid",
      "confidence": "low | medium | high",
      "view": "一句话核心理由(中文,≤60字)",
      "key_risks": ["..."],
      "agreement": "agree | diverge | not_applicable",
      "model": "claude-opus-4-8",
      "generated_at": "ISO 8601"
    }
  }
}
```

`status=ok` 的 symbol 条目必填 `stance/confidence/view/agreement`;`status=unavailable` 的条目只需 `reason`。顶层 `status=unavailable` 表示整体不可用(如无 key)。

> 2026-07-08 调整:原设计为单 symbol 扁平结构,实施 task-001 时改为 symbols 包装,原因:多 symbol 离线复盘(`--ai-opinion-context-file`)需要按 symbol 键控,且与现有 overlay 契约保持同构。

### 4.5 agreement 映射(脚本计算,不由模型自述)

| 脚本 verdict | AI stance | agreement |
|---|---|---|
| 是 | buy | agree |
| 观察 | watch | agree |
| 否 / 持仓需减风险 | avoid | agree |
| 其余组合 | — | diverge |
| 无法评分 | 任意 | not_applicable(此时不调用 API,直接 unavailable) |

## 5. `indicators.py` 接入

- 新 flag:`--ai-opinion auto|on|off`,默认 `auto`(有 key 才调用,无 key 不触网,行为对齐 `--finnhub-context`)
- 离线复盘:`--ai-opinion-context-file path`,加载时经契约校验,失败则丢弃并 stderr 警告(对齐现有 overlay)
- 结果落点:`supplemental.ai_opinion`,**不进 `score` 对象**
- 多 symbol:每个评分 symbol 各一次调用(SPY/QQQ 基准不调用);注意成本与延迟提示

## 6. `scanner.py` 接入

- 只对**最终 top-N 候选**跑 AI 意见(位置在新闻复核降级之后),不对全股池跑
- flag:`--ai-opinion auto|on|off`,默认 `auto`
- 输出中每个候选带 AI 意见行;降级不影响扫描主流程

## 7. 契约与校验

- `validate_agent_contract.py` 新增 kind `ai_opinion`
- `references/agent-contracts.md` 增补 `ai_opinion` 契约形状与示例
- `indicators.py` 加载 `--ai-opinion-context-file` 时自动复检

## 8. 输出格式变更(SKILL.md)

1. **数据角色**段落加一句:"AI 意见默认作为对照展示 overlay,不参与评分、不触发降级、不可用于升级结论。"
2. **结论段**固定字段,在"一句话"之前新增:
   - `AI 意见: 同意,<view> / 分歧:AI 倾向<stance 中文>,<view> / 无法确认`
3. **输出前自检清单**同步该行。
4. `references/setup.md` 增加 `ANTHROPIC_API_KEY` 可选配置说明(未配置时功能自动关闭)。
5. 轻量模式:结论段本就完整保留,AI 意见行随之保留,无需特殊处理。

## 9. 测试计划

- `tests/test_ai_opinion.py`:mock API —— 正常返回、超时/异常降级、无 key 跳过、agreement 映射全组合、证据载荷不含 verdict/composite(防锚定回归测试)
- `tests/test_agent_contracts.py`:新增 `ai_opinion` kind 的合法/非法样例
- `tests/test_indicators.py`:`--ai-opinion off` 默认路径不变;`--ai-opinion-context-file` 离线注入;契约校验失败丢弃
- `tests/test_scanner.py`:top-N 候选才调用;降级不阻断
- 端到端:真实 key 单股跑通;全量 pytest 通过

2026-07-10 review 增补回归:

- 顶层 batch 成功但单票 unavailable 时,`on` 模式必须失败。
- 离线 artifact 附加时忽略其 `agreement`,按当前脚本 verdict 重算。
- `docs/` 属于版本化权威文档,不得被 `.gitignore` 整体排除。

## 10. 明确不做(第一版)

- 不给 AI 意见任何评分权(封顶/降级/升级都不给)
- 不做多模型投票、不做 AI 意见缓存
- 不在 scanner 初筛阶段调用 AI

升级路径:若后续用 `backtest_*` 体系回测证明 AI 意见有信息量,再考虑给予类似新闻面的 `min(cap)` 封顶权——届时才需要动纪律条款。
