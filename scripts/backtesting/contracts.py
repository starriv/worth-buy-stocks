"""Versioned constants and small shared contracts for historical backtests."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any


CONTRACT_VERSION = "1.4"
ARTIFACT_VERSION = "1.4"
# Older artifacts stay loadable/renderable; results across versions must not
# be pooled into one statistic.
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1.0", "1.1", "1.2", "1.3", "1.4"})
SUPPORTED_ARTIFACT_VERSIONS = frozenset({"1.0", "1.1", "1.2", "1.3", "1.4"})
# 12-1 momentum (the largest alpha weight) needs 253 closes; anything less
# silently drops that factor for the first scored sessions and makes early
# scores incomparable with later ones.
WARMUP_SESSIONS = 260
# Extra SPY sessions loaded before the warmup window so a symbol with sporadic
# halts can still meet the per-symbol warmup requirement.
WARMUP_LOAD_BUFFER_SESSIONS = 30
DEFAULT_HORIZONS = (21, 63)
DEFAULT_COST_BPS = 10.0
PRIMARY_HORIZON = 63
MIN_INFERENCE_GROUP = 8
BOOTSTRAP_SEED = 42


@dataclass(frozen=True)
class BacktestConfig:
    """Semantic inputs that must be recorded in the canonical artifact."""

    symbol: str
    start: str
    end: str
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    cost_bps: float = DEFAULT_COST_BPS
    data_source: str = "cache-only"
    feed: str = "iex"
    adjustment: str = "split"
    warmup_sessions: int = WARMUP_SESSIONS
    signal_step: int = 1

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if not symbol or "," in symbol:
            raise ValueError("symbol must contain exactly one ticker")
        try:
            start = dt.date.fromisoformat(str(self.start))
            end = dt.date.fromisoformat(str(self.end))
        except (TypeError, ValueError) as exc:
            raise ValueError("start/end must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("start cannot be after end")
        if not self.horizons or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in self.horizons
        ):
            raise ValueError("horizons must contain positive integers")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons must not contain duplicates")
        horizons = tuple(sorted(self.horizons))
        if horizons != DEFAULT_HORIZONS:
            raise ValueError("contract 1.4 requires horizons 21,63")
        if (
            isinstance(self.cost_bps, bool)
            or not isinstance(self.cost_bps, (int, float))
            or not math.isfinite(float(self.cost_bps))
            or self.cost_bps < 0
        ):
            raise ValueError("cost_bps must be >= 0")
        data_source = str(self.data_source).strip().lower()
        feed = str(self.feed).strip().lower()
        adjustment = str(self.adjustment).strip().lower()
        if data_source not in {"cache-only", "auto", "input"}:
            raise ValueError("data_source must be cache-only, auto, or input")
        if feed != "iex" or adjustment != "split":
            raise ValueError("contract 1.4 requires feed=iex and adjustment=split")
        if (
            isinstance(self.warmup_sessions, bool)
            or not isinstance(self.warmup_sessions, int)
            or self.warmup_sessions < WARMUP_SESSIONS
        ):
            raise ValueError(f"warmup_sessions must be >= {WARMUP_SESSIONS}")
        if (
            isinstance(self.signal_step, bool)
            or not isinstance(self.signal_step, int)
            or self.signal_step < 1
        ):
            raise ValueError("signal_step must be >= 1")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "start", start.isoformat())
        object.__setattr__(self, "end", end.isoformat())
        object.__setattr__(self, "horizons", horizons)
        object.__setattr__(self, "data_source", data_source)
        object.__setattr__(self, "feed", feed)
        object.__setattr__(self, "adjustment", adjustment)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": self.start,
            "end": self.end,
            "horizons": list(self.horizons),
            "cost_bps_oneway": self.cost_bps,
            "data_source": self.data_source,
            "feed": self.feed,
            "adjustment": self.adjustment,
            "warmup_sessions": self.warmup_sessions,
            "signal_step": self.signal_step,
        }
