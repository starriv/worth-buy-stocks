# scripts/ 工程说明

`worth-buy-stocks` skill 的脚本层。纯 Python 标准库 + 本机 `alpaca` CLI，无第三方依赖。
确定性计算（同输入同输出）、不下单、不打印凭证。面向接手者，非用户文档（用户文档见 `../SKILL.md`）。

## 分层与依赖

```
运行时（skill 评分链路）
  metrics.py ───────────────┐  纯指标原语，无内部依赖
  scoring.py ──────────────┐│  确定性因子打分引擎，纯读分析字典、无内部依赖
                           ││
  analysis.py ◄── metrics, scoring   单股分析 + 跨股聚合(相对强度) + 附 score 块
                           ▲
  indicators.py ◄── fetching, metrics, analysis, scoring   CLI 入口 + 向后兼容再导出
  fetching.py               Alpaca 日线拉取（分页/去重/feed 回退），无内部依赖

旁路工具（不在评分链路上）
  chart.py ◄── fetching(惰性)         终端 K 线
  notify_telegram.py                  Telegram 推送（读环境变量）
  stop_notify_hook.py ◄── notify_telegram(子进程)   Claude Code Stop hook

研究/回测（离线，不属于 skill 运行时）
  backtest_common.py ◄── fetching, analysis, metrics   篮子常量 + 取数面板 + 切片打分 + 统计原语
  backtest_score.py        ◄── backtest_common   分数分桶 / 截面 IC
  backtest_factor_ic.py    ◄── backtest_common   逐因子单因子 IC
  backtest_robustness.py   ◄── backtest_common   非重叠窗口 + regime 拆分 + 幸存者探针
```

依赖方向单向向上，无环。`indicators.py` 仅做 CLI + 再导出（旧的 `import indicators as I` 仍可用）。

## 各文件职责

| 文件 | 职责 | 入口 |
|------|------|------|
| `metrics.py` | EMA/MACD/RSI/KDJ/均线/收益率/周线聚合；量化因子：年化波动、最大回撤、ATR、效率比、对数价格回归、12-1 动量 | 库 |
| `fetching.py` | `alpaca data multi-bars` 拉日线，分页、去重、sip→iex 回退 | 库 |
| `analysis.py` | `analyze_symbol`（单票全字段）+ `build_result`（跨票聚合相对强度、附 `score`） | 库 |
| `scoring.py` | `score(analysis)` 确定性多因子打分：alpha 加权 + 风险否决 + 确认 overlay + 反波动率仓位 | 库 |
| `indicators.py` | CLI：拉数→分析→打分→输出 JSON | `python3 indicators.py` |
| `chart.py` | 终端蜡烛图（默认自取数据，或 `--input -` 喂 JSON） | `python3 chart.py` |
| `notify_telegram.py` | 把文本推到 Telegram，读环境变量，未配置则提示 | `python3 notify_telegram.py` |
| `stop_notify_hook.py` | Stop hook：分析结束自动推送（内容守卫 + 去重 + 非阻塞） | 注册于 settings.json |
| `backtest_common.py` | 回测公共件：`UNIVERSE`/`Panel`/`spearman`/`quintile_spread` 等 | 库 |
| `backtest_*.py` | 三类因子有效性回测 | `python3 backtest_xxx.py` |

## 怎么跑

```bash
# 单股评分（默认两年日线，自动预热 MA200/周线 MACD）
python3 scripts/indicators.py --symbols AAPL,SPY,QQQ --feed iex --adjustment split

# 离线/测试：喂 multi-bars JSON，不触网
cat bars.json | python3 scripts/indicators.py --input -

# 终端 K 线（自取，最常用）
python3 scripts/chart.py --symbol AAPL --count 30

# 推送（需先 export TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）
printf '%s' "结论文本" | python3 scripts/notify_telegram.py

# 回测（触网，较慢；输出纯统计，看方向不看绝对收益）
python3 scripts/backtest_score.py        # 分桶 + 截面 IC
python3 scripts/backtest_factor_ic.py    # 逐因子 IC
python3 scripts/backtest_robustness.py   # 非重叠 + regime + 幸存者探针

# 单元测试（纯计算 + 离线路径，不触网）
python3 -m unittest discover -s tests
```

## 打分引擎要点（scoring.py）

权重**经回测校准**，不是拍脑袋：逐因子 IC 显示只有**风险调整动量(≈0.10)** 与 **相对强度(≈0.06)** 在
3 个月前瞻稳定为正，故排名分 `composite` 只用这两个（`ALPHA_WEIGHTS = momentum 60 / rel_strength 40`）。
其余各归其位：

- **trend（MA200/MA60/周线）** → 风险否决层 `_gates`，封顶分数而非进排名。
- **technical / volume**（IC≈0）→ 确认 overlay，技术转弱时把「是」封顶为「观察」，绝不加分。
- 缺失因子按可用权重重归一；反波动率仓位 = 目标波动 / 实现波动 × 信号强度。

## 已知局限（诚实标注）

回测基于**含幸存者偏差**的大盘篮子、**2023–26 单边牛市**样本；非重叠窗口下 3 个月独立样本仅 ~8 期，
**t≈1.2 不显著**，且**从未在熊市验证**。结论：方向上像有微弱 edge，统计上未证实。详见各回测脚本 docstring。
本评分是**纪律/方向工具**，不是被证明的盈利策略。

## 约定

- 改打分权重：只动 `scoring.py` 的 `ALPHA_WEIGHTS` / 各 `_f_*` 断点，`score()` 与回测自动跟随。
- 改回测篮子/窗口：只动 `backtest_common.py` 的 `UNIVERSE` / `WARMUP` / `HORIZONS`。
- 新增纯函数请配单测（`tests/test_*.py`）；触网的回测脚本不进单测，靠手动跑验证。
- 历史不足时函数返回 `None`（不抛错、不退化为 0），下游据此记「无法确认」。
