# Make 命令统一入口

> **日期**: 2026-07-10

## 背景

分析、扫描、缓存治理、资产同步、全市场预热、回测和质量检查已有稳定 Python CLI，但日常命令分散在多份文档，长参数重复且 purge 容易误用。需要一个可发现的工程入口，同时避免在 Makefile 中复制业务逻辑。

## 实现方案

- 根目录新增 Makefile，默认目标为只读 `help`，按基础、运行、缓存、预热、危险操作、研究和质量分类展示 target。
- target 使用静默 recipe，只转发现有 Python CLI，保持 stdout JSON/图表协议、stderr 日志、退出码、coverage、限速和锁语义不变。
- 公共配置统一为 `PYTHON`、`CACHE_DB`、`FEED`、`SYMBOLS`、`YEARS`、`WORKERS`、`BATCH_SIZE`、`MAX_RPM`、`MAX_RUNTIME` 等变量。
- `GLOBAL_ARGS` 放在带 subcommand CLI 的 subcommand 之前；`ARGS` 追加在具体 subcommand 之后，兼容 argparse 参数位置。
- plan 与长任务分开：`warm-market-plan`/`reconcile-plan` 只 dry-run，实际 `warm-market`/`reconcile-market` 必须显式调用。
- `cache-purge` 使用空默认 `PURGE_SYMBOLS`，日期必须成对；`cache-purge-all` 额外要求 `CONFIRM=purge-all`，均在 Python 启动前拦截错误用法。

## API

```bash
make help
make analyze SYMBOLS=AAPL,SPY,QQQ
make scan TOP=20

make status
make sync-assets
make warm-market-plan
make reconcile-plan

make test
make check
```

高级示例：

```bash
make analyze SYMBOLS=AAPL,SPY,QQQ ARGS="--cache refresh"
make status GLOBAL_ARGS="--log-format console --log-level DEBUG"
make cache-purge PURGE_SYMBOLS=AAPL START=2026-07-01 END=2026-07-10
```

## 数据库变更

无。Makefile 默认继续使用 skill 根目录 `data/market-data.sqlite3`，也可通过 `CACHE_DB` 覆盖。

## 修改文件

| 文件 | 变更 |
|---|---|
| `Makefile` | 统一 target、公共变量、参数透传、长任务分流和 purge guard |
| `tests/test_makefile.py` | help、dry-run、纯 JSON status、缺参数与确认 guard |
| `README.md`、`references/setup.md` | 用户安装、分析、缓存和预热入口改为 Make 优先 |
| `SKILL.md`、`scripts/README.md` | agent 运维边界、变量契约和维护规则 |

## 验证

- `make` 与 `make -C <skill> help` 退出 0，不触网、不创建数据库，也不输出 entering/leaving directory 噪声。
- `make doctor` 使用临时缺失 DB 通过，Python 3.12、structlog、Alpaca CLI 和只读 status 均正常。
- reconcile dry-run、日志全局参数、forward-log 默认篮子和 purge guard 的代表性命令通过。
- 466 unittest passed；466 pytest + 13 subtests passed。
- `make check` 完成 compileall、pytest、skill quick validation 和 `git diff --check`。

## 注意事项

- Makefile 是薄转发层；新增评分、行情、数据库或限速逻辑必须继续放在 Python 模块。
- `ARGS`/`GLOBAL_ARGS` 按 shell 参数原样展开，只应传可信的本地命令参数。
- `make warm-market` 和 `make reconcile-market` 是显式联网长任务，不属于默认目标、安装、测试或自动收尾。

*创建于: 2026-07-10*
