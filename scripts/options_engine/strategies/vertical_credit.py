"""Bollinger-triggered bull-put and bear-call credit-spread module."""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import OptionsSellerConfig
from ..portfolio_risk import evaluate_portfolio_risk
from ..scoring import score_spread
from ..spreads import build_vertical_spreads
from ..structure_optimizer import assign_profiles
from ..volatility_surface import enrich_spread_with_surface


class VerticalCreditStrategy:
    family = "vertical_credit"

    def screen_underlying(
        self, symbol: str, setup: dict[str, Any], cfg: OptionsSellerConfig
    ) -> list[dict[str, Any]]:
        if setup.get("status") != "ok" or not (setup.get("liquidity") or {}).get("eligible"):
            return []
        rows = []
        for strategy, signal in (setup.get("signals") or {}).items():
            if not signal.get("eligible"):
                continue
            rows.append({
                "symbol": symbol,
                "strategy_family": self.family,
                "strategy": strategy,
                "underlying_score": int(signal.get("boll_score") or 0)
                + int(signal.get("trend_score") or 0),
                "last_close": setup.get("last_close"),
                "last_date": setup.get("last_date"),
                "classification": signal.get("classification"),
                "band_anchor": signal.get("band_anchor"),
                "bollinger": setup.get("bollinger"),
            })
        return rows

    def chain_bounds(
        self, setup: dict[str, Any], cfg: OptionsSellerConfig, as_of_date: dt.date
    ) -> dict[str, Any]:
        boll = setup.get("bollinger") or {}
        spot = float(setup["last_close"])
        distance = cfg.max_short_distance_pct / 100.0
        return {
            "expiration_gte": as_of_date + dt.timedelta(days=cfg.dte_min),
            "expiration_lte": as_of_date + dt.timedelta(days=cfg.dte_max),
            "strike_gte": max(
                0.01,
                min(float(boll["lower"]) - cfg.max_width, spot * (1 - distance) - cfg.max_width),
            ),
            "strike_lte": max(
                float(boll["upper"]) + cfg.max_width, spot * (1 + distance) + cfg.max_width
            ),
        }

    def analyze(
        self,
        symbol: str,
        setup: dict[str, Any],
        chain: dict[str, Any],
        events: dict[str, Any],
        cfg: OptionsSellerConfig,
        *,
        as_of_date: dt.date,
        as_of: str | None,
        option_feed: str,
        market_regime: dict[str, Any] | None,
        account_equity: float | None,
        account_context: dict[str, Any] | None,
        bars_by_symbol: dict[str, list[dict]] | None,
        volatility_surface: dict[str, Any] | None,
    ) -> dict[str, Any]:
        ranked = []
        diagnostics: dict[str, dict[str, Any]] = {}
        surface = volatility_surface or {}
        for strategy, signal in (setup.get("signals") or {}).items():
            if not signal.get("eligible"):
                continue
            strategy_diagnostics: dict[str, Any] = {}
            spreads = build_vertical_spreads(
                symbol,
                strategy,
                chain,
                setup,
                cfg,
                as_of_date=as_of_date,
                diagnostics=strategy_diagnostics,
            )
            diagnostics[strategy] = strategy_diagnostics
            for spread in spreads:
                spread["strategy_family"] = self.family
                spread = enrich_spread_with_surface(spread, surface, setup)
                spread["portfolio_risk"] = evaluate_portfolio_risk(
                    spread, account_context, bars_by_symbol, cfg, market_regime
                )
                spread["score"] = score_spread(
                    spread,
                    setup,
                    cfg,
                    option_feed=option_feed,
                    events=events,
                    account_equity=account_equity,
                    as_of=as_of,
                    market_regime=market_regime,
                    portfolio_risk=spread["portfolio_risk"],
                )
                spread["score"]["strategy_score"] = spread["score"]["seller_score"]
                ranked.append(spread)
        ranked, profiles = assign_profiles(ranked)
        return {"candidates": ranked, "diagnostics": diagnostics, "profiles": profiles}
