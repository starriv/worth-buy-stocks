#!/usr/bin/env python3
"""Alpaca Market Data 日线拉取：分页、去重、feed 受限自动回退。

只调用本机 `alpaca` CLI 的 `data multi-bars`，不下单、不打印凭证。
sip 等高级 feed 因订阅受限（403/forbidden）时自动回退 iex 一次。
"""
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


def fetch_bars(symbols, start, end, feed, adjustment, limit, timeout):
    """拉取日线；sip 等 feed 受限时自动回退 iex 一次。

    返回 (bars_dict, used_feed, note)。note 为回退说明或 None。
    """
    try:
        return _fetch_feed(symbols, start, end, feed, adjustment, limit, timeout), feed, None
    except FeedLimitError as e:
        if feed.lower() == "iex":
            raise RuntimeError(f"iex feed 受限: {e}")
        note = f"{feed} feed 受限({e})，已回退 iex"
        bars = _fetch_feed(symbols, start, end, "iex", adjustment, limit, timeout)
        return bars, "iex", note
