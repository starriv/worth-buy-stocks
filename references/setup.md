# 本地环境设置清单

首次使用或环境检查失败时,把本清单给用户在本机执行。不要让用户把密钥发到对话里。

## 必需依赖

- Python 3.10+:运行本 skill 的本地脚本。
- Python 依赖:`requirements.txt` 中固定的 `structlog==26.1.0`，负责全部 stderr 运行日志。
- GNU Make:提供根目录统一命令入口；macOS Command Line Tools 与常见 Linux 开发环境默认包含。
- Alpaca CLI:提供 Market Data snapshot 和日线数据。
- Alpaca API keys:paper 或 live 均可,至少需要 Market Data 权限;若要纳入持仓/账户敞口,需要同一 profile 可读取 account / positions。

## 安装 Alpaca CLI

先安装 Python 依赖：

```bash
SKILL_DIR="${SKILL_DIR:-$HOME/.codex/skills/worth-buy-stocks}"
make -C "$SKILL_DIR" install
```

若安装器提示当前 Python 不支持 structlog 26.1.0，先确认 `python3 --version` 为 3.10 或更高；Python 3.9 不在本 skill 支持范围。

再安装 Alpaca CLI：

```bash
# macOS / Homebrew
brew install alpacahq/tap/cli

# 或 Go
go install github.com/alpacahq/cli/cmd/alpaca@latest
```

## 配置凭证(二选一)

```bash
# 推荐:让 CLI 管理 profile
alpaca profile login

# 或本地 shell 环境变量;只在用户机器设置,不要写入 skill 或输出
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
# 默认数据源固定使用 iex。没有 SIP 权限时不要设置为 sip。
export ALPACA_DATA_FEED="iex"
```

## 验证环境

```bash
SKILL_DIR="${SKILL_DIR:-$HOME/.codex/skills/worth-buy-stocks}"
python3 --version
python3 -c 'import structlog; print(structlog.__version__)'
make --version
make -C "$SKILL_DIR" help
alpaca version
alpaca data multi-snapshots --symbols SPY,QQQ --feed iex --quiet
alpaca account get --quiet
alpaca position list --quiet
python3 "$SKILL_DIR/scripts/cache_admin.py" status
```

- `alpaca` 不存在:先安装 CLI。
- `structlog` 导入失败:使用同一个 Python 运行 `python3 -m pip install -r "$SKILL_DIR/requirements.txt"`。
- 认证失败:运行 `alpaca profile login` 或设置 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`。
- 账户/持仓读取失败不阻断价量评分,只是敞口 overlay 不可用。

## SQLite 行情缓存

默认数据库位于 skill 根目录 `data/market-data.sqlite3`，目录和文件会在首次在线分析时创建。`data/` 只保存本机公开行情缓存，不提交到仓库，也不保存 Alpaca/Finnhub/Telegram 凭证。

如需把缓存放到其他磁盘，设置：

```bash
export WORTH_BUY_STOCKS_DB="$HOME/.local/share/worth-buy-stocks/market-data.sqlite3"
```

常用检查与恢复：

```bash
make -C "$SKILL_DIR" status
make -C "$SKILL_DIR" integrity
make -C "$SKILL_DIR" cache-refresh SYMBOLS=SPY,QQQ
make -C "$SKILL_DIR" warm-market-plan
make -C "$SKILL_DIR" sync-assets
make -C "$SKILL_DIR" reconcile-plan
```

缓存损坏或不可写时，在线分析会保守回退 Alpaca 并返回降级说明；`--cache off` 可建立无缓存对照。`--input` 始终旁路数据库和网络。

空闲时预热全市场最近三年 final 日线：

```bash
make -C "$SKILL_DIR" warm-market \
  YEARS=3 WORKERS=4 BATCH_SIZE=50 \
  MAX_RPM=120 MAX_RUNTIME=3600 FEED=iex ADJUSTMENT=split
```

默认写入 `$SKILL_DIR/data/market-data.sqlite3`，只请求 bars，不请求 snapshot。维护窗口到期或 Ctrl-C 后直接重复同一命令，已完成 coverage 会跳过。120 RPM 是该预热进程的保守预算，低于 Alpaca Basic 200 RPM；它无法限制同账号的其他客户端，因此预热期间仍应避免并发运行高频行情任务。正式运行前先执行 `--dry-run`；小规模连通性验证用 `--symbols-file` 并把 `--max-rpm` 设为 30 或更低。

若目标是 Alpaca 当前全部可交易资产，而不是 scanner 股池，使用资产目录对账：

```bash
make -C "$SKILL_DIR" sync-assets
make -C "$SKILL_DIR" reconcile-plan
make -C "$SKILL_DIR" reconcile-market \
  YEARS=3 WORKERS=4 BATCH_SIZE=50 \
  MAX_RPM=120 MAX_RUNTIME=3600 FEED=iex ADJUSTMENT=split
```

`sync-assets` 按 UTC 日期缓存完整 active US equity 目录；同日复跑默认只读 SQLite，`--force` 才刷新。`reconcile-market` 只为当前、可交易、Basic IEX 支持的主流交易所资产补 final 日线，OTC 与非交易资产仍保留在目录并明确排除。完整任务不会自动执行；先 dry-run，必要时用 `--symbols-file` 和不高于 30 RPM 做 canary，然后在空闲窗口显式启动。

## 可选依赖

- Finnhub API key:补充 quote/news/profile/earnings。从本地 `.env` 的 `FINNHUB_API_KEY` 读取;无 key 自动跳过。
- AI 意见(Claude API):对评分结论生成对照展示用的 AI 第二意见,不参与评分。需要两项都满足才启用,缺任一项自动关闭、其余功能不受影响:
  1. `pip install anthropic`(官方 SDK);
  2. 本地 `.env` 配置 `ANTHROPIC_API_KEY`。
- Telegram 推送:从本地 `.env` 读取 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`;仅在用户要求通知时需要。

## `.env` 位置

按以下顺序查找,取第一个命中的文件;环境变量作为最终 fallback:

1. `WORTH_BUY_STOCKS_ENV_FILE` 环境变量指定的路径。
2. skill 根目录 `.env`(存在才使用)。
3. `~/.config/worth-buy-stocks/.env`(尊重 `XDG_CONFIG_HOME`)。

如果 skill 目录本身是会被分享或发布的 git repo,建议把密钥放到第 3 个位置,repo 内不留 `.env`:

```bash
mkdir -p ~/.config/worth-buy-stocks
mv "$SKILL_DIR/.env" ~/.config/worth-buy-stocks/.env
```
