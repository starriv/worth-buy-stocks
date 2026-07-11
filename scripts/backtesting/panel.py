"""Cross-sectional historical panel and cache-backed loader."""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

from fetching import fetch_bars
from pipeline import build_result
from skill_logging import get_logger

from .universe import BANKRUPT, BENCH, HISTORY_DAYS, UNIVERSE


logger = get_logger("backtesting.panel")


class Panel:
    """Once-loaded read-only panel used by cross-sectional research."""

    def __init__(
        self,
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        used_feed: str,
        adjustment: str,
    ) -> None:
        self.bars = {symbol: list(rows) for symbol, rows in bars.items()}
        self.used_feed = used_feed
        self.adjustment = adjustment
        self.syms = list(dict.fromkeys(UNIVERSE + BENCH + list(bars)))
        self.closes_by_date = {
            symbol: {str(bar["t"])[:10]: bar["c"] for bar in self.bars.get(symbol, [])}
            for symbol in self.syms
        }
        self.dates_by_symbol = {
            symbol: sorted(self.closes_by_date.get(symbol, {}))
            for symbol in self.syms
        }
        self.calendar = [str(bar["t"])[:10] for bar in self.bars.get("SPY", [])]

    def close(self, symbol: str, date: str):
        return self.closes_by_date.get(symbol, {}).get(date)

    def close_on_or_before(
        self, symbol: str, date: str, after_date: str | None = None
    ):
        for candidate in reversed(self.dates_by_symbol.get(symbol, [])):
            if candidate <= date and (after_date is None or candidate > after_date):
                return self.closes_by_date.get(symbol, {}).get(candidate)
        return None

    def fwd_return_pct(self, symbol: str, time_index: int, horizon: int):
        if time_index + horizon >= len(self.calendar):
            return None
        entry_date = self.calendar[time_index]
        target_date = self.calendar[time_index + horizon]
        entry = self.close(symbol, entry_date)
        target = self.close(symbol, target_date)
        if target is None:
            dates = self.dates_by_symbol.get(symbol, [])
            if symbol in BANKRUPT and dates and dates[-1] < target_date:
                return -100.0 if entry else None
            target = self.close_on_or_before(symbol, target_date, after_date=entry_date)
        return (target / entry - 1) * 100 if (entry and target) else None

    def scores_at(self, date: str):
        sliced = {
            symbol: [bar for bar in self.bars.get(symbol, []) if str(bar["t"])[:10] <= date]
            for symbol in self.syms
        }
        return build_result(
            self.syms, sliced, self.used_feed, self.adjustment
        )["symbols"]


def load_panel(
    feed: str,
    adjustment: str,
    timeout: int,
    history_days: int = HISTORY_DAYS,
    *,
    cache_mode: str = "auto",
    cache_db: str | None = None,
) -> Panel:
    today = dt.date.today()
    end = today.isoformat()
    start = (today - dt.timedelta(days=history_days)).isoformat()
    symbols = UNIVERSE + BENCH
    logger.info(
        "backtest_history_fetch_started",
        symbols_count=len(symbols),
        start=start,
        end=end,
        feed=feed,
    )
    bars, used_feed, note = fetch_bars(
        symbols,
        start,
        end,
        feed,
        adjustment,
        10000,
        timeout,
        cache_mode=cache_mode,
        cache_db=cache_db,
    )
    if note:
        logger.info(
            "market_data_note",
            operation="backtest_history_fetch",
            reason=note,
        )
    return Panel(bars, used_feed, adjustment)


__all__ = ["Panel", "load_panel"]
