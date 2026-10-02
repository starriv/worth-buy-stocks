"""Stable strategy registry used by both the CLI and pure service layer."""
from __future__ import annotations

from .calendar_iv import CalendarIVStrategy
from .iron_butterfly import IronButterflyStrategy
from .iron_condor import IronCondorStrategy
from .vertical_credit import VerticalCreditStrategy

_MODULES = {
    "vertical_credit": VerticalCreditStrategy(),
    "calendar_iv": CalendarIVStrategy(),
    "iron_condor": IronCondorStrategy(),
    "iron_butterfly": IronButterflyStrategy(),
}


def available_strategy_families() -> tuple[str, ...]:
    return tuple(_MODULES)


def resolve_strategy_families(selection: str | None) -> tuple[str, ...]:
    value = str(selection or "vertical_credit").strip().lower()
    if value == "income":
        # Default rent collection: directional credits first, range credits next.
        # Debit calendars and ATM butterflies require an explicit request.
        return ("vertical_credit", "iron_condor")
    if value == "all":
        return available_strategy_families()
    if value not in _MODULES:
        raise ValueError(f"unsupported option strategy family: {value}")
    return (value,)


def strategy_modules(families: tuple[str, ...] | list[str]):
    unknown = [name for name in families if name not in _MODULES]
    if unknown:
        raise ValueError(f"unsupported option strategy families: {','.join(unknown)}")
    return [_MODULES[name] for name in families]
