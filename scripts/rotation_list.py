#!/usr/bin/env python3
"""Current-day Top-N rotation list from the promoted rotation-study config.

Produces the analysis-only holdings list for the cross-sectional rotation
strategy that passed the pre-registered holdout (``rotation-study`` winner
``step63_top20``): rank the frozen research universe by the production
cross-sectional ``composite`` on the latest completed session, hold the top N
equal weight while SPY closes above its 200-session average, otherwise go
fully to cash. Output is a decision list, never an order instruction.

stdout carries the JSON result; logs go to stderr only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, os.path.dirname(__file__))

from backtesting.cross_sectional import regime_map  # noqa: E402
from backtesting.universe import UNIVERSE  # noqa: E402
from skill_logging import (  # noqa: E402
    add_logging_arguments,
    bind_log_context,
    configure_logging,
    get_logger,
)


logger = get_logger("rotation_list")
DEFAULT_TOP_N = 20
PROMOTED_CONFIG = "step63_top20"
REBALANCE_SESSIONS = 63


def build_rotation_list(
    panel: Any,
    *,
    top_n: int = DEFAULT_TOP_N,
    signal_date: str | None = None,
    holdings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank the universe at ``signal_date`` and diff against current holdings."""

    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    calendar = list(panel.calendar)
    if not calendar:
        raise ValueError("panel calendar is empty")
    date = signal_date or calendar[-1]
    if date not in set(calendar):
        raise ValueError(f"signal date {date} is not a completed SPY session")
    regimes = regime_map(panel.bars.get("SPY", []))
    regime_on = bool(regimes.get(date))
    ranked: list[dict[str, Any]] = []
    symbols = panel.scores_at(date)
    for symbol in UNIVERSE:
        score = (symbols.get(symbol, {}) or {}).get("score") or {}
        composite = score.get("composite")
        if isinstance(composite, (int, float)) and math.isfinite(float(composite)):
            ranked.append({"symbol": symbol, "composite": float(composite)})
    ranked.sort(key=lambda row: (-row["composite"], row["symbol"]))
    members = ranked[:top_n] if regime_on else []
    weight = round(100.0 / top_n, 2)
    target = [
        {**row, "weight_pct": weight}
        for row in members
    ]
    held = sorted(str(symbol).upper() for symbol in (holdings or {}))
    target_symbols = {row["symbol"] for row in target}
    output: dict[str, Any] = {
        "strategy": PROMOTED_CONFIG,
        "signal_date": date,
        "rebalance_every_sessions": REBALANCE_SESSIONS,
        "regime": "risk_on" if regime_on else "risk_off",
        "action": "hold_top_n_equal_weight" if regime_on else "all_cash",
        "top_n": top_n,
        "scored_universe": len(ranked),
        "target": target,
        "note": (
            "研究晋升配置的分析名单：SPY 高于 200 日线时等权持有横截面评分前 N，"
            "跌破则全现金；每 63 个交易日复核一次。只分析不交易。"
        ),
    }
    if holdings is not None:
        output["diff"] = {
            "buy": sorted(target_symbols - set(held)),
            "sell": sorted(set(held) - target_symbols),
            "keep": sorted(target_symbols & set(held)),
        }
    return output


def _load_holdings(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {str(item).upper(): None for item in raw}
    if isinstance(raw, dict):
        return {str(key).upper(): value for key, value in raw.items()}
    raise ValueError("holdings 文件必须是 symbol 列表或 {symbol: weight} 对象")


def main() -> int:
    parser = argparse.ArgumentParser(description="输出轮动策略当前 Top-N 名单（只分析不交易）")
    add_logging_arguments(parser)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--date", help="信号日（默认最近完成交易日）")
    parser.add_argument("--holdings", help="当前持仓 JSON（列表或 {symbol: weight}）")
    parser.add_argument("--feed", choices=("iex",), default="iex")
    parser.add_argument("--adjustment", choices=("split",), default="split")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    parser.add_argument("--cache-db")
    args = parser.parse_args()
    configure_logging(level=args.log_level, log_format=args.log_format)
    bind_log_context(command="rotation_list", feed=args.feed)
    if args.top < 1 or args.timeout < 1:
        print(json.dumps({"error": "argument_invalid", "message": "top >= 1 且 timeout >= 1"}, ensure_ascii=False))
        return 2
    try:
        holdings = _load_holdings(args.holdings)
        from backtesting.panel import load_panel

        panel = load_panel(
            args.feed, args.adjustment, args.timeout,
            cache_mode=args.cache, cache_db=args.cache_db,
        )
        result = build_rotation_list(
            panel, top_n=args.top, signal_date=args.date, holdings=holdings,
        )
        result["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        logger.info(
            "rotation_list_completed",
            signal_date=result["signal_date"],
            regime=result["regime"],
            members=len(result["target"]),
        )
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("rotation_list_invalid", reason=str(exc))
        print(json.dumps({"error": "rotation_list_invalid", "message": str(exc)}, ensure_ascii=False))
        return 3
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("rotation_list_failed", reason=str(exc))
        print(json.dumps({"error": "rotation_list_failed", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
