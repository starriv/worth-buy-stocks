"""Deterministic historical research package for worth-buy-stocks.

The package has no import-time IO.  Data acquisition, point-in-time replay,
event study, simulation, artifact generation and visualization are separate
layers so every report can be reproduced from one canonical artifact.
"""
from __future__ import annotations

from .contracts import ARTIFACT_VERSION, CONTRACT_VERSION, DEFAULT_HORIZONS, WARMUP_SESSIONS

__all__ = [
    "ARTIFACT_VERSION",
    "CONTRACT_VERSION",
    "DEFAULT_HORIZONS",
    "WARMUP_SESSIONS",
    "BacktestDataError",
    "DataBundle",
    "build_artifact",
    "load_artifact",
    "load_backtest_data",
    "validate_artifact",
    "write_artifact",
]


def __getattr__(name: str):
    """Keep convenience exports without coupling submodule import graphs."""

    if name in {"BacktestDataError", "DataBundle", "load_backtest_data"}:
        from . import data

        return getattr(data, name)
    if name in {"build_artifact", "load_artifact", "validate_artifact", "write_artifact"}:
        from . import artifact

        return getattr(artifact, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
