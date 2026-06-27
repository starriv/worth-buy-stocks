#!/usr/bin/env python3
"""Shared utility helpers for the worth-buy-stocks scripts.

Currently exposes ``to_num`` — a permissive ``float`` coercion that returns
``None`` for empty/None/non-numeric inputs instead of raising. Several modules
(``scoring``, ``finnhub``, ``portfolio``) previously each carried a private
``_num`` copy; this module centralises that single source of truth.

Module-specific rounding helpers (``_price``, ``_pct``, ``_round``,
``_round_num``, ``_pct_from_factor``) intentionally remain in their owning
modules because their rounding policies differ per caller.
"""
from __future__ import annotations

from typing import Any


def to_num(value: Any) -> float | None:
    """Permissively coerce ``value`` to ``float``; return ``None`` on failure.

    ``None`` and empty strings are treated as missing. Any non-numeric input
    that ``float()`` cannot parse returns ``None`` rather than raising, so
    callers can use this to safely normalise loosely-typed JSON fields.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
