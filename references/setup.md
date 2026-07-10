# 本地环境设置清单

首次使用或环境检查失败时,把本清单给用户在本机执行。不要让用户把密钥发到对话里。

## 必需依赖

- Python 3.10+:运行本 skill 的本地脚本。
- Alpaca CLI:提供 Market Data snapshot 和日线数据。
- Alpaca API keys:paper 或 live 均可,至少需要 Market Data 权限;若要纳入持仓/账户敞口,需要同一 profile 可读取 account / positions。

## 安装 Alpaca CLI

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
python3 --version
alpaca version
alpaca data multi-snapshots --symbols SPY,QQQ --feed iex --quiet
alpaca account get --quiet
alpaca position list --quiet
```

- `alpaca` 不存在:先安装 CLI。
- 认证失败:运行 `alpaca profile login` 或设置 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`。
- 账户/持仓读取失败不阻断价量评分,只是敞口 overlay 不可用。

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
