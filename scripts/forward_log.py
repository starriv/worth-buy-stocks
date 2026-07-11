#!/usr/bin/env python3
"""前瞻验证日志：把每次评分固化为样本外证据（docs/model-card.md 的执行侧）。

``record``   对篮子打分，把模型版本、信号日期、时机分类、介入前后结论与分数
             逐票追加到 JSONL；同日同票重跑覆盖旧行
             （幂等），跨日只追加、绝不改写历史——这是"样本外"成立的前提。
``evaluate`` 把已满足 horizon（默认 63 交易日，与回测口径一致）的记录与真实
             后续收益对账，按 verdict 分桶报均值收益与同窗口 SPY 超额。

纯函数（merge_records / evaluate_records / bucket_stats）不触网，可离线测试；
只有 CLI 的取数环节碰 Alpaca。跨 model_version 的记录不混算。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from backtesting.universe import UNIVERSE  # noqa: E402
from fetching import fetch_bars  # noqa: E402
from pipeline import build_result  # noqa: E402
from scoring import MODEL_VERSION  # noqa: E402
from skill_logging import (  # noqa: E402
    add_logging_arguments,
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)

DEFAULT_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "forward_log.jsonl")
DEFAULT_HORIZON = 63          # 交易日；与回测/确认层 A/B 的 63 日口径一致
RECORD_LOOKBACK_DAYS = 750    # 打分所需历史（MA200 + 12-1 动量预热）
BENCH = ["SPY", "QQQ"]
logger = get_logger("forward_log")


# ---- 纯函数（离线可测） ----

def merge_records(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同 (run_date, symbol, model_version) 的旧行被新行覆盖；其余保序保留。"""
    key = lambda r: (r.get("run_date"), r.get("symbol"), r.get("model_version"))  # noqa: E731
    fresh = {key(r) for r in new}
    return [r for r in existing if key(r) not in fresh] + list(new)


def evaluate_records(
    records: list[dict[str, Any]],
    closes_by_date: dict[str, dict[str, float]],
    calendar: list[str],
    horizon: int,
) -> dict[str, Any]:
    """对账 v1.1+ 信号与 legacy v1.0 记录。

    v1.1+：信号在已完成日线收盘后生成，股票与 SPY 都用 signal_date 后下一交易日
    的收盘作为统一、可复现的 entry；legacy 无 signal_date 记录保留原 last_close
    口径并标记 method；所有模型版本在统计时严格分桶，不跨版本混算。
    """
    date_idx = {d: i for i, d in enumerate(calendar)}

    def _close_on_or_before(sym: str, date: str, after: str) -> float | None:
        by_date = closes_by_date.get(sym, {})
        best = None
        for d, c in by_date.items():
            if after < d <= date and (best is None or d > best[0]):
                best = (d, c)
        return best[1] if best else None

    matured, immature, invalid = [], 0, 0
    for r in records:
        run_date = str(r.get("run_date") or "")
        signal_date = str(r.get("signal_date") or "")
        symbol = str(r.get("symbol") or "")
        if signal_date:
            # 信号使用 signal_date 收盘数据，执行从下一交易日收盘开始；股票和基准同日。
            ti = next((date_idx[d] for d in calendar if d > signal_date), None)
            entry_date = calendar[ti] if ti is not None else None
            entry_price = closes_by_date.get(symbol, {}).get(entry_date) if entry_date else None
            method = "next_session_close"
        else:
            # v1.0 legacy：缺少真实 signal_date，只能保留旧口径并显式标记。
            ti = next((date_idx[d] for d in calendar if d >= run_date), None)
            entry_date = calendar[ti] if ti is not None else None
            entry_price = r.get("last_close")
            method = "legacy_recorded_close"
        if ti is None:
            immature += 1
            continue
        if not isinstance(entry_price, (int, float)) or entry_price <= 0:
            invalid += 1
            continue
        if ti + horizon >= len(calendar):
            immature += 1
            continue
        target = calendar[ti + horizon]
        c1 = closes_by_date.get(symbol, {}).get(target)
        if c1 is None:
            c1 = _close_on_or_before(symbol, target, entry_date)
        spy0 = closes_by_date.get("SPY", {}).get(entry_date)
        spy1 = closes_by_date.get("SPY", {}).get(target)
        if not c1 or not spy0 or not spy1:
            invalid += 1
            continue
        ret = (c1 / entry_price - 1) * 100
        spy_ret = (spy1 / spy0 - 1) * 100
        matured.append({**r, "entry_date": entry_date, "entry_price": entry_price,
                        "evaluation_method": method, "realized_pct": round(ret, 2),
                        "spy_pct": round(spy_ret, 2),
                        "excess_pct": round(ret - spy_ret, 2)})
    return {"matured": matured, "immature_count": immature, "invalid_count": invalid}


def bucket_stats(matured: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 (model_version, verdict) 分桶的均值统计；跨版本不混算。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for m in matured:
        k = f"{m.get('model_version')}/{m.get('verdict')}"
        buckets.setdefault(k, []).append(m)
    out = {}
    for k, rows in sorted(buckets.items()):
        n = len(rows)
        out[k] = {
            "n": n,
            "mean_pct": round(sum(r["realized_pct"] for r in rows) / n, 2),
            "mean_excess_pct": round(sum(r["excess_pct"] for r in rows) / n, 2),
        }
    return out


def timing_bucket_stats(matured: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 (model_version, entry_timing) 分桶，供第四层样本外 A/B。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in matured:
        timing = row.get("entry_timing") or "unknown"
        key = f"{row.get('model_version')}/{timing}"
        buckets.setdefault(key, []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for key, rows in sorted(buckets.items()):
        n = len(rows)
        out[key] = {
            "n": n,
            "mean_pct": round(sum(r["realized_pct"] for r in rows) / n, 2),
            "mean_excess_pct": round(sum(r["excess_pct"] for r in rows) / n, 2),
        }
    return out


# ---- IO ----

def load_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---- CLI 子命令（触网） ----

def _fetch(
    symbols: list[str], lookback_days: int, feed: str, adjustment: str, timeout: int,
    *, cache_mode: str = "auto", cache_db: str | None = None,
):
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    return fetch_bars(
        symbols, start, end, feed, adjustment, 10000, timeout,
        cache_mode=cache_mode, cache_db=cache_db,
    )


def cmd_record(args) -> int:
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(UNIVERSE))
    syms = list(dict.fromkeys(symbols + BENCH))
    bars, used_feed, note = _fetch(
        syms, RECORD_LOOKBACK_DAYS, args.feed, args.adjustment, args.timeout,
        cache_mode=args.cache, cache_db=args.cache_db,
    )
    if note:
        logger.info("market_data_note", operation="forward_record", reason=note)
    result = build_result(syms, bars, used_feed, args.adjustment)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    run_date = now_utc.date().isoformat()
    rows = []
    for sym, a in result["symbols"].items():
        if sym in BENCH or not isinstance(a, dict) or "error" in a:
            continue
        sc = a.get("score") or {}
        if sc.get("composite") is None:
            continue
        rows.append({
            "run_date": run_date,
            "recorded_at": now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "model_version": MODEL_VERSION,
            "symbol": sym,
            "composite": sc.get("composite"),
            "base_verdict": sc.get("verdict_before_entry_timing"),
            "verdict": sc.get("verdict"),
            "entry_timing": (a.get("entry_timing") or {}).get("classification"),
            "timing_score_pct": (a.get("entry_timing") or {}).get("timing_score_pct"),
            "confirmation_ok": (sc.get("confirmation") or {}).get("ok"),
            "market_risk_off": bool(result.get("market_risk_off")),
            "signal_date": a.get("last_date"),
            "signal_close": a.get("last_close"),
            # legacy readers still recognize last_close; v1.1+ evaluates from next-session price.
            "last_close": a.get("last_close"),
        })
    merged = merge_records(load_jsonl(args.log), rows)
    write_jsonl(args.log, merged)
    print(f"记录 {len(rows)} 票（{run_date}, {MODEL_VERSION}）→ {args.log}，累计 {len(merged)} 行")
    return 0


def cmd_evaluate(args) -> int:
    records = load_jsonl(args.log)
    if not records:
        print("日志为空：先运行 record 积累记录")
        return 0
    syms = sorted({r["symbol"] for r in records} | {"SPY"})
    earliest = min(r["run_date"] for r in records)
    lookback = (datetime.date.today() - datetime.date.fromisoformat(earliest)).days + 30
    bars, _, note = _fetch(
        syms, lookback, args.feed, args.adjustment, args.timeout,
        cache_mode=args.cache, cache_db=args.cache_db,
    )
    if note:
        logger.info("market_data_note", operation="forward_evaluate", reason=note)
    closes = {s: {b["t"][:10]: b["c"] for b in bars.get(s, [])} for s in syms}
    calendar = sorted(closes.get("SPY", {}))
    res = evaluate_records(records, closes, calendar, args.horizon_days)
    print(f"记录 {len(records)} 行：到期 {len(res['matured'])}，未到期 {res['immature_count']}，无效/缺价 {res['invalid_count']}")
    if not res["matured"]:
        print(f"暂无可评估样本（horizon={args.horizon_days} 交易日）。继续积累。")
        return 0
    print(f"\n按 verdict 分桶（{args.horizon_days} 交易日，超额基准 SPY）：")
    for k, st in bucket_stats(res["matured"]).items():
        print(f"  {k:12}  n={st['n']:<4} 均值 {st['mean_pct']:+.2f}%   SPY 超额 {st['mean_excess_pct']:+.2f}%")
    print("\n按入场时机分桶：")
    for k, st in timing_bucket_stats(res["matured"]).items():
        print(f"  {k:28}  n={st['n']:<4} 均值 {st['mean_pct']:+.2f}%   SPY 超额 {st['mean_excess_pct']:+.2f}%")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="评分前瞻验证日志（record/evaluate）")
    p.add_argument("--log", default=os.path.normpath(DEFAULT_LOG))
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    p.add_argument("--cache-db", help="SQLite 文件路径；默认 skill 根目录 data/market-data.sqlite3")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("record", help="对篮子打分并追加当日记录（同日重跑覆盖）")
    pr.add_argument("--symbols", help="逗号分隔；默认回测篮子 UNIVERSE")
    pe = sub.add_parser("evaluate", help="对到期记录对账，按 verdict 分桶")
    pe.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON)
    add_logging_arguments(p)
    args = p.parse_args(argv)
    configure_logging(level=args.log_level, log_format=args.log_format)
    clear_log_context()
    bind_log_context(command=f"forward_log.{args.cmd}", feed=args.feed)
    return cmd_record(args) if args.cmd == "record" else cmd_evaluate(args)


if __name__ == "__main__":
    raise SystemExit(main())
