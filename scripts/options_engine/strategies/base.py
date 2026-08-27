"""Shared contracts for independent option-screening strategy modules."""
from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

from ..config import OptionsSellerConfig


class StrategyModule(Protocol):
    """A strategy owns its setup gate, chain range, construction, and scoring."""

    family: str

    def screen_underlying(
        self, symbol: str, setup: dict[str, Any], cfg: OptionsSellerConfig
    ) -> list[dict[str, Any]]: ...

    def chain_bounds(
        self, setup: dict[str, Any], cfg: OptionsSellerConfig, as_of_date: dt.date
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...
