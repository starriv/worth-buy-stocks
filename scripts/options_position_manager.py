#!/usr/bin/env python3
"""Ledger-backed, read-only lifecycle management for defined-risk credit structures."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from options_engine.ledger import OptionsLedger, position_from_candidate
from options_engine.lifecycle import LifecycleConfig, review_portfolio
from options_engine.lifecycle_live import hydrate_live_payload
from options_engine.lifecycle_notifications import notify_lifecycle
from options_engine.lifecycle_reporting import build_lifecycle_alerts, write_lifecycle_report


def _load(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _dump(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _ledger_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger-db", help="默认 data/options-ledger.sqlite3")


def _parser(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="定义风险期权结构台账与生命周期检查")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="记录用户确认的实际开仓")
    _ledger_argument(add)
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="单个持仓条款 JSON")
    source.add_argument("--candidate-result", help="options_seller 结果 JSON")
    add.add_argument("--candidate-id")
    add.add_argument("--quantity", type=int)
    add.add_argument("--initial-credit", type=float, help="实际成交净权利金")
    add.add_argument("--opened-at", help="实际开仓 ISO 时间")

    listing = commands.add_parser("list", help="列出台账持仓")
    _ledger_argument(listing)
    listing.add_argument("--status", choices=("open", "closed", "all"), default="open")

    close = commands.add_parser("close", help="记录用户确认的实际平仓")
    _ledger_argument(close)
    close.add_argument("--position-id", required=True)
    close.add_argument("--closed-at", required=True)
    close.add_argument("--close-debit", type=float, required=True, help="实际平仓净支出")
    close.add_argument("--quantity", type=int, help="本次平仓组数；缺省为全部剩余组数")

    history = commands.add_parser("history", help="查看单个价差的历史检查记录")
    _ledger_argument(history)
    history.add_argument("--position-id", required=True)
    history.add_argument("--limit", type=int, default=100)

    performance = commands.add_parser("performance", help="汇总实际平仓成交的描述性绩效")
    _ledger_argument(performance)
    performance.add_argument("--output", help="可选：同时写入指定 JSON 文件")

    review = commands.add_parser("review", help="检查已有价差生命周期")
    _ledger_argument(review)
    source = review.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="完整离线生命周期 JSON；不会触网或写台账")
    source.add_argument("--live", action="store_true", help="从台账读取并自动补充只读实时数据")
    review.add_argument("--record-review", action=argparse.BooleanOptionalAction, default=True)
    review.add_argument("--take-profit-pct", type=float, default=50.0)
    review.add_argument("--exit-dte", type=int, default=14)
    review.add_argument("--hard-exit-dte", type=int, default=7)
    review.add_argument("--defense-delta", type=float, default=0.45)
    review.add_argument("--max-close-debit-multiple", type=float, default=2.0)
    review.add_argument("--max-quote-age-minutes", type=int, default=15)
    review.add_argument("--pin-risk-dte", type=int, default=1)
    review.add_argument("--pin-risk-width-fraction", type=float, default=0.10)
    review.add_argument("--option-feed", choices=("auto", "opra", "indicative"), default="auto")
    review.add_argument("--feed", default="iex")
    review.add_argument("--adjustment", default="split")
    review.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    review.add_argument("--cache-db")
    review.add_argument("--events", choices=("auto", "on", "off"), default="auto")
    review.add_argument("--timeout", type=int, default=30)
    review.add_argument("--finnhub-timeout", type=int, default=15)
    review.add_argument("--workers", type=int, default=4)
    review.add_argument("--report-dir", help="显式写入中文生命周期日报的目录")
    review.add_argument(
        "--report-format", choices=("markdown", "json", "both"), default="markdown",
    )
    review.add_argument(
        "--include-performance", action=argparse.BooleanOptionalAction, default=False,
        help="在输出/日报附带台账实际平仓描述性绩效",
    )
    review.add_argument(
        "--notify", choices=("off", "on", "alerts"), default="off",
        help="Telegram 默认关闭；alerts 仅有生命周期告警时发送",
    )
    review.add_argument("--notify-timeout", type=int, default=15)
    return parser.parse_args(argv)


def _find_candidate(payload: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    matches = [
        candidate
        for symbol_row in (payload.get("symbols") or {}).values()
        for candidate in (symbol_row.get("candidates") or [])
        if str(candidate.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise ValueError("candidate_id must match exactly one candidate")
    return matches[0]


def main(argv=None) -> int:
    args = _parser(argv)
    if args.command == "review" and args.input:
        if args.include_performance:
            raise ValueError("offline review cannot read the performance ledger")
        if args.notify != "off":
            raise ValueError("offline review cannot send notifications")
    ledger = OptionsLedger(args.ledger_db)
    if args.command == "add":
        if args.input:
            position = _load(args.input)
            position = position.get("position") if isinstance(position, dict) and "position" in position else position
        else:
            if not args.candidate_id or args.quantity is None or args.initial_credit is None or not args.opened_at:
                raise ValueError("candidate add requires candidate-id, quantity, initial-credit and opened-at")
            candidate = _find_candidate(_load(args.candidate_result), args.candidate_id)
            position = position_from_candidate(
                candidate, quantity=args.quantity,
                initial_credit=args.initial_credit, opened_at=args.opened_at,
            )
        _dump({"status": "ok", "position": ledger.add(position)})
        return 0
    if args.command == "list":
        rows = ledger.list(args.status)
        _dump({"status": "ok", "count": len(rows), "positions": rows})
        return 0
    if args.command == "close":
        _dump({"status": "ok", "position": ledger.close(
            args.position_id, closed_at=args.closed_at, close_debit=args.close_debit,
            quantity=args.quantity,
        )})
        return 0
    if args.command == "history":
        rows = ledger.reviews(args.position_id, args.limit)
        _dump({"status": "ok", "count": len(rows), "reviews": rows})
        return 0
    if args.command == "performance":
        result = ledger.performance()
        if args.output:
            path = Path(args.output).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["output_file"] = str(path)
        _dump(result)
        return 0

    cfg = LifecycleConfig(
        take_profit_pct=args.take_profit_pct,
        exit_dte=args.exit_dte,
        hard_exit_dte=args.hard_exit_dte,
        defense_delta=args.defense_delta,
        max_close_debit_multiple=args.max_close_debit_multiple,
        max_quote_age_minutes=args.max_quote_age_minutes,
        pin_risk_dte=args.pin_risk_dte,
        pin_risk_width_fraction=args.pin_risk_width_fraction,
    ).validate()
    if args.input:
        payload = _load(args.input)
    else:
        open_positions = ledger.list("open")
        payload = hydrate_live_payload(
            open_positions, option_feed=args.option_feed, equity_feed=args.feed,
            adjustment=args.adjustment, cache_mode=args.cache, cache_db=args.cache_db,
            events_mode=args.events, timeout=args.timeout,
            finnhub_timeout=args.finnhub_timeout, workers=args.workers,
        )
    result = review_portfolio(payload, cfg)
    result["data_flags"] = payload.get("data_flags") or []
    if args.live:
        result["source_context"] = {
            "account": payload.get("account_summary"),
            "snapshot_status": payload.get("snapshot_status"),
            "expected_quote_session": payload.get("expected_quote_session"),
        }
    if args.live and args.record_review:
        result["reviews_recorded"] = ledger.record_reviews(
            str(result.get("as_of")), result.get("positions") or []
        )
    result["alerts"] = build_lifecycle_alerts(result)
    performance = ledger.performance() if args.include_performance else None
    if performance is not None:
        result["performance"] = performance
    if args.report_dir:
        result["report_files"] = write_lifecycle_report(
            result, args.report_dir, output_format=args.report_format, performance=performance,
        )
    if args.notify != "off":
        result["notification"] = notify_lifecycle(
            result, mode=args.notify, timeout=args.notify_timeout,
        )
    _dump(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
        _dump({"error": str(exc)})
        raise SystemExit(1)
