# SQLite 行情缓存与 session-aware 读穿

> **日期**: 2026-07-10

## 背景

单股、图表、scanner、回测和 forward log 原先会跨进程重复请求相同历史日线；盘中固定时钟剔除当日 K 线也无法正确覆盖节假日、提前收盘和 finalization 延迟。

## 实现方案

- `fetching.py` 保持兼容门面，并将 Alpaca bars/snapshot transport 与缓存策略分开。
- `market_data_cache.py` 使用 XNYS calendar 区分盘前、盘中、收盘宽限期和休市；final 日线 read-through，盘中 snapshot 更新 latest/live。
- 新完成 session 回拉最近 5 个交易日；重叠 OHLCV 变化时刷新该 symbol 的完整请求范围。
- DB/calendar/写入失败时保守回退远端；远端也失败时不写 coverage，不把陈旧数据伪装为已同步。
- `--input` 成为硬离线边界，不读写 DB，也不触发账户、Finnhub、snapshot、AI 或通知网络调用。

## API

- 行情入口新增 `cache_mode=auto|refresh|off`、`cache_db`；原位置参数调用保持兼容。
- `indicators.py`、`scanner.py`、`chart.py`、三个 backtest CLI 和 `forward_log.py` 新增 `--cache` / `--cache-db`。
- `cache_admin.py` 提供 `status`、`integrity-check`、`warm`、`refresh`、全市场 `warm-market` 与受保护的 `purge`。

## 数据库变更

默认路径为 `data/market-data.sqlite3`，可由 `WORTH_BUY_STOCKS_DB` 或 `--cache-db` 覆盖。行情缓存基础交付为 schema v2；资产目录功能将当前 schema 提升到 v3，包含：

- `daily_bars`：只保存可评分的完成日线；
- `live_daily_bars`：只保存当前 regular session 的未完成日 K；
- `latest_snapshots`：每个 symbol/feed 只保留最新一份（v2 起为 rowid 表，适配 KB 级 JSON payload）；
- `sync_ranges`：表达已查询覆盖，包括合法空区间（v2 移除了与主键重复的索引）；
- `market_sessions`：缓存 XNYS regular calendar；
- `feed_fallbacks`：记录 sip→iex 等 feed 受限回退，后续调用直接读可达 feed 的缓存，`--cache refresh` 会重新探测并清除。
- `assets`：保存完整 Alpaca active US equity 目录、交易属性、当前状态、IEX 对账资格与排除原因；
- `asset_catalog_state`：记录目录 UTC 同步时间和 source/current/tradable/eligible/unsupported 计数。

v1/v2 数据库在首次打开时自动迁移到 v3，保留 snapshot、bars 与 coverage。SQLite 使用 WAL、busy timeout、短事务和每操作独立连接（进程内按路径复用 store 实例）。范围 purge 会同步切开 coverage；`purge --all` 之后执行 VACUUM 回收文件空间；status/integrity 对缺失 DB 保持只读、不创建文件（status 使用 `quick_check`，完整扫描走 `integrity-check` 子命令）。

## 会话策略要点

- 收盘 + 15 分钟宽限后，若远端尚未发布最新完成日的日线，该日不标记覆盖，后续调用自动重查直到发布，避免当日 bar 静默缺失。
- 盘中捕获的 snapshot 在该 session 收盘定稿后视为陈旧：优先用 `daily_bars` 的最终日线合成（零网络），否则仅对受影响 symbol 触发一次远端刷新；远端不可用时才降级返回陈旧快照并置 `stale=true`。
- 日线远端故障时，warm 缓存返回已有数据并在 note 中标注降级；仅在缓存完全无数据时向上抛错。

## 修改文件

| 文件 | 变更 |
|---|---|
| `scripts/market_data_store.py` | schema、迁移、CRUD、coverage、并发与完整性 |
| `scripts/market_data_cache.py` | session 分类、bars/snapshot read-through、修订与降级策略 |
| `scripts/fetching.py` | 兼容门面、纯远端 transport、cache mode |
| `scripts/cache_admin.py` | 缓存治理 CLI 与全市场 warm-market 运维入口 |
| `scripts/market_warmup.py` | universe、coverage plan、有界 batch、resume 与维护窗口 |
| `scripts/market_data_rate_limit.py` | page 级均匀限速、429 全局冷却与同库单实例 |
| `scripts/asset_catalog.py` | Alpaca 资产目录拉取、UTC 日级 freshness 与原子替换 |
| `scripts/market_reconcile.py` | 全资产目标选择与三年 coverage 查漏补缺 |
| `scripts/indicators.py`、`scripts/scanner.py`、`scripts/chart.py` | 运行时接入与硬离线边界 |
| `scripts/backtest_*.py`、`scripts/forward_log.py` | 研究入口接入与无缓存基线 |
| `tests/test_*cache*.py`、`tests/test_market_data_policy.py` | schema/session/入口/admin/golden/故障与并发覆盖 |

## 验证

- SQLite 基础交付时 `unittest`/临时 pytest 各 392 passed；后续 warm-market 增量验收见 `002-market-data-warmup.md`。
- 真实 AAPL 5 次基准：cold 中位 4.1091s，warm 中位 0.0082s，提升 99.8%；warm 的 bars/snapshot/calendar 调用均为 0。
- 真实 4 票 scanner 冒烟：cold 4.513s，warm 0.032s；warm 三类行情调用均为 0，DB 118,784 bytes。
- remote/cache 的完整 `symbols.*.score` 与 scanner candidates 深度相等；live snapshot 写入后评分 `last_date` 仍为上一完成 session。
- skill `quick_validate`、SQLite `integrity_check`、`py_compile` 与 `git diff --check` 通过。

## 注意事项

- 完整 warm 覆盖才承诺盘前/休市零网络；冷启动和 partial miss 会只补缺失 symbol/range。
- SIP 回退 IEX 后只写 IEX 命名空间。
- 缓存错误随当前调用返回，不持久化到 SQLite，`cache_admin status` 会明确报告这一点。

*创建于: 2026-07-10*
