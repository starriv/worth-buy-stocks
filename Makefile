SHELL := /bin/sh
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

PYTHON ?= python3
CODEX_HOME ?= $(HOME)/.codex
SKILL_DIR := $(CURDIR)
CACHE_DB ?= $(SKILL_DIR)/data/market-data.sqlite3
SKILL_VALIDATOR ?= $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py

FEED ?= iex
ADJUSTMENT ?= split
SYMBOLS ?= AAPL,SPY,QQQ
SYMBOL ?= AAPL
FORWARD_SYMBOLS ?=
TOP ?= 20
COUNT ?= 30
PLAN ?=
HTML ?= auto

YEARS ?= 3
WORKERS ?= 4
BATCH_SIZE ?= 50
MAX_RPM ?= 120
MAX_RUNTIME ?= 3600
TIMEOUT ?= 30
FORCE ?= 0

START ?=
END ?=
PURGE_SYMBOLS ?=
CONFIRM ?=
KIND ?= result
ARTIFACT ?= result.json
NOTIFY_FILE ?=
HORIZON_DAYS ?= 63
DATA_SOURCE ?= cache-only
COST_BPS ?= 10

# GLOBAL_ARGS goes before a subcommand; ARGS goes after it.
GLOBAL_ARGS ?=
ARGS ?=

CACHE_ADMIN = $(PYTHON) scripts/cache_admin.py --cache-db "$(CACHE_DB)" $(GLOBAL_ARGS)
MARKET_JOB_ARGS = --years "$(YEARS)" --workers "$(WORKERS)" \
	--batch-size "$(BATCH_SIZE)" --max-rpm "$(MAX_RPM)" \
	--max-runtime "$(MAX_RUNTIME)" --feed "$(FEED)" \
	--adjustment "$(ADJUSTMENT)" --timeout "$(TIMEOUT)"
START_ARG = $(if $(strip $(START)),--start "$(START)",)
END_ARG = $(if $(strip $(END)),--end "$(END)",)
PLAN_ARG = $(if $(strip $(PLAN)),--plan "$(PLAN)",)
FORCE_ARG = $(if $(filter 1 true yes on,$(FORCE)),--force,)
FORCE_ASSETS_ARG = $(if $(filter 1 true yes on,$(FORCE)),--force-assets,)
FORWARD_SYMBOLS_ARG = $(if $(strip $(FORWARD_SYMBOLS)),--symbols "$(FORWARD_SYMBOLS)",)

.PHONY: help install doctor \
	analyze scan chart notify \
	status integrity cache-warm cache-refresh sync-assets \
	warm-market-plan warm-market reconcile-plan reconcile-market \
	cache-purge cache-purge-all \
	backtest-factor backtest-score backtest-robustness backtest-visual backtest-lifecycle-validate \
	forward-record forward-evaluate contract \
	compile test-unit test validate-skill diff-check check

help: ## [基础] 显示统一命令入口和常用变量
	@awk 'BEGIN { FS = ":.*## "; printf "worth-buy-stocks commands\n\n" } /^[a-zA-Z0-9_.-]+:.*## / { printf "  %-22s %s\n", $$1, $$2 } END { printf "\nVariables: PYTHON CACHE_DB FEED SYMBOLS SYMBOL START END ARGS GLOBAL_ARGS\n          DATA_SOURCE COST_BPS HTML YEARS WORKERS BATCH_SIZE MAX_RPM MAX_RUNTIME TIMEOUT FORCE\n" }' $(MAKEFILE_LIST)

install: ## [基础] 安装 Python 强制依赖
	@$(PYTHON) -m pip install -r requirements.txt

doctor: ## [基础] 检查 Python、structlog、Alpaca CLI 和缓存状态
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"; print(sys.version.split()[0])'
	@$(PYTHON) -c 'import structlog; print("structlog", structlog.__version__)'
	@command -v alpaca >/dev/null
	@alpaca version
	@$(CACHE_ADMIN) status

analyze: ## [运行] 分析 SYMBOLS（默认 AAPL,SPY,QQQ），JSON 写 stdout
	@$(PYTHON) scripts/indicators.py $(GLOBAL_ARGS) --symbols "$(SYMBOLS)" \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" $(ARGS)

scan: ## [运行] 全市场扫描，使用 TOP 和 ARGS 调整范围
	@$(PYTHON) scripts/scanner.py $(GLOBAL_ARGS) --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --top "$(TOP)" \
		--cache-db "$(CACHE_DB)" $(ARGS)

chart: ## [运行] 为 SYMBOL 生成终端和 HTML K 线，可传 PLAN=result.json
	@$(PYTHON) scripts/chart.py $(GLOBAL_ARGS) --symbol "$(SYMBOL)" --count "$(COUNT)" \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" $(PLAN_ARG) --html "$(HTML)" $(ARGS)

notify: ## [运行] 推送 NOTIFY_FILE 中的摘要到 Telegram
	@[ -n "$(NOTIFY_FILE)" ] || { printf '%s\n' 'NOTIFY_FILE is required' >&2; exit 2; }
	@$(PYTHON) scripts/notify_telegram.py $(GLOBAL_ARGS) --file "$(NOTIFY_FILE)" $(ARGS)

status: ## [缓存] 只读查看 SQLite、资产目录和 coverage 状态
	@$(CACHE_ADMIN) status

integrity: ## [缓存] 只读运行 SQLite integrity_check
	@$(CACHE_ADMIN) integrity-check

cache-warm: ## [缓存] 补齐 SYMBOLS 的日线和 snapshot
	@$(CACHE_ADMIN) warm --symbols "$(SYMBOLS)" $(START_ARG) $(END_ARG) \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" --timeout "$(TIMEOUT)" $(ARGS)

cache-refresh: ## [缓存] 强制刷新 SYMBOLS，可传 START/END
	@$(CACHE_ADMIN) refresh --symbols "$(SYMBOLS)" $(START_ARG) $(END_ARG) \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" --timeout "$(TIMEOUT)" $(ARGS)

sync-assets: ## [缓存] 每日同步 Alpaca active US-equity 资产目录；FORCE=1 强刷
	@$(CACHE_ADMIN) sync-assets $(FORCE_ARG) --timeout "$(TIMEOUT)" $(ARGS)

warm-market-plan: ## [预热] dry-run scanner 股池三年预热，不请求 bars
	@$(CACHE_ADMIN) warm-market $(MARKET_JOB_ARGS) --dry-run $(ARGS)

warm-market: ## [预热] 显式执行 scanner 股池缺口预热，可中断续跑
	@$(CACHE_ADMIN) warm-market $(MARKET_JOB_ARGS) $(ARGS)

reconcile-plan: ## [预热] dry-run 全资产目录 coverage 查漏，不请求 bars
	@$(CACHE_ADMIN) reconcile-market $(MARKET_JOB_ARGS) \
		$(FORCE_ASSETS_ARG) --dry-run $(ARGS)

reconcile-market: ## [预热] 显式补齐全部 IEX-eligible 资产 coverage
	@$(CACHE_ADMIN) reconcile-market $(MARKET_JOB_ARGS) \
		$(FORCE_ASSETS_ARG) $(ARGS)

cache-purge: ## [危险] 清理 PURGE_SYMBOLS，可选 START/END（必须同时提供）
	@[ -n "$(PURGE_SYMBOLS)" ] || { printf '%s\n' 'PURGE_SYMBOLS is required' >&2; exit 2; }
	@[ -z "$(START)$(END)" ] || { [ -n "$(START)" ] && [ -n "$(END)" ]; } || { printf '%s\n' 'START and END must be provided together' >&2; exit 2; }
	@$(CACHE_ADMIN) purge --symbols "$(PURGE_SYMBOLS)" $(START_ARG) $(END_ARG) \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" $(ARGS)

cache-purge-all: ## [危险] 清空缓存；必须传 CONFIRM=purge-all
	@[ "$(CONFIRM)" = "purge-all" ] || { printf '%s\n' 'CONFIRM=purge-all is required' >&2; exit 2; }
	@$(CACHE_ADMIN) purge --all $(ARGS)

backtest-factor: ## [研究] 运行逐因子 IC 回测
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) factor-ic --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(ARGS)

backtest-score: ## [研究] 运行 composite 分桶与确认层回测
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) score --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(ARGS)

backtest-robustness: ## [研究] 运行稳健性、regime 和幸存者偏差检查
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) robustness --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(ARGS)

backtest-visual: ## [研究] 运行单标的事件研究、交易仿真并生成 JSON/HTML
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) single --symbol "$(SYMBOL)" \
		$(START_ARG) $(END_ARG) --data-source "$(DATA_SOURCE)" \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" --cost-bps "$(COST_BPS)" \
		--html "$(HTML)" $(ARGS)

backtest-lifecycle-validate: ## [研究] 运行预注册 stateful 多标的 cache-only holdout
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) lifecycle-validate \
		--cache-db "$(CACHE_DB)" $(ARGS)

forward-record: ## [研究] 记录当日前瞻评分，可用 FORWARD_SYMBOLS 覆盖默认篮子
	@$(PYTHON) scripts/forward_log.py --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(GLOBAL_ARGS) \
		record $(FORWARD_SYMBOLS_ARG) $(ARGS)

forward-evaluate: ## [研究] 按 HORIZON_DAYS 评估成熟前瞻记录
	@$(PYTHON) scripts/forward_log.py --feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(GLOBAL_ARGS) \
		evaluate --horizon-days "$(HORIZON_DAYS)" $(ARGS)

contract: ## [质量] 校验 ARTIFACT，KIND 默认 result
	@[ -n "$(ARTIFACT)" ] || { printf '%s\n' 'ARTIFACT is required' >&2; exit 2; }
	@$(PYTHON) scripts/validate_agent_contract.py $(GLOBAL_ARGS) --kind "$(KIND)" "$(ARTIFACT)" $(ARGS)

compile: ## [质量] 编译全部 Python 模块
	@$(PYTHON) -m compileall -q scripts tests

test-unit: ## [质量] 运行 unittest 全量回归
	@$(PYTHON) -m unittest discover -s tests -q

test: ## [质量] 运行 pytest 全量回归
	@$(PYTHON) -m pytest tests/ -q

validate-skill: ## [质量] 运行 Codex skill 结构校验
	@[ -f "$(SKILL_VALIDATOR)" ] || { printf '%s\n' "skill validator not found: $(SKILL_VALIDATOR)" >&2; exit 2; }
	@$(PYTHON) "$(SKILL_VALIDATOR)" "$(SKILL_DIR)"

diff-check: ## [质量] 检查 Git whitespace 错误
	@git diff --check
	@git diff --cached --check

check: compile test validate-skill diff-check ## [质量] 运行提交前完整检查
