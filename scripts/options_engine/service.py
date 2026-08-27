"""Pure orchestration across independently registered option strategies."""
from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from .config import OptionsSellerConfig
from .events import normalize_events, normalize_macro_events
from .iv_history import attach_history
from .setup import evaluate_bollinger_setup
from .strategies import strategy_modules
from .volatility_surface import build_volatility_surface

VERDICT_ORDER = {"适合构建": 0, "观察等待": 1, "回避": 2, "数据不足": 3}
_NEW_YORK = ZoneInfo("America/New_York")


def _date(value: Any, fallback: str | None = None) -> dt.date:
    text = str(value or fallback or dt.date.today().isoformat()).strip()
    if "T" not in text:
        return dt.date.fromisoformat(text[:10])
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(_NEW_YORK).date()


def screen_underlyings(
    bars_by_symbol: dict[str, list[dict]],
    cfg: OptionsSellerConfig,
    strategy_families: tuple[str, ...] = ("vertical_credit",),
) -> list[dict[str, Any]]:
    """Rank symbols through each selected strategy's independent setup gate."""
    rows = []
    modules = strategy_modules(strategy_families)
    for raw_symbol in sorted(bars_by_symbol or {}):
        symbol = str(raw_symbol).strip().upper()
        setup = evaluate_bollinger_setup(bars_by_symbol[raw_symbol], cfg)
        for module in modules:
            rows.extend(module.screen_underlying(symbol, setup, cfg))
    rows.sort(key=lambda row: (
        -row["underlying_score"], row["symbol"], row["strategy_family"], row["strategy"]
    ))
    return rows


def analyze_symbol(
    symbol: str,
    bars: list[dict],
    chain: dict[str, Any] | None,
    events: Any,
    cfg: OptionsSellerConfig,
    *,
    as_of: str | None = None,
    account_equity: float | None = None,
    market_regime: dict[str, Any] | None = None,
    volatility_surface: dict[str, Any] | None = None,
    iv_history_context: dict[str, Any] | None = None,
    account_context: dict[str, Any] | None = None,
    bars_by_symbol: dict[str, list[dict]] | None = None,
    macro_events: dict[str, Any] | None = None,
    strategy_families: tuple[str, ...] = ("vertical_credit",),
) -> dict[str, Any]:
    """Return one canonical, deterministic symbol analysis."""
    cfg.validate()
    sym = str(symbol).strip().upper()
    setup = evaluate_bollinger_setup(bars, cfg)
    if setup.get("status") != "ok":
        return {
            "underlying": setup,
            "strategies": {},
            "candidates": [],
            "data_flags": [setup.get("reason") or "标的日线数据不足"],
        }
    event_ctx = normalize_events(events, sym, as_of_date=setup.get("last_date"))
    if macro_events is not None:
        event_ctx["macro_events"] = normalize_macro_events(macro_events)
    effective_equity = account_equity
    if effective_equity is None:
        account = (account_context or {}).get("account") or {}
        effective_equity = account.get("equity") or account.get("portfolio_value")
    strategies = setup.get("signals") or {}
    modules = strategy_modules(strategy_families)
    module_setups = {
        module.family: module.screen_underlying(sym, setup, cfg)
        for module in modules
    }
    eligible_modules = [module for module in modules if module_setups[module.family]]
    if not eligible_modules:
        no_setup_reason = (
            "当前没有布林线卖方触发形态"
            if strategy_families == ("vertical_credit",)
            else "当前没有符合所选策略的标的初筛形态"
        )
        return {
            "underlying": setup,
            "strategies": strategies,
            "strategy_modules": module_setups,
            "events": event_ctx,
            "candidates": [],
            "data_flags": [no_setup_reason],
        }
    if not isinstance(chain, dict) or chain.get("status") != "ok":
        return {
            "underlying": setup,
            "strategies": strategies,
            "strategy_modules": module_setups,
            "events": event_ctx,
            "candidates": [],
            "data_flags": [
                (chain or {}).get("reason") if isinstance(chain, dict) else "期权链不可用"
            ],
        }
    as_of_date = _date(as_of or chain.get("as_of"), setup.get("last_date"))
    surface = volatility_surface or build_volatility_surface(
        chain, float(setup["last_close"]), as_of_date=as_of_date
    )
    surface = attach_history(surface, iv_history_context)
    ranked: list[dict[str, Any]] = []
    spread_diagnostics: dict[str, dict[str, Any]] = {}
    recommendation_profiles: dict[str, Any] = {}
    module_results: dict[str, Any] = {
        module.family: {
            "status": "eligible" if module_setups[module.family] else "no_setup",
            "setup": module_setups[module.family],
            "candidates_built": 0,
        }
        for module in modules
    }
    for module in eligible_modules:
        result = module.analyze(
            sym,
            setup,
            chain,
            event_ctx,
            cfg,
            as_of_date=as_of_date,
            as_of=as_of or chain.get("as_of"),
            option_feed=str(chain.get("feed") or "unknown"),
            market_regime=market_regime,
            account_equity=effective_equity,
            account_context=account_context,
            bars_by_symbol=bars_by_symbol,
            volatility_surface=surface,
        )
        ranked.extend(result.get("candidates") or [])
        spread_diagnostics.update(result.get("diagnostics") or {})
        recommendation_profiles[module.family] = result.get("profiles") or {}
        module_results[module.family] = {
            "status": "eligible",
            "setup": module_setups[module.family],
            "candidates_built": len(result.get("candidates") or []),
        }
    ranked.sort(key=lambda row: (
        VERDICT_ORDER.get((row.get("score") or {}).get("verdict"), 9),
        -int((row.get("score") or {}).get("seller_score") or 0),
        row.get("expiration_date") or "",
        row.get("short_leg", {}).get("strike") or 0,
        row.get("width") or 0,
    ))
    data_flags = list(chain.get("data_flags") or [])
    data_flags.extend(surface.get("data_flags") or [])
    if not ranked:
        rejected = {
            reason: sum(
                int((row.get("rejections") or {}).get(reason) or 0)
                for row in spread_diagnostics.values()
            )
            for reason in sorted({
                reason
                for row in spread_diagnostics.values()
                for reason in (row.get("rejections") or {})
            })
        }
        if rejected:
            data_flags.extend(
                f"spread_rejected:{reason}={count}"
                for reason, count in rejected.items()
                if count
            )
        else:
            data_flags.append("spread_rejected:no_matching_option_contracts")
    return {
        "underlying": setup,
        "strategies": strategies,
        "strategy_modules": module_results,
        "events": event_ctx,
        "macro_events": event_ctx.get("macro_events"),
        "option_chain": {
            "status": chain.get("status"),
            "feed": chain.get("feed"),
            "as_of": chain.get("as_of"),
            "contracts_count": len(chain.get("contracts") or []),
        },
        "volatility_surface": surface,
        "market_regime": market_regime or {
            "status": "unavailable", "state": "unknown", "risk_multiplier": 0.0,
        },
        "candidates": ranked[:cfg.max_candidates_per_symbol],
        "recommendation_profiles": recommendation_profiles,
        "spread_diagnostics": spread_diagnostics,
        "data_flags": data_flags,
    }
