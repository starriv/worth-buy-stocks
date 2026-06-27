#!/usr/bin/env python3
"""Constants for the deterministic multi-factor scoring engine.

ALPHA weights are calibrated from per-factor IC backtests
(see ``backtest_factor_ic.py``). See the package docstring in
``scoring/__init__.py`` for the full IC calibration narrative.
"""
from __future__ import annotations

# ALPHA 因子权重（详见包 docstring）。efficiency 为近正交的第三因子（小权重）。
ALPHA_WEIGHTS: dict[str, int] = {"momentum": 55, "rel_strength": 35, "efficiency": 10}
TARGET_VOL_PCT: float = 20.0  # 反波动率仓位的目标年化波动
LOW_LIQUIDITY_USD: float = 5_000_000  # 20 日均成交额低于此值 → 低流动性警示（仅 flag，不封顶）
LLM_CAP_HARD: int = 50  # 致命红旗（事件破坏趋势前提）→ 封顶为否
LLM_CAP_SOFT: int = 74  # 软红旗 / 数据存疑 → 把本可成立的「是」降级为「观察」
CONFIRMATION_MIN: dict[str, float] = {
    "technical": 0.50,
    "volume": 0.40,
    "trend_quality": 0.40,
}
