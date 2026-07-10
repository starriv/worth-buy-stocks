#!/usr/bin/env python3
"""Alpaca Market Data 日线拉取：分页、去重、feed 受限自动回退。

只调用本机 `alpaca` CLI 的 `data multi-bars`，不下单、不打印凭证。
sip 等高级 feed 因订阅受限（403/forbidden）时自动回退 iex 一次。
"""
import datetime
import json
import subprocess
from collections import defaultdict


class FeedLimitError(RuntimeError):
    """sip 等高级 feed 因订阅受限（403/forbidden）返回时抛出，用于触发回退。"""


def _is_feed_limit(text):
    t = (text or "").lower()
    return any(s in t for s in ("403", "forbidden", "subscription", "not authorized"))


def _fetch_feed(symbols, start, end, feed, adjustment, limit, timeout):
    """单一 feed 下拉取日线并处理分页；按时间戳去重后升序返回 {symbol: [bar,...]}。"""
    out = defaultdict(dict)  # symbol -> {timestamp: bar}，天然按 t 去重
    page_token = ""
    while True:
        cmd = [
            "alpaca", "data", "multi-bars",
            "--symbols", ",".join(symbols),
            "--start", start,
            "--end", end,
            "--timeframe", "1Day",
            "--adjustment", adjustment,
            "--feed", feed,
            "--limit", str(limit),
            "--sort", "asc",
            "--quiet",
        ]
        if page_token:
            cmd += ["--page-token", page_token]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            detail = res.stderr.strip() or res.stdout.strip()
            if _is_feed_limit(detail):
                raise FeedLimitError(detail)
            raise RuntimeError(f"alpaca multi-bars 失败: {detail}")
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"无法解析 alpaca 输出为 JSON: {e}; 输出前 200 字符: {res.stdout[:200]!r}"
            )
        for sym, bars in (data.get("bars") or {}).items():
            for b in bars:
                out[sym][b["t"]] = b      # 同一 t 覆盖，去重
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
    return {sym: [bymt[t] for t in sorted(bymt)] for sym, bymt in out.items()}


def _drop_unclosed_today(bars_map):
    """盘中运行时剔除今天的未完成日线；收盘后原样保留。

    盘中的当日 bar 只含开盘以来的部分量价，会污染 RSI/量能比/反转日检测等
    末根敏感指标（SKILL 口径是"已完成日线"）。当日盘中信息由 snapshot
    overlay 补充展示，不进评分。返回 (bars_map, note)。
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        market_closed = (now_et.hour, now_et.minute) >= (16, 5)
        today = now_et.date().isoformat()
    except Exception:  # 无 tzdata 时保守回退：UTC 21:05 前视为未收盘
        now = datetime.datetime.now(datetime.timezone.utc)
        market_closed = (now.hour, now.minute) >= (21, 5)
        today = now.date().isoformat()
    if market_closed:
        return bars_map, None
    dropped = False
    out = {}
    for sym, blist in bars_map.items():
        if blist and str(blist[-1].get("t", ""))[:10] == today:
            out[sym] = blist[:-1]
            dropped = True
        else:
            out[sym] = blist
    note = "盘中运行：今日日线未收盘，已剔除（评分基于最近一根已完成日线）" if dropped else None
    return out, note


def fetch_bars(symbols, start, end, feed, adjustment, limit, timeout):
    """拉取日线；sip 等 feed 受限时自动回退 iex 一次；盘中剔除今日未完成 bar。

    返回 (bars_dict, used_feed, note)。note 为回退/剔除说明或 None。
    """
    try:
        bars = _fetch_feed(symbols, start, end, feed, adjustment, limit, timeout)
        used_feed, note = feed, None
    except FeedLimitError as e:
        if feed.lower() == "iex":
            raise RuntimeError(f"iex feed 受限: {e}")
        note = f"{feed} feed 受限({e})，已回退 iex"
        bars = _fetch_feed(symbols, start, end, "iex", adjustment, limit, timeout)
        used_feed = "iex"
    bars, drop_note = _drop_unclosed_today(bars)
    if drop_note:
        note = f"{note}；{drop_note}" if note else drop_note
    return bars, used_feed, note


def _run_json(cmd, timeout):
    """Run an alpaca CLI command and parse JSON stdout; raise RuntimeError on failure."""
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        detail = (res.stderr.strip() or res.stdout.strip() or "unknown error").replace("\n", " ")
        raise RuntimeError(detail[:300])
    text = res.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"无法解析 alpaca 输出为 JSON: {e}; 输出前 200 字符: {text[:200]!r}")


def _normalize_snapshot(symbol, raw):
    """Normalize one alpaca multi-snapshots entry into a compact supplemental dict.

    Returns None when the entry has no usable daily bar / quote / trade / minute
    bar — callers should drop such symbols rather than emit a stub. Snapshot is
    supplemental only; it never feeds score.
    """
    raw = raw if isinstance(raw, dict) else {}
    daily = raw.get("dailyBar") if isinstance(raw.get("dailyBar"), dict) else None
    quote = raw.get("latestQuote") if isinstance(raw.get("latestQuote"), dict) else None
    trade = raw.get("latestTrade") if isinstance(raw.get("latestTrade"), dict) else None
    minute = raw.get("minuteBar") if isinstance(raw.get("minuteBar"), dict) else None
    if not (daily or quote or trade or minute):
        return None

    out = {"symbol": str(symbol or "").strip().upper()}

    if daily:
        bar = {
            "open": daily.get("o"),
            "high": daily.get("h"),
            "low": daily.get("l"),
            "close": daily.get("c"),
            "volume": daily.get("v"),
            "vwap": daily.get("vw"),
            "date": _snapshot_date(daily.get("t")),
        }
        bar = {k: v for k, v in bar.items() if v is not None}
        out["daily_bar"] = bar
        if daily.get("o") and daily.get("c"):
            try:
                out["daily_change_pct"] = round(
                    (float(daily["c"]) / float(daily["o"]) - 1) * 100, 2
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    if quote:
        q = {
            "bid": quote.get("bp"),
            "ask": quote.get("ap"),
            "bid_size": quote.get("bs"),
            "ask_size": quote.get("as"),
            "quote_time": quote.get("t"),
        }
        q = {k: v for k, v in q.items() if v is not None}
        if q:
            out["quote"] = q
            if quote.get("bp") is not None and quote.get("ap") is not None:
                try:
                    spread = round(float(quote["ap"]) - float(quote["bp"]), 4)
                    out["spread"] = spread
                    mid = (float(quote["ap"]) + float(quote["bp"])) / 2
                    if mid:
                        out["spread_pct"] = round(spread / mid * 100, 4)
                except (TypeError, ValueError):
                    pass

    if trade:
        t = {
            "price": trade.get("p"),
            "size": trade.get("s"),
            "trade_time": trade.get("t"),
        }
        t = {k: v for k, v in t.items() if v is not None}
        if t:
            out["latest_trade"] = t

    if minute:
        m = {
            "open": minute.get("o"),
            "high": minute.get("h"),
            "low": minute.get("l"),
            "close": minute.get("c"),
            "time": minute.get("t"),
        }
        m = {k: v for k, v in m.items() if v is not None}
        if m:
            out["minute_bar"] = m

    return out


def _snapshot_date(value):
    """Best-effort ISO date from an alpaca snapshot timestamp string."""
    if not value:
        return None
    text = str(value)
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]


def fetch_snapshots(symbols, feed="iex", timeout=30):
    """Fetch same-day snapshots via `alpaca data multi-snapshots` and normalize.

    Returns ``{"status", "as_of", "feed", "symbols"}``. Snapshots are supplemental
    only and never feed score; any failure degrades to ``status=unavailable``
    without blocking the price/volume pipeline. ``symbols`` keys are uppercased,
    order-preserved and de-duplicated against the input order.
    """
    clean = []
    for sym in symbols or []:
        s = str(sym or "").strip().upper()
        if s and s not in clean:
            clean.append(s)
    if not clean:
        return {"status": "unavailable", "reason": "没有可查询的 symbol"}

    try:
        raw = _run_json(
            ["alpaca", "data", "multi-snapshots",
             "--symbols", ",".join(clean),
             "--feed", feed, "--quiet"],
            timeout,
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"alpaca multi-snapshots 失败: {e}"}
    raw = raw if isinstance(raw, dict) else {}
    symbols_out = {}
    for sym in clean:
        norm = _normalize_snapshot(sym, raw.get(sym))
        if norm:
            symbols_out[sym] = norm
    if not symbols_out:
        return {"status": "unavailable", "reason": "multi-snapshots 返回空数据"}
    return {
        "status": "ok",
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "feed": feed,
        "symbols": symbols_out,
    }
