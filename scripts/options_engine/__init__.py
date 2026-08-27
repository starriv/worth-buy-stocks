"""Deterministic defined-risk option seller screening primitives."""

from .config import OptionsSellerConfig
from .service import analyze_symbol, screen_underlyings

__all__ = ["OptionsSellerConfig", "analyze_symbol", "screen_underlyings"]
