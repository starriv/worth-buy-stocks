#!/usr/bin/env python3
"""全市场扫描：取股池 → 流动性初筛 → 精简批量评分 → 提取候选 → 新闻复核降级 → AI 第二意见。

把"扫全市场找今天能买的"固化为一条命令。复用 ``pipeline.build_result`` 做两轮评分：

1. 精简轮：对流动性初筛后的活跃池跑**无 overlay**的核心价量评分，提取 ``verdict=="是"``
   （已隐含 ``confirmation.ok``，见 scoring/engine.py）的候选。
2. 复核轮：仅对候选开 Finnhub 新闻 overlay（``llm_context_from_finnhub``），重跑评分，
   把被软红旗降级（是→观察/否）的剔除到 ``downgraded``。

精简轮关 overlay 先粗筛、再只对候选开 Finnhub，省 API 调用。日线在两轮间复用，不重复拉。
AI 第二意见只对 top-N 截断后的最终候选逐票生成（只展示不评分），无 ANTHROPIC_API_KEY 时自动跳过。

设计原则同 indicators.py：只读、不下单、不打印凭证；网络分段各自失败降级，不阻断核心评分；
纯函数（过滤/提取/合并）与网络函数分离，纯函数可离线单测。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# 本模块位于 scripts/，平级 import（与 indicators.py 一致）
from fetching import _run_json, fetch_bars, fetch_snapshots  # noqa: E402
from finnhub import (  # noqa: E402
    fetch_finnhub_context,
    has_api_key as finnhub_has_api_key,
    llm_context_from_finnhub,
)
from pipeline import build_result  # noqa: E402
from ai_opinion import (  # noqa: E402
    has_api_key as ai_opinion_has_api_key,
    opinions_for_result,
    sdk_available as ai_opinion_sdk_available,
    unavailable_opinions,
)

BENCHMARK_SYMBOLS = ("SPY", "QQQ")
# IEX 日成交量约为全市场 2-3%；5 万 IEX 股 ≈ 数百万股全市场，作活跃粗筛门槛。
DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_VOLUME = 50_000
DEFAULT_SNAPSHOT_CHUNK = 200
DEFAULT_BARS_CHUNK = 80
DEFAULT_TOP = 20
HISTORY_DAYS = 728  # 约两年，同 indicators.py，预热 MA60 与周线 MACD


# ----------------------------------------------------------------------------
# 纯函数（可离线单测）
# ----------------------------------------------------------------------------

def filter_common_tickers(assets: list[dict]) -> list[str]:
    """从 ``alpaca asset list`` 的字典列表里挑普通股代码。

    过滤规则：``fractionable`` 为真、代码全字母、长度 ≤ 5（剔优先股如 ``T.PRC``、
    权证、单位），大写去重保序。
    """
    clean: list[str] = []
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        if not a.get("fractionable"):
            continue
        sym = str(a.get("symbol") or "").strip().upper()
        if not sym or not sym.isalpha() or len(sym) > 5:
            continue
        if sym not in clean:
            clean.append(sym)
    return clean


def filter_liquidity(snap_ctx: dict, min_price: float, min_volume: float) -> list[dict]:
    """从已归一化的 snapshot context 抽 close/volume 并按阈值过滤。

    输入是 ``fetch_snapshots`` 返回的 ``{status, symbols}``；取每个 symbol 的
    ``daily_bar.c`` 与 ``daily_bar.v``。返回 ``[{symbol, close, volume}]``，按
    成交量降序。snapshot 失败/缺字段时该 symbol 跳过。
    """
    if not isinstance(snap_ctx, dict) or snap_ctx.get("status") != "ok":
        return []
    out: list[dict] = []
    for sym, snap in (snap_ctx.get("symbols") or {}).items():
        if not isinstance(snap, dict):
            continue
        bar = snap.get("daily_bar") or {}
        # fetch_snapshots 归一化后用全称键（close/volume）；兼容原始 c/v
        close = bar.get("close")
        if close is None:
            close = bar.get("c")
        vol = bar.get("volume")
        if vol is None:
            vol = bar.get("v")
        if close is None or vol is None:
            continue
        try:
            close_f = float(close)
            vol_f = float(vol)
        except (TypeError, ValueError):
            continue
        if close_f < min_price or vol_f < min_volume:
            continue
        out.append({"symbol": str(sym).strip().upper(), "close": close_f, "volume": vol_f})
    out.sort(key=lambda x: -x["volume"])
    return out


def _candidate_row(sym: str, a: dict) -> dict:
    """从单个 symbol 的 build_result 分析对象抽候选行字段。"""
    sc = a.get("score") or {}
    bd = sc.get("factor_breakdown") or {}
    tp = sc.get("trade_plan") or {}
    et = a.get("entry_timing") or {}
    return {
        "symbol": sym,
        "composite": sc.get("composite"),
        "verdict": sc.get("verdict"),
        "suggested_action": sc.get("suggested_action"),
        "entry_timing": {
            "classification": et.get("classification"),
            "timing_score_pct": et.get("timing_score_pct"),
            "ext_atr_above_ma20": et.get("ext_atr_above_ma20"),
            "pullback_depth_pct": (et.get("pullback") or {}).get("depth_from_30d_high_pct"),
            "bounce_signal_count": (et.get("bounce") or {}).get("signal_count"),
        },
        "factor_breakdown": {
            k: (v.get("score_pct") if isinstance(v, dict) else None)
            for k, v in bd.items()
        },
        "last_close": a.get("last_close"),
        "relative_strength_pct": a.get("relative_strength_pct"),
        "trade_plan": {
            "suggested_entry_price": tp.get("suggested_entry_price"),
            "stop_loss_price": tp.get("stop_loss_price"),
            "take_profit_price": tp.get("take_profit_price"),
            "take_profit_2_price": tp.get("take_profit_2_price"),
            "trailing_stop_pct": tp.get("trailing_stop_pct"),
            "max_chase_price": tp.get("max_chase_price"),
        },
        "data_flags": sc.get("data_flags") or [],
        "blocking_reasons": sc.get("blocking_reasons") or [],
    }


def extract_candidates(result: dict, top_n: int | None = None) -> list[dict]:
    """从精简轮 build_result 输出提取 ``verdict=="是"`` 的候选，按 composite 降序。

    ``verdict=="是"`` 已隐含 ``confirmation.ok==true``（scoring/engine.py:82），
    无需再单独判 confirmation。跳过基准 SPY/QQQ。``top_n`` 截断。
    """
    syms = (result or {}).get("symbols") or {}
    cands = []
    for sym, a in syms.items():
        if sym in BENCHMARK_SYMBOLS or not isinstance(a, dict):
            continue
        sc = a.get("score")
        if not isinstance(sc, dict) or sc.get("verdict") != "是":
            continue
        cands.append(_candidate_row(sym, a))
    cands.sort(key=lambda x: (-(x.get("composite") or 0), x["symbol"]))
    if top_n is not None:
        cands = cands[:top_n]
    return cands


def _filter_entry(cands: list[dict], mode: str) -> list[dict]:
    """按入场时机分类过滤候选（--entry）。all 原样返回。"""
    if mode == "all":
        return cands
    want = "pullback_reversal" if mode == "pullback" else "trend_continuation"
    return [c for c in cands if (c.get("entry_timing") or {}).get("classification") == want]


def merge_news_downgrades(lean_candidates: list[dict], verified_result: dict) -> tuple[list[dict], list[dict]]:
    """对比精简轮候选与复核轮（带 llm_context）结果，分流 final / downgraded。

    复核后仍 ``verdict=="是"`` → final（用复核轮的 score 字段，因可能带 llm_overlay
    但未触发降级时 composite 不变）；变成 ``观察/否/无法评分/持仓需减风险`` → downgraded，
    透传 ``cap_applied`` 与 ``llm_overlay.downgrade_reasons``。
    """
    verified = (verified_result or {}).get("symbols") or {}
    final: list[dict] = []
    downgraded: list[dict] = []
    for cand in lean_candidates:
        sym = cand["symbol"]
        a = verified.get(sym)
        sc = (a or {}).get("score") if isinstance(a, dict) else None
        v_verdict = sc.get("verdict") if isinstance(sc, dict) else None
        if isinstance(sc, dict) and v_verdict == "是":
            final.append(_candidate_row(sym, a))
        else:
            llm = sc.get("llm_overlay") if isinstance(sc, dict) else None
            downgraded.append({
                "symbol": sym,
                "lean_composite": cand.get("composite"),
                "verified_verdict": v_verdict or "无法评分",
                "cap_applied": sc.get("cap_applied") if isinstance(sc, dict) else None,
                "downgrade_reasons": (llm or {}).get("downgrade_reasons") or [],
            })
    final.sort(key=lambda x: (-(x.get("composite") or 0), x["symbol"]))
    return final, downgraded


def _attach_ai_opinions(final_candidates: list[dict], scored_result: dict,
                        mode: str, llm_context: dict | None = None,
                        offline: bool = False) -> str | None:
    """对最终候选（top-N 截断后）逐票生成 AI 第二意见并附加到候选行。

    只展示不评分：候选的 composite/verdict/排序在调用前已定型，本函数只往
    每行追加 ``ai_opinion`` 字段。整体不可用返回 data_flag 文本（调用方决定
    是否记录）；``mode=="on"`` 时不可用直接报错；单票失败由 ai_opinion 内部
    降级为该票 ``status=unavailable``，不影响其他候选。
    """
    if not final_candidates or mode == "off":
        return None
    # ``--input`` is a hard offline boundary; replay artifacts may be supplied
    # in the input JSON, but live model calls never override it.
    if offline:
        return None
    if mode == "auto" and not (ai_opinion_sdk_available() and ai_opinion_has_api_key()):
        return "未配置 ANTHROPIC_API_KEY，跳过 AI 意见（auto 模式）"
    syms = [c["symbol"] for c in final_candidates]
    sys.stderr.write(f"AI 意见: 对 {len(syms)} 个候选逐票调用，预计每票数十秒\n")
    artifact = opinions_for_result(scored_result, llm_context=llm_context, symbols=syms)
    if artifact.get("status") != "ok":
        if mode == "on":
            raise RuntimeError(artifact.get("reason") or "AI 意见生成失败")
        return f"AI 意见不可用：{artifact.get('reason')}"
    if mode == "on":
        failures = unavailable_opinions(artifact, syms)
        if failures:
            detail = "；".join(f"{sym}: {reason}" for sym, reason in failures.items())
            raise RuntimeError(f"AI 意见生成失败：{detail}")
    entries = artifact.get("symbols") or {}
    for cand in final_candidates:
        entry = entries.get(cand["symbol"])
        if entry:
            cand["ai_opinion"] = entry
    return None


def _verdict_dist(result: dict) -> dict[str, int]:
    syms = (result or {}).get("symbols") or {}
    dist: dict[str, int] = {}
    for sym, a in syms.items():
        if sym in BENCHMARK_SYMBOLS:
            continue
        v = (a.get("score") or {}).get("verdict") if isinstance(a, dict) else None
        if not v:
            v = "无法评分"
        dist[v] = dist.get(v, 0) + 1
    return dist


def _market_regime(result: dict) -> dict:
    spy = (result or {}).get("symbols", {}).get("SPY")
    spy_a = spy if isinstance(spy, dict) else {}
    spy_sc = spy_a.get("score") or {}
    return {
        "spy_above_MA200": spy_a.get("ma", {}).get("above_MA200"),
        "market_risk_off": bool(result.get("market_risk_off")),
        "spy_composite": spy_sc.get("composite"),
        "spy_verdict": spy_sc.get("verdict"),
    }


# ----------------------------------------------------------------------------
# 网络函数
# ----------------------------------------------------------------------------

def fetch_active_assets(exchanges=("NYSE", "NASDAQ"), asset_class="us_equity", timeout=30) -> list[dict]:
    """从 ``alpaca asset list`` 取活跃美股原始字典列表（每个交易所一次调用）。"""
    out: list[dict] = []
    for ex in exchanges:
        cmd = [
            "alpaca", "asset", "list",
            "--asset-class", asset_class,
            "--status", "active",
            "--exchange", ex,
            "--quiet",
        ]
        try:
            data = _run_json(cmd, timeout)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"警告: asset list ({ex}) 失败已跳过: {e}\n")
            continue
        arr = data if isinstance(data, list) else (data.get("assets") or data.get("data") or [])
        if isinstance(arr, list):
            out.extend(arr)
    return out


def fetch_liquidity(symbols: list[str], feed: str, min_price: float, min_volume: float,
                    chunk: int = DEFAULT_SNAPSHOT_CHUNK, timeout: int = 30, *,
                    adjustment: str = "split", cache_mode: str = "auto",
                    cache_db: str | None = None, now_fn=None) -> tuple[list[dict], list[str]]:
    """分块拉 snapshot 并按流动性过滤。返回 (pool, data_flags)。单块失败跳过不阻断。

    分块之间并行（ThreadPoolExecutor），耗时从「各 chunk 之和」降到「最慢 chunk」。
    """
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return [], []
    batches = [syms[i:i + chunk] for i in range(0, len(syms), chunk)]
    data_flags: list[str] = []
    merged: dict[str, dict] = {}

    def _one(i_batch):
        i, batch = i_batch
        ctx = fetch_snapshots(
            batch, feed=feed, timeout=timeout, adjustment=adjustment,
            cache_mode=cache_mode, cache_db=cache_db, now_fn=now_fn,
        )
        return i, ctx

    workers = max(1, min(len(batches), 8))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_one, (idx, b)) for idx, b in enumerate(batches)]
        results: list[tuple[int, dict]] = [f.result() for f in futures]
    for _idx, ctx in sorted(results, key=lambda x: x[0]):
        if ctx.get("status") != "ok":
            data_flags.append(f"snapshot 分块拉取失败已跳过: {ctx.get('reason')}")
            continue
        if ctx.get("stale") or "不可用" in str(ctx.get("cache_note") or ""):
            data_flags.append(str(ctx.get("cache_note")))
        for sym, snap in (ctx.get("symbols") or {}).items():
            merged[sym] = snap
    snap_ctx = {"status": "ok", "symbols": merged} if merged else {"status": "unavailable", "reason": "无 snapshot"}
    pool = filter_liquidity(snap_ctx, min_price, min_volume)
    return pool, data_flags


def fetch_pool_bars(symbols: list[str], start: str, end: str, feed: str, adjustment: str,
                    chunk: int = DEFAULT_BARS_CHUNK, timeout: int = 30, *,
                    cache_mode: str = "auto", cache_db: str | None = None,
                    now_fn=None) -> tuple[dict, list[str]]:
    """分块拉两年日线，合并 {symbol: [bar]}。SPY/QQQ 若缺失单独补拉（regime 依赖）。

    分块之间并行（ThreadPoolExecutor），耗时从「各 chunk 之和」降到「最慢 chunk」。
    """
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return {}, []
    bars: dict[str, list] = {}
    data_flags: list[str] = []
    batches = [syms[i:i + chunk] for i in range(0, len(syms), chunk)]

    def _one(batch):
        try:
            b_bars, _used_feed, note = fetch_bars(
                batch, start, end, feed, adjustment, 10000, timeout,
                cache_mode=cache_mode, cache_db=cache_db, now_fn=now_fn,
            )
            degraded = note if "不可用" in str(note or "") or "回退" in str(note or "") else None
            return b_bars, degraded
        except Exception as e:  # noqa: BLE001
            return None, f"bars 分块拉取失败已跳过: {e}"

    workers = max(1, min(len(batches), 8))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_one, b) for b in batches]
        results = [f.result() for f in futures]
    for chunk_bars, err in results:
        if err:
            data_flags.append(err)
        if chunk_bars:
            bars.update(chunk_bars)
    # 确保基准在列（build_result 的 market_risk_off 依赖 SPY.above_MA200）
    for bench in BENCHMARK_SYMBOLS:
        if bench not in bars:
            try:
                b_bars, _u, _n = fetch_bars(
                    [bench], start, end, feed, adjustment, 10000, timeout,
                    cache_mode=cache_mode, cache_db=cache_db, now_fn=now_fn,
                )
                bars.update(b_bars)
            except Exception as e:  # noqa: BLE001
                data_flags.append(f"基准 {bench} 日线拉取失败: {e}")
    return bars, data_flags


# ----------------------------------------------------------------------------
# 离线输入（测试/复盘）
# ----------------------------------------------------------------------------

def _load_input(path: str) -> dict:
    """读离线 JSON（文件或 '-' stdin）。"""
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------------

def _ensure_benchmarks(symbols: list[str]) -> list[str]:
    clean = []
    for s in symbols:
        s = str(s or "").strip().upper()
        if s and s not in clean:
            clean.append(s)
    for b in BENCHMARK_SYMBOLS:
        if b not in clean:
            clean.append(b)
    return clean


def _today_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def scan(args) -> dict:
    """主流程：取股池 → 流动性初筛 → 精简评分 → 提取候选 → 新闻复核降级。"""
    feed = args.feed
    adjustment = args.adjustment
    as_of = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    data_flags: list[str] = []
    today_d = datetime.datetime.now(datetime.timezone.utc).date()
    today = today_d.isoformat()

    # ---- 1. 股池 ----
    if args.symbols:
        pool_syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbols_file:
        with open(args.symbols_file, encoding="utf-8") as f:
            pool_syms = [s.strip().upper() for s in f.read().split(",") if s.strip()]
        pool_syms = [s for s in pool_syms if s]
    else:
        exchanges = [e.strip().upper() for e in args.exchange.split(",") if e.strip()]
        assets = fetch_active_assets(exchanges=exchanges, timeout=args.timeout)
        pool_syms = filter_common_tickers(assets)
    universe_count = len(pool_syms)

    # ---- 2. 流动性初筛 ----
    pool, snap_flags = fetch_liquidity(
        pool_syms, feed, args.min_price, args.min_volume,
        chunk=args.snapshot_chunk, timeout=args.timeout,
        adjustment=adjustment, cache_mode=args.cache, cache_db=args.cache_db,
    )
    data_flags.extend(snap_flags)
    pool_syms = [p["symbol"] for p in pool]
    pool_count = len(pool_syms)

    # ---- 3. 精简评分（无 overlay）----
    start = args.start or (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    end = args.end or today
    score_syms = _ensure_benchmarks(pool_syms)
    bars, bars_flags = fetch_pool_bars(
        score_syms, start, end, feed, adjustment,
        chunk=args.bars_chunk, timeout=args.timeout,
        cache_mode=args.cache, cache_db=args.cache_db,
    )
    data_flags.extend(bars_flags)
    lean_result = build_result(score_syms, bars, feed, adjustment)
    lean_result.setdefault("_data_flags", [])  # 内部标记，不输出
    verdict_dist = _verdict_dist(lean_result)

    # ---- 4. 提取候选 ----
    lean_candidates = extract_candidates(lean_result, top_n=None)
    # --entry 是候选资格过滤，必须在 Finnhub 复核前执行，避免对不会进入结果的
    # 另一类时机候选消耗新闻 API 配额。top-N 仍在新闻后截断，以便降级后回补。
    candidates = _filter_entry(lean_candidates, args.entry)

    # ---- 5. 新闻复核降级 ----
    downgraded: list[dict] = []
    verify = args.verify_news
    do_verify = verify == "on" or (verify == "auto" and finnhub_has_api_key())
    scored_result, cand_llm_ctx = lean_result, None
    if do_verify and candidates:
        cand_syms = _ensure_benchmarks([c["symbol"] for c in candidates])
        finnhub_ctx = fetch_finnhub_context(
            [c["symbol"] for c in candidates], today=today_d,
            news_days=args.finnhub_news_days, earnings_days=args.finnhub_earnings_days,
            timeout=args.finnhub_timeout,
        )
        llm_ctx = llm_context_from_finnhub(finnhub_ctx, today=today_d)
        verified = build_result(
            cand_syms, bars, feed, adjustment,
            llm_context=llm_ctx, finnhub_context=finnhub_ctx,
        )
        final_candidates, downgraded = merge_news_downgrades(candidates, verified)
        scored_result, cand_llm_ctx = verified, llm_ctx
    else:
        final_candidates = candidates
        if do_verify and not candidates:
            pass  # 无候选，无需复核
        elif verify == "auto" and not finnhub_has_api_key():
            data_flags.append("未配置 FINNHUB_API_KEY，跳过新闻复核（auto 模式）")

    if args.top is not None:
        final_candidates = final_candidates[:args.top]

    # ---- 6. AI 第二意见（仅最终候选；只展示不评分）----
    ai_flag = _attach_ai_opinions(final_candidates, scored_result, args.ai_opinion, cand_llm_ctx)
    if ai_flag:
        data_flags.append(ai_flag)

    return {
        "as_of": as_of,
        "feed": feed,
        "adjustment": adjustment,
        "thresholds": {"min_price": args.min_price, "min_volume": args.min_volume},
        "counts": {
            "universe": universe_count,
            "liquidity_pool": pool_count,
            "scored": len([s for s in score_syms if s not in BENCHMARK_SYMBOLS]),
            "candidates_lean": len(lean_candidates),
            "candidates_entry_filtered": len(candidates),
            "verdict_dist": verdict_dist,
        },
        "market_regime": _market_regime(lean_result),
        "candidates": final_candidates,
        "downgraded": downgraded,
        "data_flags": data_flags,
    }


def _scan_offline(args) -> dict:
    """离线复盘：从 --input JSON 读预取的 assets/snapshots/bars，不触网。

    输入 JSON 形如 {"assets":[...], "snapshots":{...}, "bars":{...}, "finnhub":{...}}。
    用于测试与回放今天的结果。"""
    data = _load_input(args.input)
    feed = args.feed
    adjustment = args.adjustment
    as_of = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    today_d = datetime.datetime.now(datetime.timezone.utc).date()

    if args.symbols:
        pool_syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        pool_syms = filter_common_tickers(data.get("assets") or [])

    snap_ctx = data.get("snapshots") or {"status": "unavailable", "reason": "无 snapshot"}
    pool = filter_liquidity(snap_ctx, args.min_price, args.min_volume)
    pool_syms = [p["symbol"] for p in pool]

    bars = data.get("bars") or {}
    score_syms = _ensure_benchmarks(pool_syms)
    lean_result = build_result(score_syms, bars, feed, adjustment)
    verdict_dist = _verdict_dist(lean_result)
    lean_candidates = extract_candidates(lean_result, top_n=None)
    candidates = _filter_entry(lean_candidates, args.entry)

    downgraded: list[dict] = []
    finnhub_ctx = data.get("finnhub")
    scored_result, cand_llm_ctx = lean_result, None
    if args.verify_news != "off" and candidates and isinstance(finnhub_ctx, dict):
        llm_ctx = llm_context_from_finnhub(finnhub_ctx, today=today_d)
        cand_syms = _ensure_benchmarks([c["symbol"] for c in candidates])
        verified = build_result(
            cand_syms, bars, feed, adjustment,
            llm_context=llm_ctx, finnhub_context=finnhub_ctx,
        )
        final_candidates, downgraded = merge_news_downgrades(candidates, verified)
        scored_result, cand_llm_ctx = verified, llm_ctx
    else:
        final_candidates = candidates

    if args.top is not None:
        final_candidates = final_candidates[:args.top]

    # 离线复盘是硬边界：只使用输入 artifact，任何 live 开关都不触网。
    _attach_ai_opinions(final_candidates, scored_result, args.ai_opinion, cand_llm_ctx, offline=True)

    return {
        "as_of": as_of,
        "feed": feed,
        "adjustment": adjustment,
        "offline": True,
        "thresholds": {"min_price": args.min_price, "min_volume": args.min_volume},
        "counts": {
            "universe": len(pool_syms) if args.symbols else len(filter_common_tickers(data.get("assets") or [])),
            "liquidity_pool": len(pool_syms),
            "scored": len([s for s in score_syms if s not in BENCHMARK_SYMBOLS]),
            "candidates_lean": len(lean_candidates),
            "candidates_entry_filtered": len(candidates),
            "verdict_dist": verdict_dist,
        },
        "market_regime": _market_regime(lean_result),
        "candidates": final_candidates,
        "downgraded": downgraded,
        "data_flags": [],
    }


def _build_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="全市场扫描：取股池→流动性初筛→精简评分→新闻复核降级")
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--exchange", default="NYSE,NASDAQ", help="股池来源交易所，逗号分隔")
    p.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE)
    p.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME, help="IEX 日成交量下限")
    p.add_argument("--snapshot-chunk", type=int, default=DEFAULT_SNAPSHOT_CHUNK)
    p.add_argument("--bars-chunk", type=int, default=DEFAULT_BARS_CHUNK)
    p.add_argument("--top", type=int, default=DEFAULT_TOP)
    p.add_argument("--entry", choices=("all", "pullback", "trend"), default="all",
                   help="按入场时机过滤候选：pullback=只要回调拐点，trend=只要趋势延续，all=不过滤")
    p.add_argument("--verify-news", choices=("auto", "on", "off"), default="auto",
                   help="auto=有 FINNHUB_API_KEY 才复核，on=强制复核，off=关闭")
    p.add_argument("--ai-opinion", choices=("auto", "on", "off"), default="auto",
                   help="仅对最终候选生成 AI 第二意见（只展示不评分）：auto=有 ANTHROPIC_API_KEY 才调用，on=失败时报错，off=关闭")
    p.add_argument("--finnhub-news-days", type=int, default=30)
    p.add_argument("--finnhub-earnings-days", type=int, default=14)
    p.add_argument("--finnhub-timeout", type=int, default=15)
    p.add_argument("--symbols", help="覆盖股池（逗号分隔，跳过 asset list）")
    p.add_argument("--symbols-file", help="覆盖股池（文件）")
    p.add_argument("--start", help="默认约两年前")
    p.add_argument("--end", help="默认今天")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument(
        "--cache", choices=("auto", "refresh", "off"), default="auto",
        help="行情缓存：auto=按 session 读穿 SQLite，refresh=强制刷新，off=仅远端",
    )
    p.add_argument(
        "--cache-db",
        help="SQLite 文件路径；默认 skill 根目录 data/market-data.sqlite3",
    )
    p.add_argument("--input", help="离线模式：读预取 JSON {assets,snapshots,bars,finnhub}，不触网")
    p.add_argument("--notify", choices=("on", "off"), default="off", help="on=把摘要推 Telegram")
    return p.parse_args(argv)


def _summary_text(result: dict) -> str:
    """把扫描结果压成 Telegram 摘要文本。"""
    cands = result.get("candidates") or []
    downs = result.get("downgraded") or []
    mr = result.get("market_regime") or {}
    lines = ["全市场扫描 %s" % result.get("as_of", "")[:10]]
    lines.append("股池 %s / 活跃池 %s / 是 %s" % (
        result.get("counts", {}).get("universe"),
        result.get("counts", {}).get("liquidity_pool"),
        result.get("counts", {}).get("candidates_lean"),
    ))
    lines.append("大盘: SPY %s risk_off=%s" % (mr.get("spy_verdict"), mr.get("market_risk_off")))
    if cands:
        lines.append("可买 %d:" % len(cands))
        entry_label = {"pullback_reversal": "拐点", "trend_continuation": "延续"}
        for c in cands:
            tp = c.get("trade_plan") or {}
            tag = entry_label.get((c.get("entry_timing") or {}).get("classification"), "")
            lines.append("  %s%s %s/100 入%s 止%s 止盈%s" % (
                c["symbol"], f"[{tag}]" if tag else "", c.get("composite"),
                tp.get("suggested_entry_price"),
                tp.get("stop_loss_price"), tp.get("take_profit_price")))
    else:
        lines.append("无可买候选")
    if downs:
        lines.append("被新闻降级 %d: %s" % (len(downs), ", ".join(d["symbol"] for d in downs)))
    return "\n".join(lines)


def main():
    args = _build_args()
    if args.input:
        result = _scan_offline(args)
    else:
        result = scan(args)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    if args.notify == "on" and not args.input:
        import subprocess as _sp
        skill_dir = os.path.dirname(os.path.abspath(__file__))
        _sp.run(
            ["python3", os.path.join(skill_dir, "notify_telegram.py")],
            input=_summary_text(result), text=True, capture_output=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.exit(1)
