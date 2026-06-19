#!/usr/bin/env python3
"""从 Alpaca 日线确定性计算趋势/相对强度/技术指标（CLI 入口）。

只用 Alpaca Market Data（multi-bars）。不下单、不打印凭证。
输出 JSON：每个 symbol 的均线、MACD、RSI、KDJ、量价，以及相对 SPY/QQQ 的强度。
所有指标基于已收盘日线计算（脚本默认请求的就是已完成日线）。

工程拆分（同目录模块）：
  metrics.py   纯指标原语（EMA/MACD/RSI/KDJ/均线/收益率/周线聚合）
  fetching.py  Alpaca CLI 日线拉取（分页/去重/feed 回退）
  analysis.py  单 symbol 分析与跨 symbol 聚合（相对强度）
本文件保留 CLI 与向后兼容的再导出（旧的 `import indicators as I` 仍可用）。

用法：
  indicators.py --symbols AAPL,SPY,QQQ --start 2024-06-01 --end 2026-06-19 \
      --feed iex --adjustment split

  # 离线/测试：直接喂 multi-bars 形状的 JSON（{"bars": {...}}），不调用网络
  alpaca data multi-bars --symbols AAPL,SPY,QQQ ... | indicators.py --input -

依赖：仅 Python 标准库 + 本机 `alpaca` CLI。
"""
import argparse
import datetime
import json
import sys

# 向后兼容再导出：保持 `import indicators as I` 的旧访问路径可用。
from fetching import (  # noqa: F401
    FeedLimitError,
    _fetch_feed,
    _is_feed_limit,
    fetch_bars,
)
from metrics import (  # noqa: F401
    _recent_cross,
    ema_series,
    kdj,
    ma,
    macd,
    pct_return,
    rsi,
    to_weekly,
)
from analysis import (  # noqa: F401
    MA60_SLOPE_LOOKBACK,
    MIN_BARS,
    R1M,
    R3M,
    R6M,
    STRUCT_WINDOW,
    TRADING_DAYS_52W,
    _load_input,
    _weekly_bear,
    analyze_symbol,
    build_result,
    relative_strength,
)
from scoring import score  # noqa: F401


def main():
    p = argparse.ArgumentParser(description="Alpaca 日线技术指标计算")
    p.add_argument("--symbols", help="逗号分隔，建议含 SPY,QQQ")
    p.add_argument("--start", help="默认约两年前（足够 MA60/周线 MACD 预热）")
    p.add_argument("--end", help="默认今天")
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--input", help="离线模式：从文件或 '-'(stdin) 读 multi-bars JSON，不调用网络")
    args = p.parse_args()

    if args.input:
        bars = _load_input(args.input)
        symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else list(bars.keys()))
        result = build_result(symbols, bars, args.feed, args.adjustment)
    else:
        if not args.symbols:
            p.error("非离线模式需要 --symbols")
        end = args.end or datetime.date.today().isoformat()
        # 约两年日线：MA60 与周线 MACD(需 ~78 周) 都能充分预热
        start = args.start or (datetime.date.today() - datetime.timedelta(days=728)).isoformat()
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        bars, used_feed, note = fetch_bars(
            symbols, start, end, args.feed,
            args.adjustment, args.limit, args.timeout)
        result = build_result(symbols, bars, used_feed, args.adjustment, note)

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.exit(1)
