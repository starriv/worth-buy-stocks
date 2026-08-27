"""Live read-only asset, event-calendar, and option-chain orchestration."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fetching import _run_json
from finnhub import (
    fetch_economic_calendar,
    fetch_finnhub_context,
    has_api_key as finnhub_has_api_key,
    llm_context_from_finnhub,
)

from .config import OptionsSellerConfig
from .data import fetch_normalized_chain
from .events import extract_corporate_action_events, normalize_events
from .strategies import strategy_modules


def filter_options_assets(
    assets: Any,
    exchanges: tuple[str, ...] = ("AMEX", "ARCA", "BATS", "NASDAQ", "NYSE", "NYSEARCA"),
) -> list[str]:
    """Keep active, tradable, option-enabled US equity and ETF symbols."""
    rows = assets if isinstance(assets, list) else (
        (assets or {}).get("assets") or (assets or {}).get("data") or []
        if isinstance(assets, dict) else []
    )
    allowed = {str(value).upper() for value in exchanges}
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in (None, "active"):
            continue
        if not row.get("tradable", True):
            continue
        if allowed and str(row.get("exchange") or "").upper() not in allowed:
            continue
        if "has_options" not in (row.get("attributes") or []):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
            continue
        if symbol not in out:
            out.append(symbol)
    return out


def fetch_options_assets(timeout: int = 30) -> list[dict]:
    payload = _run_json([
        "alpaca", "asset", "list",
        "--asset-class", "us_equity",
        "--status", "active",
        "--attributes", "has_options",
        "--quiet",
    ], timeout)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("assets") or payload.get("data") or []
    return []


def fetch_corporate_action_events(
    symbols: list[str], start: str, end: str, timeout: int = 30
) -> dict[str, dict[str, list[str]]]:
    """Fetch announced cash dividends, following pagination."""
    if not symbols:
        return {}
    payloads = []
    token = ""
    while True:
        cmd = [
            "alpaca", "data", "corporate-actions",
            "--symbols", ",".join(symbols),
            "--types", "cash_dividend",
            "--start", start,
            "--end", end,
            "--limit", "1000",
            "--quiet",
        ]
        if token:
            cmd += ["--page-token", token]
        payload = _run_json(cmd, timeout)
        payloads.append(payload)
        token = str((payload or {}).get("next_page_token") or "") if isinstance(payload, dict) else ""
        if not token:
            return extract_corporate_action_events(payloads)


def collect_live_events(
    symbols: list[str], start: str, end: str, args, *, horizon_days: int | None = None
) -> tuple[dict[str, dict], list[str]]:
    """Combine Finnhub earnings/news with Alpaca dividend announcements."""
    if args.events == "off":
        return {symbol: {"status": "unavailable"} for symbol in symbols}, ["事件日历已关闭"]
    flags = []
    earnings_ctx: dict[str, Any] = {"status": "unavailable", "symbols": {}}
    do_finnhub = args.events == "on" or (args.events == "auto" and finnhub_has_api_key())
    today = dt.date.fromisoformat(start)
    if do_finnhub:
        earnings_ctx = fetch_finnhub_context(
            symbols,
            today=today,
            news_days=30,
            earnings_days=int(horizon_days or args.dte_max),
            max_news=10, timeout=args.finnhub_timeout, include_benchmarks=False,
        )
    else:
        flags.append("未配置 Finnhub，财报日历不可用")
    risk_ctx = (
        llm_context_from_finnhub(earnings_ctx, today=today)
        if earnings_ctx.get("status") == "ok" else {}
    )
    try:
        dividends = fetch_corporate_action_events(symbols, start, end, timeout=args.timeout)
        dividend_ok = True
    except Exception as exc:  # noqa: BLE001 - optional risk source
        dividends, dividend_ok = {}, False
        flags.append(f"Alpaca 除息日历不可用: {exc}")

    out = {}
    for symbol in symbols:
        fh = ((earnings_ctx.get("symbols") or {}).get(symbol) or {})
        normalized_fh = normalize_events({
            "status": fh.get("status"),
            "earnings": fh.get("earnings", []),
            "ex_dividend_dates": [],
        }, symbol)
        dividend = dividends.get(symbol) or {"ex_dividend_dates": []}
        risk = risk_ctx.get(symbol) or {}
        out[symbol] = {
            "status": (
                "ok" if normalized_fh.get("status") == "ok" and dividend_ok
                else "unavailable"
            ),
            "earnings_dates": normalized_fh.get("earnings_dates") or [],
            "ex_dividend_dates": dividend.get("ex_dividend_dates") or [],
            "red_flags": risk.get("red_flags") or [],
            "sources": risk.get("sources") or [],
        }
    return out, flags


def collect_live_macro_events(start: str, end: str, args) -> tuple[dict[str, Any], list[str]]:
    """Fetch the shared US high-impact macro calendar or load an explicit file."""
    path = getattr(args, "macro_events_file", None)
    if path:
        import json
        from pathlib import Path

        try:
            payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
            raise ValueError(f"宏观事件文件不可用: {exc}") from exc
        return payload, []
    mode = getattr(args, "macro_events", "auto")
    if mode == "off":
        return {"status": "unavailable", "events": []}, ["宏观事件日历已关闭"]
    if mode == "auto" and not finnhub_has_api_key():
        return {"status": "unavailable", "events": []}, ["未配置 Finnhub，宏观事件日历不可用"]
    result = fetch_economic_calendar(start, end, timeout=args.finnhub_timeout)
    flags = [] if result.get("status") == "ok" else ["Finnhub 宏观事件日历不可用"]
    return result, flags


def expiration_window(as_of: dt.date, cfg: OptionsSellerConfig) -> tuple[str, str]:
    return (
        (as_of + dt.timedelta(days=cfg.dte_min)).isoformat(),
        (as_of + dt.timedelta(days=cfg.dte_max)).isoformat(),
    )


def fetch_chain_for_setup(
    symbol: str, setup: dict, cfg: OptionsSellerConfig, args, as_of_date: dt.date
) -> dict:
    lower = float((setup.get("bollinger") or {})["lower"])
    upper = float((setup.get("bollinger") or {})["upper"])
    spot = float(setup["last_close"])
    distance = cfg.max_short_distance_pct / 100.0
    exp_gte, exp_lte = expiration_window(as_of_date, cfg)
    return fetch_normalized_chain(
        symbol, exp_gte, exp_lte,
        feed=args.option_feed,
        strike_gte=max(0.01, min(lower - cfg.max_width, spot * (1 - distance) - cfg.max_width)),
        strike_lte=max(upper + cfg.max_width, spot * (1 + distance) + cfg.max_width),
        timeout=args.timeout,
    )


def fetch_chain_for_strategies(
    symbol: str,
    setup: dict,
    cfg: OptionsSellerConfig,
    args,
    as_of_date: dt.date,
    strategy_families: tuple[str, ...],
) -> dict:
    """Fetch one union chain window for all selected, eligible strategy modules."""
    bounds = [
        module.chain_bounds(setup, cfg, as_of_date)
        for module in strategy_modules(strategy_families)
        if module.screen_underlying(symbol, setup, cfg)
    ]
    if not bounds:
        return {"status": "unavailable", "symbol": symbol, "reason": "所选策略没有标的初筛形态"}
    return fetch_normalized_chain(
        symbol,
        min(row["expiration_gte"] for row in bounds).isoformat(),
        max(row["expiration_lte"] for row in bounds).isoformat(),
        feed=args.option_feed,
        strike_gte=min(float(row["strike_gte"]) for row in bounds),
        strike_lte=max(float(row["strike_lte"]) for row in bounds),
        timeout=args.timeout,
    )
