# Refactor: 全 skill 统一结构化日志

> **日期**: 2026-07-10

## 动机

脚本曾混用 `sys.stderr.write`、root `logging.*`、import-time `basicConfig` 和 warm-market 自写 JSONL。不同入口的格式、上下文与脱敏能力不一致，也容易污染 stdout 的 JSON artifact、终端 K 线和回测报告。

本次引入 `structlog==26.1.0`，把所有运行诊断统一到 stderr，同时保持既有产品输出与退出码协议。

## 变更映射

| Before | After |
|---|---|
| 各 CLI 自行写 stderr 或配置 root logger | `scripts/skill_logging.py` 统一配置 `worth_buy_stocks.*` 命名空间 |
| warm-market 自行序列化 progress JSON | progress/heartbeat 作为普通结构化事件进入同一 processor chain |
| 只有自由文本，难以稳定解析 | UTC timestamp、level、snake_case event、logger 加按需上下文字段 |
| 无统一敏感信息处理 | 字段、URL、header、环境赋值和 traceback 在 renderer 前递归脱敏 |
| 日志格式固定或分叉 | `auto` 在 TTY 输出 console、非 TTY 输出 JSONL，可由 CLI/env 覆盖 |
| 新代码可能重新引入 raw stderr | AST guard 阻止绕过 facade、root logger 和独立 JSON log writer |

stdout 契约没有改变：JSON CLI 仍输出单一 JSON 文档，chart/backtest/forward-log 仍输出原终端或研究报告。所有日志只写 stderr。

## 修改文件

| 文件 | 变更 |
|---|---|
| `requirements.txt` | 精确锁定 `structlog==26.1.0` |
| `scripts/skill_logging.py` | 统一参数、handler、processor、console/JSONL、contextvars、stdlib bridge 与脱敏 |
| `scripts/indicators.py`、`scripts/scanner.py`、`scripts/chart.py` | 迁移告警、AI 提示、artifact 与顶层异常 |
| `scripts/cache_admin.py` | 迁移 warm/reconcile/sync progress、heartbeat 与失败事件 |
| `scripts/backtest.py`、`scripts/backtesting/*.py`、`scripts/forward_log.py` | 使用统一日志门面，保留 stdout 研究报告 |
| `scripts/notify_telegram.py`、`scripts/validate_agent_contract.py`、`scripts/ai_opinion.py` | 接入统一 CLI 配置与结构化错误事件 |
| `tests/test_skill_logging.py` | facade、并发、幂等、stdlib bridge、JSON/console 与脱敏测试 |
| `tests/test_logging_guard.py` | 全仓日志静态守卫 |
| `tests/test_logging_entrypoints.py` | 离线 stdout/stderr 端到端协议测试 |
| `README.md`、`references/setup.md`、`scripts/README.md`、`SKILL.md` | 安装、参数、通道、schema 与维护约束 |

## 验证

- Python 3.10.0 成功导入 `structlog 26.1.0`；系统 Python 3.9 明确不在支持范围。
- 11 个公共 CLI 的 `--help` 均包含 `--log-level` 和 `--log-format`。
- raw stderr、root `logging.*`、`logging.basicConfig` 和独立 progress serializer 静态扫描为零。
- facade 10 项、静态 guard 3 项、离线入口 5 项专项测试通过。
- cache-admin status/integrity 实测 `ok`；3-symbol warm-market dry-run 为 covered 2、pending 1、pages 0，stdout/stderr 分离测试通过。
- `python3 -m unittest discover -s tests -v`：456 passed。
- `python3 -m pytest -q`：460 passed。
- `python3 -m compileall -q scripts tests/test_skill_logging.py` 与 `git diff --check` 通过。

*创建于: 2026-07-10*
