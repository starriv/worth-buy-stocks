#!/usr/bin/env python3
"""Modular defined-risk option strategy analyzer and two-stage scanner.

This command is read-only.  It never invokes Alpaca order, exercise, or position
mutation commands.  ``--input`` is a hard offline boundary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

from fetching import fetch_bars
from options_engine.config import OptionsSellerConfig
from options_engine.contracts import (
    CONTRACT_VERSION,
    RESULT_KIND,
    validate_offline_input,
    validate_result,
)
from options_engine.data import normalize_offline_chain
from options_engine.live import (
    collect_live_events,
    collect_live_macro_events,
    fetch_chain_for_strategies,
    fetch_options_assets,
    filter_options_assets,
)
from options_engine.iv_history import IVHistoryStore, constant_maturity_iv
from options_engine.portfolio_risk import option_underlyings
from options_engine.regime import evaluate_market_regime
from options_engine.service import VERDICT_ORDER, analyze_symbol, screen_underlyings
from options_engine.setup import evaluate_bollinger_setup
from options_engine.strategies import resolve_strategy_families
from options_engine.volatility_surface import build_volatility_surface
from portfolio import fetch_account_context, normalize_account_context
from scanner import fetch_liquidity, fetch_pool_bars
from skill_logging import (
    add_logging_arguments,
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)

logger = get_logger("options_seller")
HISTORY_DAYS = 420
DEFAULT_CHAIN_TOP = 25
DEFAULT_TOP = 20
_NEW_YORK = ZoneInfo("America/New_York")
_SCAN_ACCOUNT_CONTEXT = {"status": "ignored_for_scan"}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _market_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(_NEW_YORK)


def _market_date(value: str) -> dt.date:
    text = str(value).strip()
    if "T" not in text:
        return dt.date.fromisoformat(text[:10])
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(_NEW_YORK).date()


def _validate_live_end(value: str | None, today: dt.date) -> None:
    if value and dt.date.fromisoformat(str(value)[:10]) < today:
        raise ValueError("实时期权链不能与历史 --end 混用；历史复盘请使用 --input")


def _completed_daily_bars(rows: list[dict], market_now: dt.datetime) -> list[dict]:
    """Exclude an in-progress US daily bar before the regular close."""
    if market_now.weekday() >= 5 or market_now.time() >= dt.time(16, 5):
        return rows
    today = market_now.date().isoformat()
    return [row for row in rows if str(row.get("t") or "")[:10] < today]


def _load_json(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _symbols_arg(args) -> list[str]:
    values = []
    if getattr(args, "symbols", None):
        values.extend(str(args.symbols).split(","))
    if getattr(args, "symbols_file", None):
        values.extend(Path(args.symbols_file).read_text(encoding="utf-8").replace("\n", ",").split(","))
    clean = []
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in clean:
            clean.append(symbol)
    return clean


def _config(args) -> OptionsSellerConfig:
    return OptionsSellerConfig(
        boll_window=args.boll_window,
        boll_deviations=args.boll_deviations,
        boll_touch_lookback=args.boll_touch_lookback,
        dte_min=args.dte_min,
        dte_max=args.dte_max,
        short_delta_min=args.delta_min,
        short_delta_max=args.delta_max,
        cautious_short_delta_max=args.cautious_delta_max,
        min_open_interest=args.min_open_interest,
        suitable_min_open_interest=args.suitable_min_open_interest,
        max_quote_spread_pct=args.max_quote_spread_pct,
        suitable_max_quote_spread_pct=args.suitable_max_quote_spread_pct,
        max_quote_age_minutes=args.max_quote_age_minutes,
        min_credit_width_pct=args.min_credit_width_pct,
        min_width=args.min_width,
        max_width=args.max_width,
        dynamic_width_atr_min=args.dynamic_width_atr_min,
        dynamic_width_atr_max=args.dynamic_width_atr_max,
        min_expected_move_coverage=args.min_expected_move_coverage,
        cautious_min_expected_move_coverage=args.cautious_min_expected_move_coverage,
        iv_history_min_observations=args.iv_history_min_observations,
        macro_event_blackout_days=args.macro_event_blackout_days,
        min_underlying_price=args.min_price,
        min_underlying_volume=args.min_volume,
        min_underlying_dollar_volume=args.min_dollar_volume,
        max_risk_pct=args.max_risk_pct,
        max_symbol_option_risk_pct=args.max_symbol_option_risk_pct,
        max_correlated_option_risk_pct=args.max_correlated_option_risk_pct,
        max_expiration_option_risk_pct=args.max_expiration_option_risk_pct,
        max_total_option_risk_pct=args.max_total_option_risk_pct,
        correlation_threshold=args.correlation_threshold,
        round_trip_cost_per_spread=args.round_trip_cost,
        max_candidates_per_symbol=args.per_symbol,
        calendar_front_dte_min=args.calendar_front_dte_min,
        calendar_front_dte_max=args.calendar_front_dte_max,
        calendar_back_dte_min=args.calendar_back_dte_min,
        calendar_back_dte_max=args.calendar_back_dte_max,
        calendar_min_expiry_gap_days=args.calendar_min_expiry_gap_days,
        calendar_max_expiry_gap_days=args.calendar_max_expiry_gap_days,
        calendar_max_abs_net_delta=args.calendar_max_abs_net_delta,
        calendar_max_moneyness_pct=args.calendar_max_moneyness_pct,
        calendar_max_middle_slope_pct=args.calendar_max_middle_slope_pct,
        calendar_max_pct_b_deviation=args.calendar_max_pct_b_deviation,
        calendar_min_front_back_iv_edge=args.calendar_min_front_back_iv_edge,
        calendar_min_net_vega=args.calendar_min_net_vega,
        iron_condor_dte_min=args.iron_condor_dte_min,
        iron_condor_dte_max=args.iron_condor_dte_max,
        iron_condor_short_delta_min=args.iron_condor_short_delta_min,
        iron_condor_short_delta_max=args.iron_condor_short_delta_max,
        iron_condor_max_abs_net_delta=args.iron_condor_max_abs_net_delta,
        iron_condor_min_credit_width_pct=args.iron_condor_min_credit_width_pct,
        iron_condor_min_expected_move_coverage=args.iron_condor_min_expected_move_coverage,
        iron_condor_max_middle_slope_pct=args.iron_condor_max_middle_slope_pct,
        iron_condor_max_pct_b_deviation=args.iron_condor_max_pct_b_deviation,
        iron_butterfly_dte_min=args.iron_butterfly_dte_min,
        iron_butterfly_dte_max=args.iron_butterfly_dte_max,
        iron_butterfly_short_delta_min=args.iron_butterfly_short_delta_min,
        iron_butterfly_short_delta_max=args.iron_butterfly_short_delta_max,
        iron_butterfly_max_abs_net_delta=args.iron_butterfly_max_abs_net_delta,
        iron_butterfly_max_body_moneyness_pct=args.iron_butterfly_max_body_moneyness_pct,
        iron_butterfly_min_credit_width_pct=args.iron_butterfly_min_credit_width_pct,
        iron_butterfly_min_break_even_coverage=args.iron_butterfly_min_break_even_coverage,
        iron_butterfly_max_middle_slope_pct=args.iron_butterfly_max_middle_slope_pct,
        iron_butterfly_max_pct_b_deviation=args.iron_butterfly_max_pct_b_deviation,
    ).validate()


def _result(
    mode: str,
    cfg: OptionsSellerConfig,
    symbols: dict,
    *,
    as_of: str,
    counts: dict,
    flags: list[str],
    offline: bool,
    strategy_selection: str,
    account_context_policy: str = "account_aware",
) -> dict:
    payload = {
        "contract_version": CONTRACT_VERSION,
        "kind": RESULT_KIND,
        "status": "ok",
        "as_of": as_of,
        "mode": mode,
        "offline": offline,
        "strategy_selection": strategy_selection,
        "account_context_policy": account_context_policy,
        "config": cfg.to_dict(),
        "counts": counts,
        "symbols": symbols,
        "data_flags": flags,
    }
    validate_result(payload)
    return payload


def _account_context(args, offline_payload: dict | None = None) -> tuple[dict | None, list[str]]:
    if args.account_context_file:
        return normalize_account_context(_load_json(args.account_context_file)), []
    if offline_payload is not None:
        raw = offline_payload.get("account_context")
        return (normalize_account_context(raw), []) if raw is not None else (None, [])
    if args.account_context == "off":
        return None, []
    try:
        return fetch_account_context(timeout=args.timeout), []
    except Exception as exc:  # noqa: BLE001 - optional read-only overlay
        if args.account_context == "on":
            raise
        return None, [f"账户期权敞口不可用: {exc}"]


def _iv_context(
    symbol: str,
    chain: dict,
    setup: dict,
    as_of: str,
    args,
    *,
    offline_history: dict | None = None,
) -> tuple[dict | None, dict | None, list[str]]:
    if setup.get("status") != "ok" or chain.get("status") != "ok":
        return None, None, []
    surface = build_volatility_surface(
        chain, float(setup["last_close"]), as_of_date=_market_date(as_of)
    )
    if offline_history is not None:
        return surface, offline_history, []
    if args.input or args.iv_history == "off":
        return surface, None, []
    flags = []
    try:
        store = IVHistoryStore(args.iv_history_db)
        constant = constant_maturity_iv(surface)
        session = _market_date(str(surface.get("as_of") or as_of)).isoformat()
        history = (
            store.history_context(
                symbol, float(constant["iv"]), current_session=session,
                min_observations=args.iv_history_min_observations,
            )
            if constant.get("status") == "ok" else None
        )
        store.record(symbol, surface, feed=str(chain.get("feed") or "unknown"))
        return surface, history, flags
    except Exception as exc:  # noqa: BLE001 - history is a degradable overlay
        if args.iv_history == "on":
            raise
        flags.append(f"IV 历史快照不可用: {exc}")
        return surface, None, flags


def analyze(args) -> dict:
    cfg = _config(args)
    strategy_families = resolve_strategy_families(args.strategy)
    symbol = args.symbol.strip().upper()
    if args.input:
        data = _load_json(args.input)
        validate_offline_input(data, required_symbols=[symbol])
        as_of = str(data.get("as_of") or _utc_now())
        bars = (data.get("bars") or {}).get(symbol) or []
        market_regime = evaluate_market_regime(data.get("bars") or {})
        chain = normalize_offline_chain(symbol, (data.get("option_chains") or {}).get(symbol))
        events = (data.get("events") or {}).get(symbol) or {"status": "unavailable"}
        macro_events = data.get("macro_events")
        account_context, account_flags = _account_context(args, data)
        offline = True
    else:
        market_now = _market_now()
        today = market_now.date()
        _validate_live_end(args.end, today)
        as_of = _utc_now()
        account_context, account_flags = _account_context(args)
        start = args.start or (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        end = args.end or today.isoformat()
        bars_map, _used_feed, note = fetch_bars(
            list(dict.fromkeys([symbol, "SPY", "QQQ", *option_underlyings(account_context)])),
            start, end, args.feed, args.adjustment, 10000, args.timeout,
            cache_mode=args.cache, cache_db=args.cache_db,
        )
        bars_map = {
            key: _completed_daily_bars(rows, market_now)
            for key, rows in bars_map.items()
        }
        bars = bars_map.get(symbol) or []
        market_regime = evaluate_market_regime(bars_map)
        setup = evaluate_bollinger_setup(bars, cfg)
        eligible = screen_underlyings({symbol: bars}, cfg, strategy_families)
        if setup.get("status") == "ok" and eligible:
            chain = fetch_chain_for_strategies(
                symbol, setup, cfg, args, today, strategy_families
            )
            horizon = max(
                cfg.dte_max if "vertical_credit" in strategy_families else 0,
                cfg.calendar_back_dte_max if "calendar_iv" in strategy_families else 0,
                cfg.iron_condor_dte_max if "iron_condor" in strategy_families else 0,
                cfg.iron_butterfly_dte_max if "iron_butterfly" in strategy_families else 0,
            )
            events_map, event_flags = collect_live_events(
                [symbol],
                today.isoformat(),
                (today + dt.timedelta(days=horizon)).isoformat(),
                args,
                horizon_days=horizon,
            )
            events = events_map.get(symbol) or {"status": "unavailable"}
            chain.setdefault("data_flags", []).extend(event_flags)
            macro_events, macro_flags = collect_live_macro_events(
                today.isoformat(),
                (today + dt.timedelta(days=cfg.macro_event_blackout_days + 7)).isoformat(),
                args,
            )
            chain.setdefault("data_flags", []).extend(macro_flags)
        else:
            chain = {"status": "unavailable", "reason": "当前没有符合所选策略的标的初筛形态"}
            events = {"status": "unavailable"}
            macro_events = None
        if note:
            chain.setdefault("data_flags", []).append(str(note))
        offline = False
    setup = evaluate_bollinger_setup(bars, cfg)
    surface, history, history_flags = _iv_context(
        symbol, chain, setup, as_of, args,
        offline_history=((data.get("iv_history") or {}).get(symbol) if args.input else None),
    )
    row = analyze_symbol(
        symbol, bars, chain, events, cfg, as_of=as_of,
        account_equity=args.account_equity, market_regime=market_regime,
        volatility_surface=surface, iv_history_context=history,
        account_context=account_context, bars_by_symbol=bars_map if not offline else data.get("bars"),
        macro_events=macro_events,
        strategy_families=strategy_families,
    )
    return _result(
        "analyze", cfg, {symbol: row}, as_of=as_of,
        counts={"symbols": 1, "candidates": len(row.get("candidates") or [])},
        flags=[*account_flags, *history_flags], offline=offline,
        strategy_selection=args.strategy,
        account_context_policy="account_aware",
    )


def _offline_scan_pool(data: dict, requested: list[str]) -> dict[str, list[dict]]:
    bars = data.get("bars") or {}
    if requested:
        return {symbol: bars.get(symbol) or [] for symbol in requested}
    return {str(symbol).upper(): rows for symbol, rows in bars.items()}


def _select_chain_symbols(
    setups: list[dict], chain_top: int, strategy_families: tuple[str, ...]
) -> list[str]:
    """Allocate second-stage chain slots fairly across selected strategy modules."""
    queues: dict[str, list[str]] = {family: [] for family in strategy_families}
    for row in setups:
        family = str(row.get("strategy_family") or "")
        symbol = str(row.get("symbol") or "")
        if family in queues and symbol and symbol not in queues[family]:
            queues[family].append(symbol)
    selected: list[str] = []
    while len(selected) < chain_top:
        progressed = False
        for family in strategy_families:
            while queues[family] and queues[family][0] in selected:
                queues[family].pop(0)
            if queues[family]:
                selected.append(queues[family].pop(0))
                progressed = True
                if len(selected) >= chain_top:
                    break
        if not progressed:
            break
    return selected


def scan(args) -> dict:
    cfg = _config(args)
    strategy_families = resolve_strategy_families(args.strategy)
    flags: list[str] = []
    requested = _symbols_arg(args)
    if args.input:
        data = _load_json(args.input)
        validate_offline_input(data, validate_account_context=False)
        as_of = str(data.get("as_of") or _utc_now())
        as_of_date = _market_date(as_of)
        bars_by_symbol = _offline_scan_pool(data, requested)
        market_regime = evaluate_market_regime(data.get("bars") or {})
        account_context = _SCAN_ACCOUNT_CONTEXT
        macro_events = data.get("macro_events")
        portfolio_bars = data.get("bars") or {}
        universe_count = len(bars_by_symbol)
        offline = True
    else:
        data = {}
        market_now = _market_now()
        as_of = _utc_now()
        as_of_date = market_now.date()
        _validate_live_end(args.end, as_of_date)
        account_context = _SCAN_ACCOUNT_CONTEXT
        if requested:
            universe = requested
        else:
            exchanges = tuple(value.strip().upper() for value in args.exchange.split(",") if value.strip())
            universe = filter_options_assets(fetch_options_assets(args.timeout), exchanges)
        universe_count = len(universe)
        pool, snapshot_flags = fetch_liquidity(
            universe, args.feed, cfg.min_underlying_price, cfg.min_underlying_volume,
            chunk=args.snapshot_chunk, timeout=args.timeout,
            adjustment=args.adjustment, cache_mode=args.cache, cache_db=args.cache_db,
        )
        flags.extend(snapshot_flags)
        liquid_symbols = [row["symbol"] for row in pool]
        start = args.start or (as_of_date - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        end = args.end or as_of_date.isoformat()
        portfolio_bars, bar_flags = fetch_pool_bars(
            liquid_symbols,
            start, end, args.feed, args.adjustment,
            chunk=args.bars_chunk, timeout=args.timeout,
            cache_mode=args.cache, cache_db=args.cache_db,
        )
        flags.extend(bar_flags)
        portfolio_bars = {
            symbol: _completed_daily_bars(rows, market_now)
            for symbol, rows in portfolio_bars.items()
        }
        market_regime = evaluate_market_regime(portfolio_bars)
        macro_events = None
        # Benchmarks are added by fetch_pool_bars for stock scoring; they are not
        # option-seller universe members unless explicitly requested.
        bars_by_symbol = {
            symbol: rows for symbol, rows in portfolio_bars.items()
            if symbol in set(liquid_symbols)
        }
        offline = False

    setups = screen_underlyings(bars_by_symbol, cfg, strategy_families)
    selected_symbols = _select_chain_symbols(
        setups, args.chain_top, strategy_families
    )

    if offline and selected_symbols:
        validate_offline_input(
            data,
            required_symbols=selected_symbols,
            validate_account_context=False,
        )

    chains: dict[str, dict] = {}
    events_by_symbol: dict[str, dict] = {}
    if offline:
        for symbol in selected_symbols:
            chains[symbol] = normalize_offline_chain(symbol, (data.get("option_chains") or {}).get(symbol))
            events_by_symbol[symbol] = (data.get("events") or {}).get(symbol) or {"status": "unavailable"}
    elif selected_symbols:
        def fetch_one(symbol: str) -> tuple[str, dict]:
            setup = evaluate_bollinger_setup(bars_by_symbol[symbol], cfg)
            try:
                return symbol, fetch_chain_for_strategies(
                    symbol, setup, cfg, args, as_of_date, strategy_families
                )
            except Exception as exc:  # noqa: BLE001 - one chain must not abort scan
                return symbol, {"status": "unavailable", "symbol": symbol, "reason": str(exc)}

        workers = max(1, min(args.workers, len(selected_symbols)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = [pool.submit(fetch_one, symbol) for symbol in selected_symbols]
            chain_rows = [future.result() for future in fetched]
        chains = {symbol: chain for symbol, chain in chain_rows}
        horizon = max(
            cfg.dte_max if "vertical_credit" in strategy_families else 0,
            cfg.calendar_back_dte_max if "calendar_iv" in strategy_families else 0,
            cfg.iron_condor_dte_max if "iron_condor" in strategy_families else 0,
            cfg.iron_butterfly_dte_max if "iron_butterfly" in strategy_families else 0,
        )
        events_by_symbol, event_flags = collect_live_events(
            selected_symbols,
            as_of_date.isoformat(),
            (as_of_date + dt.timedelta(days=horizon)).isoformat(),
            args,
            horizon_days=horizon,
        )
        flags.extend(event_flags)
        macro_events, macro_flags = collect_live_macro_events(
            as_of_date.isoformat(),
            (as_of_date + dt.timedelta(days=cfg.macro_event_blackout_days + 7)).isoformat(),
            args,
        )
        flags.extend(macro_flags)

    symbol_results = {}
    for symbol in selected_symbols:
        setup = evaluate_bollinger_setup(bars_by_symbol.get(symbol) or [], cfg)
        surface, history, history_flags = _iv_context(
            symbol, chains.get(symbol) or {}, setup, as_of, args,
            offline_history=((data.get("iv_history") or {}).get(symbol) if offline else None),
        )
        flags.extend(history_flags)
        symbol_results[symbol] = analyze_symbol(
            symbol,
            bars_by_symbol.get(symbol) or [],
            chains.get(symbol),
            events_by_symbol.get(symbol),
            cfg,
            as_of=as_of,
            account_equity=None,
            market_regime=market_regime,
            volatility_surface=surface,
            iv_history_context=history,
            account_context=account_context,
            bars_by_symbol=portfolio_bars,
            macro_events=macro_events,
            strategy_families=strategy_families,
        )

    # Rank whole symbols by their best spread, while keeping per-symbol details.
    def best_rank(symbol: str) -> tuple[int, int, str]:
        candidates = symbol_results[symbol].get("candidates") or []
        if not candidates:
            return 9, 0, symbol
        score = candidates[0].get("score") or {}
        return (
            VERDICT_ORDER.get(score.get("verdict"), 9),
            -int(score.get("seller_score") or 0),
            symbol,
        )

    ordered = sorted(symbol_results, key=best_rank)[:args.top]
    symbol_results = {symbol: symbol_results[symbol] for symbol in ordered}
    candidate_count = sum(len(row.get("candidates") or []) for row in symbol_results.values())
    return _result(
        "scan", cfg, symbol_results, as_of=as_of,
        counts={
            "universe": universe_count,
            "underlyings_with_setup": len({row["symbol"] for row in setups}),
            "chains_requested": len(selected_symbols),
            "symbols_returned": len(symbol_results),
            "candidates": candidate_count,
        },
        flags=flags, offline=offline,
        strategy_selection=args.strategy,
        account_context_policy="ignored_for_scan",
    )


def _add_shared(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--strategy",
        choices=(
            "vertical_credit", "calendar_iv", "iron_condor", "iron_butterfly", "all"
        ),
        default="vertical_credit",
        help="选择垂直信用价差、Delta 近零 IV 日历、Iron Condor、Iron Butterfly 或全部",
    )
    sub.add_argument("--input", help="离线 JSON；启用后不触网、不读写缓存")
    sub.add_argument("--feed", default="iex", help="标的日线 feed")
    sub.add_argument("--option-feed", choices=("auto", "opra", "indicative"), default="auto")
    sub.add_argument("--adjustment", default="split")
    sub.add_argument("--start")
    sub.add_argument("--end")
    sub.add_argument("--timeout", type=int, default=30)
    sub.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    sub.add_argument("--cache-db")
    sub.add_argument("--iv-history", choices=("auto", "on", "off"), default="auto")
    sub.add_argument("--iv-history-db")
    sub.add_argument("--iv-history-min-observations", type=int, default=60)
    sub.add_argument("--events", choices=("auto", "on", "off"), default="auto")
    sub.add_argument("--macro-events", choices=("auto", "on", "off"), default="auto")
    sub.add_argument("--macro-events-file")
    sub.add_argument("--macro-event-blackout-days", type=int, default=1)
    sub.add_argument("--finnhub-timeout", type=int, default=15)
    sub.add_argument(
        "--account-equity", type=float,
        help="仅用于单标的 analyze 的账户风险预算；scan 始终忽略",
    )
    sub.add_argument(
        "--account-context", choices=("auto", "on", "off"), default="auto",
        help="仅用于单标的 analyze 的只读账户 overlay；scan 始终忽略",
    )
    sub.add_argument(
        "--account-context-file",
        help="仅用于单标的 analyze 的离线账户 overlay；scan 始终忽略",
    )
    sub.add_argument("--max-risk-pct", type=float, default=0.5)
    sub.add_argument("--max-symbol-option-risk-pct", type=float, default=2.0)
    sub.add_argument("--max-correlated-option-risk-pct", type=float, default=4.0)
    sub.add_argument("--max-expiration-option-risk-pct", type=float, default=2.0)
    sub.add_argument("--max-total-option-risk-pct", type=float, default=6.0)
    sub.add_argument("--correlation-threshold", type=float, default=0.70)
    sub.add_argument(
        "--round-trip-cost",
        type=float,
        help="每组价差完整开平仓的自定义费用估计；省略时不估算净收益",
    )
    sub.add_argument("--boll-window", type=int, default=20)
    sub.add_argument("--boll-deviations", type=float, default=2.0)
    sub.add_argument("--boll-touch-lookback", type=int, default=3)
    sub.add_argument("--dte-min", type=int, default=30)
    sub.add_argument("--dte-max", type=int, default=45)
    sub.add_argument("--delta-min", type=float, default=0.15)
    sub.add_argument("--delta-max", type=float, default=0.25)
    sub.add_argument("--cautious-delta-max", type=float, default=0.20)
    sub.add_argument("--min-open-interest", type=int, default=100)
    sub.add_argument("--suitable-min-open-interest", type=int, default=500)
    sub.add_argument("--max-quote-spread-pct", type=float, default=20.0)
    sub.add_argument("--suitable-max-quote-spread-pct", type=float, default=12.0)
    sub.add_argument("--max-quote-age-minutes", type=int, default=15)
    sub.add_argument("--min-credit-width-pct", type=float, default=20.0)
    sub.add_argument("--min-width", type=float, default=1.0)
    sub.add_argument("--max-width", type=float, default=10.0)
    sub.add_argument("--dynamic-width-atr-min", type=float, default=0.25)
    sub.add_argument("--dynamic-width-atr-max", type=float, default=1.50)
    sub.add_argument("--min-expected-move-coverage", type=float, default=0.90)
    sub.add_argument("--cautious-min-expected-move-coverage", type=float, default=1.00)
    sub.add_argument("--min-price", type=float, default=5.0)
    sub.add_argument("--min-volume", type=float, default=50_000.0)
    sub.add_argument("--min-dollar-volume", type=float, default=5_000_000.0)
    sub.add_argument("--per-symbol", type=int, default=10)
    sub.add_argument("--calendar-front-dte-min", type=int, default=14)
    sub.add_argument("--calendar-front-dte-max", type=int, default=35)
    sub.add_argument("--calendar-back-dte-min", type=int, default=45)
    sub.add_argument("--calendar-back-dte-max", type=int, default=90)
    sub.add_argument("--calendar-min-expiry-gap-days", type=int, default=21)
    sub.add_argument("--calendar-max-expiry-gap-days", type=int, default=75)
    sub.add_argument("--calendar-max-abs-net-delta", type=float, default=0.10)
    sub.add_argument("--calendar-max-moneyness-pct", type=float, default=5.0)
    sub.add_argument("--calendar-max-middle-slope-pct", type=float, default=1.0)
    sub.add_argument("--calendar-max-pct-b-deviation", type=float, default=0.35)
    sub.add_argument("--calendar-min-front-back-iv-edge", type=float, default=-0.02)
    sub.add_argument("--calendar-min-net-vega", type=float, default=0.0)
    sub.add_argument("--iron-condor-dte-min", type=int, default=21)
    sub.add_argument("--iron-condor-dte-max", type=int, default=45)
    sub.add_argument("--iron-condor-short-delta-min", type=float, default=0.10)
    sub.add_argument("--iron-condor-short-delta-max", type=float, default=0.25)
    sub.add_argument("--iron-condor-max-abs-net-delta", type=float, default=0.10)
    sub.add_argument("--iron-condor-min-credit-width-pct", type=float, default=20.0)
    sub.add_argument("--iron-condor-min-expected-move-coverage", type=float, default=0.70)
    sub.add_argument("--iron-condor-max-middle-slope-pct", type=float, default=0.75)
    sub.add_argument("--iron-condor-max-pct-b-deviation", type=float, default=0.35)
    sub.add_argument("--iron-butterfly-dte-min", type=int, default=21)
    sub.add_argument("--iron-butterfly-dte-max", type=int, default=45)
    sub.add_argument("--iron-butterfly-short-delta-min", type=float, default=0.35)
    sub.add_argument("--iron-butterfly-short-delta-max", type=float, default=0.65)
    sub.add_argument("--iron-butterfly-max-abs-net-delta", type=float, default=0.10)
    sub.add_argument("--iron-butterfly-max-body-moneyness-pct", type=float, default=3.0)
    sub.add_argument("--iron-butterfly-min-credit-width-pct", type=float, default=50.0)
    sub.add_argument("--iron-butterfly-min-break-even-coverage", type=float, default=0.20)
    sub.add_argument("--iron-butterfly-max-middle-slope-pct", type=float, default=0.50)
    sub.add_argument("--iron-butterfly-max-pct-b-deviation", type=float, default=0.25)


def _parser(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模块化定义风险期权策略筛选")
    add_logging_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser("analyze", help="按所选策略分析单一标的")
    _add_shared(single)
    single.add_argument("--symbol", required=True)
    market = commands.add_parser("scan", help="两阶段扫描期权卖方标的")
    _add_shared(market)
    market.add_argument("--symbols", help="逗号分隔的覆盖股池")
    market.add_argument("--symbols-file")
    market.add_argument("--exchange", default="AMEX,ARCA,BATS,NASDAQ,NYSE,NYSEARCA")
    market.add_argument("--snapshot-chunk", type=int, default=200)
    market.add_argument("--bars-chunk", type=int, default=80)
    market.add_argument("--workers", type=int, default=4)
    market.add_argument("--chain-top", type=int, default=DEFAULT_CHAIN_TOP)
    market.add_argument("--top", type=int, default=DEFAULT_TOP)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parser(argv)
    configure_logging(level=args.log_level, log_format=args.log_format)
    clear_log_context()
    bind_log_context(command=f"options_seller_{args.command}")
    payload = analyze(args) if args.command == "analyze" else scan(args)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
        logger.exception("options_seller_failed", error_type=type(exc).__name__, reason=str(exc))
        json.dump({"error": str(exc)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        raise SystemExit(1)
