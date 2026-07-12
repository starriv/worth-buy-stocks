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
DEFAULT_LOG = os.path.join(
    os.path.dirname(__file__), "..", "data", "rotation_forward_log.jsonl"
)


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
        # 退市/停牌票在信号日没有 bar，评分只是陈旧数据的残影，不得进入名单。
        if panel.close(symbol, date) is None:
            continue
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


def load_forward_log(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def merge_snapshot(
    records: list[dict[str, Any]], result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Append the current list as a forward record, deduped by signal date."""

    snapshot = {
        "signal_date": result["signal_date"],
        "strategy": result["strategy"],
        "top_n": result["top_n"],
        "regime": result["regime"],
        "members": [
            {"symbol": row["symbol"], "composite": row["composite"]}
            for row in result["target"]
        ],
        "recorded_at": dt.datetime.now(dt.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    }
    merged = [row for row in records if row.get("signal_date") != snapshot["signal_date"]]
    merged.append(snapshot)
    merged.sort(key=lambda row: str(row.get("signal_date")))
    return merged


def write_forward_log(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def rebalance_status(
    records: list[dict[str, Any]], calendar: list[str], as_of: str
) -> dict[str, Any]:
    """Sessions elapsed since the last recorded signal and whether a new one is due."""

    recorded = [
        str(row["signal_date"]) for row in records
        if str(row.get("signal_date", "")) <= as_of
    ]
    if not recorded:
        return {"last_recorded_signal": None, "sessions_elapsed": None, "due": True}
    last = max(recorded)
    index = {date: position for position, date in enumerate(calendar)}
    if last not in index or as_of not in index:
        return {"last_recorded_signal": last, "sessions_elapsed": None, "due": True}
    elapsed = index[as_of] - index[last]
    return {
        "last_recorded_signal": last,
        "sessions_elapsed": elapsed,
        "due": elapsed >= REBALANCE_SESSIONS,
    }


def evaluate_records(
    records: list[dict[str, Any]], panel: Any, *, horizon: int = REBALANCE_SESSIONS
) -> dict[str, Any]:
    """Settle matured forward records: equal-weight gross return vs QQQ/SPY."""

    index = {date: position for position, date in enumerate(panel.calendar)}
    matured: list[dict[str, Any]] = []
    pending = 0
    for row in records:
        signal_date = str(row.get("signal_date"))
        position = index.get(signal_date)
        if position is None or position + horizon >= len(panel.calendar):
            pending += 1
            continue
        members = [str(item["symbol"]) for item in row.get("members") or []]
        returns = [
            value for value in (
                panel.fwd_return_pct(symbol, position, horizon) for symbol in members
            )
            if isinstance(value, (int, float))
        ]
        portfolio = sum(returns) / len(returns) if returns else 0.0
        qqq = panel.fwd_return_pct("QQQ", position, horizon)
        spy = panel.fwd_return_pct("SPY", position, horizon)
        matured.append({
            "signal_date": signal_date,
            "regime": row.get("regime"),
            "members": len(members),
            "portfolio_return_pct": round(portfolio, 4),
            "qqq_return_pct": round(qqq, 4) if isinstance(qqq, (int, float)) else None,
            "spy_return_pct": round(spy, 4) if isinstance(spy, (int, float)) else None,
            "excess_vs_qqq_pct": (
                round(portfolio - qqq, 4) if isinstance(qqq, (int, float)) else None
            ),
        })
    excesses = [
        row["excess_vs_qqq_pct"] for row in matured
        if isinstance(row["excess_vs_qqq_pct"], (int, float))
    ]
    return {
        "report_type": "rotation_forward_evaluation",
        "horizon_sessions": horizon,
        "matured": matured,
        "pending": pending,
        "mean_excess_vs_qqq_pct": (
            round(sum(excesses) / len(excesses), 4) if excesses else None
        ),
        "note": "毛收益口径（未扣双边 10bps，约 -0.02%/期）；空仓期组合收益按 0 计。",
    }


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
    parser.add_argument("--record", action="store_true", help="把本次名单固化为前瞻记录")
    parser.add_argument("--evaluate", action="store_true", help="结算已满 63 交易日的前瞻记录")
    parser.add_argument("--log", default=os.path.normpath(DEFAULT_LOG))
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
        records = load_forward_log(args.log)
        if args.evaluate:
            evaluation = evaluate_records(records, panel)
            print(json.dumps(evaluation, ensure_ascii=False, indent=2))
            logger.info(
                "rotation_forward_evaluated",
                matured=len(evaluation["matured"]),
                pending=evaluation["pending"],
            )
            return 0
        result = build_rotation_list(
            panel, top_n=args.top, signal_date=args.date, holdings=holdings,
        )
        result["rebalance_status"] = rebalance_status(
            records, list(panel.calendar), result["signal_date"]
        )
        if args.record:
            write_forward_log(args.log, merge_snapshot(records, result))
            result["recorded_to"] = str(Path(args.log))
        result["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        logger.info(
            "rotation_list_completed",
            signal_date=result["signal_date"],
            regime=result["regime"],
            members=len(result["target"]),
            recorded=args.record,
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
