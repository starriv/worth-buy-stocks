# Alpaca 资产目录与行情完整性对账

> **日期**: 2026-07-10

## 背景

`warm-market` 已支持 scanner 股池的安全三年预热，但 scanner universe 不是 Alpaca 全部可交易资产，也不能回答“今天 Alpaca 支持哪些 symbol”“哪些当前资产尚无三年 coverage”。需要将资产发现持久化，并把资产目录与行情 coverage 分开管理。

## 实现方案

- 每个 UTC 日期最多远端刷新一次完整 Alpaca `active + us_equity` 目录；`--force` 可显式刷新。
- 资产按 Alpaca 稳定 ID upsert；新资产记录 `first_seen_at`，持续出现更新 `last_seen_at`，本次响应消失的旧资产只设为 `is_current=0`，不删除历史。
- 目录保留 tradable、fractionable、marginable、shortable、easy-to-borrow、exchange、name 等属性，并单独记录是否适合 Basic IEX 行情对账及排除原因。
- 对账目标为当前、active、US equity、tradable，且交易所属于 AMEX/ARCA/BATS/NASDAQ/NYSE 的安全 symbol；OTC 和不可交易资产保留在目录，但不伪装成行情完整。
- `reconcile-market` 复用 `warm-market` 的三年 coverage planner、batch worker、page 级均匀限速、429 全局冷却、原子写入、维护窗口、SIGINT 收敛和同库单实例锁。
- coverage 表达“远端已成功查询该范围”，不要求每个交易日都有 bar；合法无成交/上市前空区间可被诚实记录。实际存在 final bars 的 symbol 数在摘要中另行报告。

## API

```bash
# 同日默认读目录缓存；需要时用 --force 重新拉取
python3 scripts/cache_admin.py sync-assets

# 只规划完整资产目录的三年缺口，不请求 bars
python3 scripts/cache_admin.py reconcile-market --dry-run

# 在维护窗口显式执行，可中断并直接复跑
python3 scripts/cache_admin.py reconcile-market \
  --years 3 --workers 4 --batch-size 50 \
  --max-rpm 120 --max-runtime 3600 \
  --feed iex --adjustment split
```

`--symbols-file` 仅用于低速 canary；文件内 symbol 必须属于当前 eligible 资产。完整长任务不会由 skill、CI 或收尾流程自动启动。

## 数据库变更

schema v3 新增：

- `assets`：按 `asset_id` 保存目录属性、`is_current`、`data_eligible`、排除原因与 first/last seen；
- `asset_catalog_state`：单行保存同步时间和 source/current/tradable/eligible/unsupported 统计。

目录刷新先在内存完成校验，再在一个短事务中标记旧记录、upsert 新记录并更新 state。空响应、非法响应、SQLite 锁或触发器错误都会整体回滚，旧目录仍可用。v1/v2 首次打开自动迁移到 v3 并保留原有行情数据。

## 修改文件

| 文件 | 变更 |
|---|---|
| `scripts/market_data_store.py` | schema v3、资产资格判断、目录原子替换与查询 |
| `scripts/asset_catalog.py` | Alpaca asset list transport、日级 freshness 与强制刷新 |
| `scripts/market_reconcile.py` | 目录目标选择、coverage plan 与 warm executor 复用 |
| `scripts/cache_admin.py` | `sync-assets`、`reconcile-market`、资产 status 与退出码 |
| `tests/test_asset_catalog.py` | 资格、增删、原子失败、UTC freshness 与远端故障 |
| `tests/test_market_reconcile.py` | dry-run、补缺、续传、force、14k 容量与 canary |
| `tests/test_cache_admin.py`、`tests/test_market_data_cache.py` | CLI/status 与 v1/v2→v3 迁移 |

## 验证

- 离线容量测试：14,098 个资产、13,268 个 tradable，得到 12,949 个 IEX eligible 目标和 259 个 50-symbol batches；资产列表仅获取一次。
- 真实资产同步快照：14,027 个 current、13,231 个 tradable、12,971 个 eligible、1,056 个 unsupported；其中 796 个不可交易、260 个 OTC 排除。
- 真实全目录 dry-run：12,971 个待处理、260 batches、估算 10,169,264 行、bars pages 为 0。
- 真实 canary：AAPL/MSFT/SPY/QQQ、30 RPM、2 workers；2 pages、0 retry、0 次 429、5.729 秒，写入 3,012 根 final 日线；二次运行 0 pages，SQLite integrity `ok`。
- 全量回归：460 unittest passed；临时 pytest 环境 460 passed + 13 subtests。

## 注意事项

- 未执行全市场长任务；真实 canary 后仍有 12,967 个 eligible symbol 待补，不能报告为全市场完整。
- 资产数量是同步时点快照，会随 Alpaca 上下架和状态变化；目录不是历史每一天的 point-in-time 成分表。
- 本地 RPM 预算只约束当前进程；同一 Alpaca 账号的其他客户端仍需避免同时高频请求。
- Basic IEX 不支持的 OTC 被明确标记为 unsupported；目录完整不等于所有资产都有可获取的 IEX 行情。

*创建于: 2026-07-10*
