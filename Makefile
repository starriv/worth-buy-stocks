SHELL := /bin/sh
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

PYTHON ?= python3
CODEX_HOME ?= $(HOME)/.codex
SKILL_DIR := $(CURDIR)
CACHE_DB ?= $(SKILL_DIR)/data/market-data.sqlite3
SKILL_VALIDATOR ?= $(CODEX_HOME)/skills/.system/skill-creator/scripts/quick_validate.py

FEED ?= iex
OPTION_FEED ?= auto
ADJUSTMENT ?= split
SYMBOLS ?= AAPL,SPY,QQQ
OPTIONS_SYMBOLS ?=
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
OPTIONS_LEDGER_DB ?= $(SKILL_DIR)/data/options-ledger.sqlite3
OPTIONS_REPORT_DIR ?= $(SKILL_DIR)/artifacts/options
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
OPTIONS_SYMBOLS_ARG = $(if $(strip $(OPTIONS_SYMBOLS)),--symbols "$(OPTIONS_SYMBOLS)",)

.PHONY: help install doctor \
	analyze scan options-analyze options-scan options-manage options-report options-performance options-ledger-list chart notify \
	status integrity cache-warm cache-refresh sync-assets \
	warm-market-plan warm-market reconcile-plan reconcile-market \
	cache-purge cache-purge-all \
	backtest-factor backtest-score backtest-robustness backtest-visual backtest-lifecycle-validate \
	backtest-index-lifecycle-validate backtest-pragmatic-validate backtest-pragmatic-v1-validate \
	backtest-managed-ablation backtest-signal-ablation backtest-rotation-study \
	rotation-list rotation-record rotation-evaluate \
	rotation-universe-warm-plan rotation-universe-warm \
	forward-record forward-evaluate contract \
	compile test validate-skill diff-check check

help: ## [基础] 显示统一命令入口和常用变量
	@awk 'BEGIN { FS = ":.*## "; printf "worth-buy-stocks commands\n\n" } /^[a-zA-Z0-9_.-]+:.*## / { printf "  %-22s %s\n", $$1, $$2 } END { printf "\nVariables: PYTHON CACHE_DB OPTIONS_LEDGER_DB OPTIONS_REPORT_DIR FEED OPTION_FEED SYMBOLS OPTIONS_SYMBOLS SYMBOL START END ARGS GLOBAL_ARGS\n          DATA_SOURCE COST_BPS HTML YEARS WORKERS BATCH_SIZE MAX_RPM MAX_RUNTIME TIMEOUT FORCE\n" }' $(MAKEFILE_LIST)

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

options-analyze: ## [期权] 按 ARGS 中的 --strategy 分析 SYMBOL（默认垂直信用价差）
	@$(PYTHON) scripts/options_seller.py $(GLOBAL_ARGS) analyze --symbol "$(SYMBOL)" \
		--feed "$(FEED)" --option-feed "$(OPTION_FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" $(ARGS)

options-scan: ## [期权] 大盘核心池收租信用价差＋铁鹰初筛；指定标的仍受池限制
	@$(PYTHON) scripts/options_seller.py $(GLOBAL_ARGS) scan $(OPTIONS_SYMBOLS_ARG) \
		--feed "$(FEED)" \
		--adjustment "$(ADJUSTMENT)" --cache-db "$(CACHE_DB)" --top "$(TOP)" $(ARGS)

options-manage: ## [期权] 从本地台账核对账户并实时检查已有信用价差
	@$(PYTHON) scripts/options_position_manager.py review --live \
		--ledger-db "$(OPTIONS_LEDGER_DB)" --feed "$(FEED)" \
		--option-feed "$(OPTION_FEED)" --cache-db "$(CACHE_DB)" $(ARGS)

options-report: ## [期权] 实时检查并生成中文 Markdown/JSON 日报（默认不通知）
	@$(PYTHON) scripts/options_position_manager.py review --live \
		--ledger-db "$(OPTIONS_LEDGER_DB)" --feed "$(FEED)" \
		--option-feed "$(OPTION_FEED)" --cache-db "$(CACHE_DB)" \
		--include-performance --report-dir "$(OPTIONS_REPORT_DIR)" --report-format both $(ARGS)

options-performance: ## [期权] 汇总实际平仓成交与管理记录覆盖率
	@$(PYTHON) scripts/options_position_manager.py performance \
		--ledger-db "$(OPTIONS_LEDGER_DB)" $(ARGS)

options-ledger-list: ## [期权] 列出本地定义风险价差台账
	@$(PYTHON) scripts/options_position_manager.py list \
		--ledger-db "$(OPTIONS_LEDGER_DB)" $(ARGS)

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

backtest-index-lifecycle-validate: ## [研究] 运行预注册指数 ETF stateful 分层 cache-only holdout
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) index-lifecycle-validate \
		--cache-db "$(CACHE_DB)" $(ARGS)

backtest-pragmatic-validate: ## [研究] 重放已拒绝的务实分段入场 v0 holdout
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) pragmatic-entry-validate \
		--cache-db "$(CACHE_DB)" $(ARGS)

backtest-pragmatic-v1-validate: ## [研究] 重放已拒绝的务实分段入场 v1 独立 holdout
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) pragmatic-entry-v1-validate \
		--cache-db "$(CACHE_DB)" $(ARGS)

backtest-managed-ablation: ## [研究] 运行 research-only 执行层消融（SYMBOLS/START/END）
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) managed-ablation \
		--symbols "$(SYMBOLS)" $(START_ARG) $(END_ARG) \
		--cache-db "$(CACHE_DB)" --cost-bps "$(COST_BPS)" $(ARGS)

backtest-signal-ablation: ## [研究] 运行 research-only 信号层消融（SYMBOLS/START/END）
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) signal-ablation \
		--symbols "$(SYMBOLS)" $(START_ARG) $(END_ARG) \
		--cache-db "$(CACHE_DB)" --cost-bps "$(COST_BPS)" $(ARGS)

backtest-rotation-study: ## [研究] 运行预注册横截面 Top-N 轮动组合研究
	@$(PYTHON) scripts/backtest.py $(GLOBAL_ARGS) rotation-study \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" --cost-bps "$(COST_BPS)" $(ARGS)

rotation-list: ## [运行] 输出轮动晋升配置的当日 Top-N 名单（只分析不交易）
	@$(PYTHON) scripts/rotation_list.py $(GLOBAL_ARGS) --top "$(TOP)" \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" $(ARGS)

rotation-record: ## [运行] 输出并固化当日轮动名单为前瞻记录（调仓日使用）
	@$(PYTHON) scripts/rotation_list.py $(GLOBAL_ARGS) --top "$(TOP)" \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" --record $(ARGS)

rotation-evaluate: ## [研究] 结算已满 63 交易日的轮动前瞻记录（vs QQQ/SPY）
	@$(PYTHON) scripts/rotation_list.py $(GLOBAL_ARGS) \
		--feed "$(FEED)" --adjustment "$(ADJUSTMENT)" \
		--cache-db "$(CACHE_DB)" --evaluate $(ARGS)

# 规则化票池复验的前置数据计划：全资产目录 6 年 coverage（IEX 边界 2020-07-27）。
# 先 make sync-assets；完整执行为数小时长任务，须用户明确启动，可中断续跑。
rotation-universe-warm-plan rotation-universe-warm: YEARS = 6

rotation-universe-warm-plan: ## [预热] dry-run 规则化票池复验所需的全资产 6 年 coverage
	@$(CACHE_ADMIN) reconcile-market $(MARKET_JOB_ARGS) \
		$(FORCE_ASSETS_ARG) --dry-run $(ARGS)

rotation-universe-warm: ## [预热] 显式执行全资产 6 年预热（长任务，可中断续跑）
	@$(CACHE_ADMIN) reconcile-market $(MARKET_JOB_ARGS) \
		$(FORCE_ASSETS_ARG) $(ARGS)

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

test: ## [质量] 运行全量回归（优先 pytest，缺失时回退 unittest）
	@if $(PYTHON) -c 'import pytest' >/dev/null 2>&1; then \
		$(PYTHON) -m pytest tests/ -q; \
	else \
		$(PYTHON) -m unittest discover -s tests -q; \
	fi

validate-skill: ## [质量] 运行 Codex skill 结构校验
	@[ -f "$(SKILL_VALIDATOR)" ] || { printf '%s\n' "skill validator not found: $(SKILL_VALIDATOR)" >&2; exit 2; }
	@$(PYTHON) "$(SKILL_VALIDATOR)" "$(SKILL_DIR)"

diff-check: ## [质量] 检查 Git whitespace 错误
	@git diff --check
	@git diff --cached --check

check: compile test validate-skill diff-check ## [质量] 运行提交前完整检查
