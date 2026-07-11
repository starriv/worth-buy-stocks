# Alpaca Basic 全市场历史行情安全预热

> **日期**: 2026-07-10

## 背景

SQLite read-through 已能复用单次分析取得的 final 日线，但原有 `cache_admin warm` 需要显式 symbols，也没有全市场发现、分页级共享限速、维护窗口或单实例保护。直接以多线程拉取数千 ticker 会把 worker/batch 并发误当作真实请求预算，并在中断后重复获取已完成范围。

## 实现方案

- 每次运行发现一次 NYSE/NASDAQ active scanner universe；`--symbols-file` 用于受控 canary，并标记 `scope_limited`。
- 以 `sync_ranges` 批量计算最近三年真实缺口；完整覆盖不读三年 bars、不进入远端队列，partial 只按缺口组批。
- 默认 4 workers、50 symbols/batch；每个 Alpaca bars page（含失败与 retry）共享 120 RPM 均匀 limiter，首分钟无 burst。
- 429 使用有界指数退避与 jitter，并触发 job-wide cooldown；同一 DB 用 POSIX advisory lock 限制单实例。
- 网络请求在 SQLite 事务外；成功 batch 原子提交 bars 与 coverage，失败、取消或写入错误保持 missing。
- `--max-runtime`/SIGINT 停止派发并安全收敛；重跑根据 coverage 续传，不保存 job 历史表。

## API

```bash
python3 scripts/cache_admin.py warm-market --dry-run

python3 scripts/cache_admin.py warm-market \
  --years 3 --workers 4 --batch-size 50 \
  --max-rpm 120 --max-runtime 3600 \
  --feed iex --adjustment split
```

默认数据库为 skill 根目录 `data/market-data.sqlite3`。stdout 只输出最终 JSON，进度/心跳写 stderr。退出码：完成或 dry-run `0`、fatal `1`、参数 `2`、可恢复 partial `3`、同库锁 `4`、SIGINT `130`。

## 数据库变更

本预热功能本身无 schema 变更，交付时继续使用 schema v2 的 `daily_bars`、`sync_ranges` 与 `market_sessions`；后续资产目录将当前数据库提升到 v3，见 `003-asset-catalog-reconciliation.md`。锁文件位于 `<database>.warm-market.lock`，不保存凭证或远端响应。

## 修改文件

| 文件 | 变更 |
|---|---|
| `scripts/market_data_rate_limit.py` | page 级均匀限速、429 retry/cooldown、同库单实例 |
| `scripts/market_warmup.py` | universe、三年 coverage plan、有界 batch、resume、取消与摘要 |
| `scripts/fetching.py` | 在真实 `multi-bars` page 前增加可选 limiter/retry hook |
| `scripts/cache_admin.py` | `warm-market` CLI、dry-run、心跳、退出码与 coverage status |
| `tests/test_market_data_rate_limit.py` | 5,000-symbol/多页/8-worker、429、timeout、非 JSON 与锁测试 |
| `tests/test_market_warmup.py` | universe、partial、resume、故障、max-runtime、DB 写失败测试 |
| `tests/test_cache_admin.py` | 参数、stdout/stderr、SIGINT、退出码与 status 测试 |
| `SKILL.md`、`README.md`、`scripts/README.md`、`references/setup.md` | 运维入口、安全边界与恢复说明 |

## 验证

- 离线 fake-time：1,000 pages 和 5,000-symbol/3-page/8-worker 场景的任意 60 秒窗口均不超过 120；相邻令牌约 0.5 秒，429 后全局冷却且 retry 重新取令牌。
- 故障矩阵：429 耗尽、timeout、非 JSON、SQLite locked/read-only、单 batch 失败、SIGINT、max-runtime 与双实例锁均保持 coverage 诚实；第二次 plan 只含缺口。
- 真实 canary：AAPL/MSFT/SPY/QQQ、3 年、2 workers、2-symbol batch、30 RPM；2 pages、0 retries、0 429、峰值 2 RPM、平均 18.812 RPM、6.379 秒，写入 3,012 根 final 日线，SQLite integrity `ok`；二次运行 0 pages。
- 真实全市场 dry-run：4,793 symbols、已覆盖 3、待处理 4,790、96 batches、估算 3,755,360 行、bars pages 0；未启动完整长任务。
- 全量回归：423 unittest passed；一次性 pytest 环境 423 passed；`py_compile` 与后续 skill/whitespace 校验通过。

## 注意事项

- 120 RPM 只约束本 warm-market 进程；Alpaca Basic 200 RPM 的余量不能防止同账号其他客户端叠加超额。
- 首期只拉 `iex + split` final 日线，不拉 snapshot、分钟线、账户、新闻或 AI。
- 完整全市场预热由用户在空闲窗口主动运行，不进入 CI、cron 或自动收尾。
- asset universe 长期持久化和 coverage 查漏补缺已在后续 `003-asset-catalog-reconciliation.md` 交付；直接 HTTP rate-limit headers 与系统空闲探测仍不在范围。

*创建于: 2026-07-10*
