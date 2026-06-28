#!/usr/bin/env python3
"""从 Alpaca 日线确定性计算趋势/相对强度/技术指标（CLI 入口）。

价量只用 Alpaca Market Data（multi-bars）；账户/持仓只读用于敞口 overlay。
不下单、不打印凭证。
输出 JSON：每个 symbol 的均线、MACD、RSI、KDJ、量价，以及相对 SPY/QQQ 的强度。
所有指标基于已收盘日线计算（脚本默认请求的就是已完成日线）。

工程拆分（同目录模块）：
  metrics.py   纯指标原语（EMA/MACD/RSI/KDJ/均线/收益率/周线聚合）
  fetching.py  Alpaca CLI 日线拉取（分页/去重/feed 回退）
  finnhub.py   Finnhub quote/news/profile/earnings 补充上下文（可选）
  portfolio.py Alpaca CLI 账户/持仓只读读取与归一化
  analysis.py  单 symbol 分析与跨 symbol 聚合（相对强度）
本文件保留 CLI 与向后兼容的再导出（旧的 `import indicators as I` 仍可用）。

用法：
  indicators.py --symbols AAPL,SPY,QQQ --start 2024-06-01 --end 2026-06-19 \
      --feed iex --adjustment split

  # 离线/测试：直接喂 multi-bars 形状的 JSON（{"bars": {...}}），不调用网络
  alpaca data multi-bars --symbols AAPL,SPY,QQQ ... | indicators.py --input -

  # 可选：把新闻面风控 overlay 的结构化 JSON 传入评分层
  indicators.py --symbols AAPL,SPY,QQQ --llm-context-file news_context.json

  # 默认：非离线模式会自动尝试读取 Alpaca 账户/持仓，失败时不阻断价量评分
  indicators.py --symbols AAPL,SPY,QQQ --account-context auto

  # 可选：Finnhub 补充 quote/news/profile/earnings；无 key 时 auto 不触网
  indicators.py --symbols AAPL,SPY,QQQ --finnhub-context auto

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
from finnhub import (  # noqa: F401
    fetch_finnhub_context,
    has_api_key as finnhub_has_api_key,
    llm_context_from_finnhub,
    merge_llm_contexts,
    normalize_finnhub_context,
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
    relative_strength,
)
from pipeline import build_result  # noqa: F401
from portfolio import (  # noqa: F401
    fetch_account_context,
    normalize_account_context,
)
from scoring import score  # noqa: F401
from agent_contracts import (  # noqa: E402
    KIND_ACCOUNT,
    KIND_BARS,
    KIND_FINNHUB,
    KIND_NEWS,
    KIND_RESULT,
    ContractError,
    validate_payload,
)

BENCHMARK_SYMBOLS = ("SPY", "QQQ")


def _validate_or_drop(kind, payload, label):
    """Validate an optional overlay artifact; on failure, warn on stderr and return False.

    The caller decides what to do with a False return — typically drop the overlay
    (mark unavailable) so the core price/volume scoring continues uninterrupted.
    """
    try:
        validate_payload(kind, payload)
    except ContractError as e:
        sys.stderr.write(f"警告: {label} 违反契约，已丢弃该 overlay: {e}\n")
        return False
    return True


def _symbols_with_benchmarks(symbols, bars=None):
    clean = []
    for sym in symbols or []:
        s = str(sym or "").strip().upper()
        if s and s not in clean:
            clean.append(s)
    for bench in BENCHMARK_SYMBOLS:
        if bench not in clean and (bars is None or bench in bars):
            clean.append(bench)
    return clean


def _load_llm_context(path):
    """Load optional {SYMBOL: context} JSON for the one-way news-risk overlay."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not _validate_or_drop(KIND_NEWS, data, "--llm-context-file"):
        return None
    if not isinstance(data, dict):
        raise RuntimeError("--llm-context-file 必须是 JSON object")
    raw = data.get("symbols") if isinstance(data.get("symbols"), dict) else data
    if not isinstance(raw, dict):
        raise RuntimeError("--llm-context-file symbols 字段必须是 object")
    return {str(k).strip().upper(): v for k, v in raw.items() if isinstance(v, dict)}


def _load_account_context(path):
    """Load optional Alpaca account/positions context JSON for offline tests/replay."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not _validate_or_drop(KIND_ACCOUNT, data, "--account-context-file"):
        return {"status": "unavailable", "reason": "account_context 契约校验失败"}
    return normalize_account_context(data)


def _load_finnhub_context(path):
    """Load optional Finnhub supplemental context JSON for offline tests/replay."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not _validate_or_drop(KIND_FINNHUB, data, "--finnhub-context-file"):
        return {"status": "unavailable", "reason": "finnhub_context 契约校验失败"}
    return normalize_finnhub_context(data)


def _load_bars_input(path):
    """Load and validate the blocking bars artifact. Fatal on contract violation."""
    bars = _load_input(path)
    try:
        validate_payload(KIND_BARS, {"status": "ok", "bars": bars})
    except ContractError as e:
        raise RuntimeError(f"bars artifact 违反契约，无法评分: {e}") from e
    return bars


def _account_context_from_args(args):
    if args.account_context_file:
        return _load_account_context(args.account_context_file)
    if args.account_context == "off":
        return None
    # `--input` is the deterministic offline path; keep it network-free unless the
    # caller explicitly asks for a live account read.
    if args.input and args.account_context != "on":
        return None
    try:
        return fetch_account_context(timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        if args.account_context == "on":
            raise
        return {"status": "unavailable", "reason": f"Alpaca 账户/持仓读取失败: {e}"}


def _finnhub_context_from_args(args, symbols):
    if args.finnhub_context_file:
        return _load_finnhub_context(args.finnhub_context_file)
    if args.finnhub_context == "off":
        return None
    if args.input and args.finnhub_context != "on":
        return None
    if args.finnhub_context == "auto" and not finnhub_has_api_key():
        return None
    ctx = fetch_finnhub_context(
        symbols,
        news_days=args.finnhub_news_days,
        earnings_days=args.finnhub_earnings_days,
        timeout=args.finnhub_timeout,
    )
    if args.finnhub_context == "on" and ctx.get("status") != "ok":
        raise RuntimeError(ctx.get("reason") or f"Finnhub context 读取失败: {ctx.get('status')}")
    return ctx


def main():
    p = argparse.ArgumentParser(description="Alpaca 日线技术指标计算")
    p.add_argument("--symbols", help="逗号分隔，建议含 SPY,QQQ")
    p.add_argument("--start", help="默认约两年前（足够 MA60/周线 MACD 预热）")
    p.add_argument("--end", help="默认今天")
    p.add_argument("--feed", default="iex", help="Alpaca 数据源，默认 iex；无 SIP 权限不要用 sip")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--input", help="离线模式：从文件或 '-'(stdin) 读 multi-bars JSON，不调用网络")
    p.add_argument("--llm-context-file", help="新闻面风控 overlay JSON；只降级不加分")
    p.add_argument(
        "--account-context",
        choices=("auto", "on", "off"),
        default="auto",
        help="Alpaca 账户/持仓 overlay：auto=非离线模式尽量读取，on=失败时报错，off=关闭",
    )
    p.add_argument(
        "--account-context-file",
        help="离线账户/持仓 overlay JSON（形如 {'account': {...}, 'positions': [...]}）",
    )
    p.add_argument(
        "--finnhub-context",
        choices=("auto", "on", "off"),
        default="auto",
        help="Finnhub 补充上下文：auto=有 .env/FINNHUB_API_KEY 时读取，on=失败时报错，off=关闭",
    )
    p.add_argument("--finnhub-context-file", help="离线 Finnhub 补充上下文 JSON")
    p.add_argument("--finnhub-news-days", type=int, default=30, help="Finnhub 新闻回看天数")
    p.add_argument("--finnhub-earnings-days", type=int, default=14, help="Finnhub 财报日历前看天数")
    p.add_argument("--finnhub-timeout", type=int, default=15, help="Finnhub 单次请求超时秒数")
    args = p.parse_args()
    llm_context = _load_llm_context(args.llm_context_file)

    if args.input:
        bars = _load_bars_input(args.input)
        symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else list(bars.keys()))
        symbols = _symbols_with_benchmarks(symbols, bars)
        if not symbols:
            p.error("未找到可分析 symbols")
        account_context = _account_context_from_args(args)
        finnhub_context = _finnhub_context_from_args(args, symbols)
        effective_llm_context = merge_llm_contexts(
            llm_context, llm_context_from_finnhub(finnhub_context)
        )
        result = build_result(
            symbols, bars, args.feed, args.adjustment,
            llm_context=effective_llm_context, account_context=account_context,
            finnhub_context=finnhub_context,
        )
    else:
        if not args.symbols:
            p.error("非离线模式需要 --symbols")
        end = args.end or datetime.date.today().isoformat()
        # 约两年日线：MA60 与周线 MACD(需 ~78 周) 都能充分预热
        start = args.start or (datetime.date.today() - datetime.timedelta(days=728)).isoformat()
        symbols = _symbols_with_benchmarks(
            [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        )
        if not symbols:
            p.error("非离线模式需要 --symbols")
        account_context = _account_context_from_args(args)
        finnhub_context = _finnhub_context_from_args(args, symbols)
        effective_llm_context = merge_llm_contexts(
            llm_context, llm_context_from_finnhub(finnhub_context)
        )
        bars, used_feed, note = fetch_bars(
            symbols, start, end, args.feed,
            args.adjustment, args.limit, args.timeout)
        result = build_result(
            symbols, bars, used_feed, args.adjustment, note,
            llm_context=effective_llm_context, account_context=account_context,
            finnhub_context=finnhub_context,
        )

    try:
        validate_payload(KIND_RESULT, result)
    except ContractError as e:
        sys.stderr.write(f"警告: 评分结果自检失败: {e}\n")

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.exit(1)
