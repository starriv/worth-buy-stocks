#!/usr/bin/env python3
"""Unified CLI for all historical backtest and research workflows."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from backtesting.artifact import build_artifact, load_artifact, write_artifact  # noqa: E402
from backtesting.contracts import BacktestConfig, DEFAULT_HORIZONS, WARMUP_SESSIONS  # noqa: E402
from backtesting import cross_sectional  # noqa: E402
from backtesting.data import BacktestDataError, load_backtest_data  # noqa: E402
from backtesting.visualization import write_html  # noqa: E402
from backtesting.lifecycle_validation import (  # noqa: E402
    run_validation as run_lifecycle_validation,
    write_validation_report,
)
from backtesting.index_lifecycle_validation import (  # noqa: E402
    run_validation as run_index_lifecycle_validation,
)
from skill_logging import (  # noqa: E402
    add_logging_arguments,
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)


logger = get_logger("backtest")
EXIT_INTERNAL = 1
EXIT_ARGUMENT = 2
EXIT_DATA = 3
RESEARCH_COMMANDS = {
    "factor-ic": ("backtest_factor_ic", cross_sectional.run_factor_ic),
    "score": ("backtest_score", cross_sectional.run_score),
    "robustness": ("backtest_robustness", cross_sectional.run_robustness),
}


def _default_dates() -> tuple[str, str]:
    end = dt.date.today()
    try:
        start = end.replace(year=end.year - 5)
    except ValueError:
        start = end.replace(year=end.year - 5, day=28)
    return start.isoformat(), end.isoformat()


def _horizons(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(sorted(set(int(item.strip()) for item in value.split(",") if item.strip())))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons 必须是逗号分隔正整数") from exc
    if not parsed or any(item < 1 for item in parsed):
        raise argparse.ArgumentTypeError("horizons 必须是逗号分隔正整数")
    return parsed


def _add_research_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    parser.add_argument("--adjustment", choices=("split",), default="split")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    parser.add_argument("--cache-db", help="SQLite 文件路径；默认 data/market-data.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统一历史回测、横截面研究与可视化")
    add_logging_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    default_start, default_end = _default_dates()

    single = sub.add_parser("single", help="运行单标的历史回测")
    single.add_argument("--symbol", required=True)
    single.add_argument("--start", default=default_start)
    single.add_argument("--end", default=default_end)
    single.add_argument("--data-source", choices=("cache-only", "auto", "input"), default="cache-only")
    single.add_argument("--input", help="input 模式的 multi-bars JSON；- 表示 stdin")
    single.add_argument("--cache-db")
    single.add_argument("--feed", choices=("iex",), default="iex")
    single.add_argument("--adjustment", choices=("split",), default="split")
    single.add_argument("--horizons", type=_horizons, default=DEFAULT_HORIZONS)
    single.add_argument("--cost-bps", type=float, default=10.0)
    single.add_argument("--warmup-sessions", type=int, default=WARMUP_SESSIONS)
    single.add_argument(
        "--signal-step", type=int, default=1,
        help="性能/测试下采样；非 1 的 artifact 固定标记 invalid_run",
    )
    single.add_argument("--timeout", type=int, default=60)
    single.add_argument("--output-dir", default="artifacts/backtests")
    single.add_argument("--json", default="auto", help="auto、- 或 JSON 输出路径")
    single.add_argument("--html", default="auto", help="auto、off 或 HTML 输出路径")

    render = sub.add_parser("render", help="从 canonical artifact 重新生成 HTML")
    render.add_argument("--artifact", required=True)
    render.add_argument("--html", default="auto")

    lifecycle = sub.add_parser(
        "lifecycle-validate", help="运行预注册 stateful 多标的 cache-only holdout"
    )
    lifecycle.add_argument("--cache-db")
    lifecycle.add_argument(
        "--output",
        default="artifacts/backtests/stateful-lifecycle-holdout-2025-04-04-2026-07-09.json",
    )

    ablation = sub.add_parser(
        "managed-ablation",
        help="运行 research-only managed 出入场/仓位消融（共享单次 replay）",
    )
    ablation.add_argument("--symbols", required=True, help="逗号分隔标的列表")
    ablation.add_argument("--start", required=True)
    ablation.add_argument("--end", required=True)
    ablation.add_argument("--cost-bps", type=float, default=10.0)
    ablation.add_argument("--cache-db")
    ablation.add_argument("--output", default="auto", help="auto 或 JSON 输出路径")

    signal_ablation = sub.add_parser(
        "signal-ablation",
        help="运行 research-only 信号层消融（时间线后处理，执行层冻结）",
    )
    signal_ablation.add_argument("--symbols", required=True, help="逗号分隔标的列表")
    signal_ablation.add_argument("--start", required=True)
    signal_ablation.add_argument("--end", required=True)
    signal_ablation.add_argument("--cost-bps", type=float, default=10.0)
    signal_ablation.add_argument("--cache-db")
    signal_ablation.add_argument("--output", default="auto", help="auto 或 JSON 输出路径")

    rotation = sub.add_parser(
        "rotation-study",
        help="运行 research-only 横截面 Top-N 轮动组合研究（预注册配置矩阵）",
    )
    rotation.add_argument("--feed", choices=("iex",), default="iex")
    rotation.add_argument("--adjustment", choices=("split",), default="split")
    rotation.add_argument("--timeout", type=int, default=60)
    rotation.add_argument("--cache", choices=("auto", "refresh", "off"), default="auto")
    rotation.add_argument("--cache-db")
    rotation.add_argument("--cost-bps", type=float, default=10.0)
    rotation.add_argument("--output", default="auto", help="auto 或 JSON 输出路径")

    index_lifecycle = sub.add_parser(
        "index-lifecycle-validate",
        help="运行预注册指数 ETF stateful 分层 cache-only holdout",
    )
    index_lifecycle.add_argument("--cache-db")
    index_lifecycle.add_argument(
        "--output",
        default="artifacts/backtests/index-etf-lifecycle-holdout-2025-04-04-2026-07-09.json",
    )

    research_help = {
        "factor-ic": "运行逐因子截面 IC 研究",
        "score": "运行 composite 分桶与确认层研究",
        "robustness": "运行非重叠、regime、行业中性与幸存者探针",
    }
    for command, help_text in research_help.items():
        research = sub.add_parser(command, help=help_text)
        _add_research_arguments(research)
    return parser


def _json_stdout(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _single(args: argparse.Namespace) -> int:
    symbol = str(args.symbol).strip().upper()
    if not symbol or "," in symbol:
        _json_stdout({"error": "symbol_invalid", "message": "single 只接受一个 symbol"})
        return EXIT_ARGUMENT
    if args.cost_bps < 0 or args.signal_step < 1 or args.timeout < 1:
        _json_stdout({
            "error": "argument_invalid",
            "message": "cost-bps >= 0、signal-step >= 1 且 timeout >= 1",
        })
        return EXIT_ARGUMENT
    bind_log_context(command="backtest_single", symbol=symbol, data_source=args.data_source)
    try:
        config = BacktestConfig(
            symbol=symbol,
            start=args.start,
            end=args.end,
            horizons=tuple(args.horizons),
            cost_bps=float(args.cost_bps),
            data_source=args.data_source,
            feed=args.feed,
            adjustment=args.adjustment,
            warmup_sessions=args.warmup_sessions,
            signal_step=args.signal_step,
        )
        bundle = load_backtest_data(
            [symbol, "SPY", "QQQ"],
            args.start,
            args.end,
            mode=args.data_source,
            feed=args.feed,
            adjustment=args.adjustment,
            cache_db=args.cache_db,
            input_path=args.input,
            warmup_sessions=args.warmup_sessions,
            timeout=args.timeout,
        )
        artifact = build_artifact(config, bundle)
    except BacktestDataError as exc:
        logger.warning("backtest_data_invalid", error_code=exc.code, reason=exc.message)
        _json_stdout(exc.as_dict())
        return EXIT_DATA
    except (ValueError, TypeError) as exc:
        logger.warning("backtest_contract_invalid", reason=str(exc))
        _json_stdout({"error": "contract_invalid", "message": str(exc)})
        return EXIT_ARGUMENT
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("backtest_failed", reason=str(exc))
        _json_stdout({"error": "backtest_failed", "message": str(exc)})
        return EXIT_INTERNAL

    output_dir = Path(args.output_dir).expanduser().resolve()
    stem = f"{symbol}-{bundle.actual_start}-{bundle.actual_end}-{artifact['semantic_hash'][:10]}"
    json_path: Path | None = None
    html_path: Path | None = None
    try:
        if args.json == "-":
            if args.html not in ("off", "auto"):
                html_path = write_html(args.html, artifact)
            elif args.html == "auto":
                html_path = write_html(output_dir / f"{stem}.html", artifact)
            _json_stdout(artifact)
        else:
            json_path = write_artifact(
                output_dir / f"{stem}.json" if args.json == "auto" else args.json,
                artifact,
            )
            if args.html != "off":
                html_path = write_html(
                    output_dir / f"{stem}.html" if args.html == "auto" else args.html,
                    artifact,
                )
            _json_stdout({
                "status": "ok",
                "validation": artifact["validation"]["status"],
                "semantic_hash": artifact["semantic_hash"],
                "json": str(json_path) if json_path else None,
                "html": str(html_path) if html_path else None,
            })
        logger.info(
            "backtest_completed",
            validation=artifact["validation"]["status"],
            semantic_hash=artifact["semantic_hash"],
            json_path=str(json_path) if json_path else None,
            html_path=str(html_path) if html_path else None,
        )
        return 0
    except (OSError, ValueError) as exc:
        logger.exception("backtest_artifact_write_failed", reason=str(exc))
        if args.json != "-":
            _json_stdout({"error": "artifact_write_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _render(args: argparse.Namespace) -> int:
    bind_log_context(command="backtest_render")
    try:
        artifact = load_artifact(args.artifact)
        source = Path(args.artifact).expanduser().resolve()
        target = source.with_suffix(".html") if args.html == "auto" else Path(args.html).expanduser().resolve()
        html_path = write_html(target, artifact)
        _json_stdout({
            "status": "ok",
            "validation": artifact["validation"]["status"],
            "semantic_hash": artifact["semantic_hash"],
            "html": str(html_path),
        })
        logger.info("backtest_html_written", html_path=str(html_path))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("backtest_artifact_invalid", reason=str(exc))
        _json_stdout({"error": "artifact_invalid", "message": str(exc)})
        return EXIT_ARGUMENT


def _lifecycle_validate(args: argparse.Namespace) -> int:
    bind_log_context(command="backtest_lifecycle_validate", data_source="cache-only")
    try:
        report = run_lifecycle_validation(cache_db=args.cache_db)
        path = write_validation_report(args.output, report)
        _json_stdout({
            "status": "ok",
            "decision": report["summary"]["decision"],
            "semantic_hash": report["semantic_hash"],
            "valid_symbols": report["summary"]["valid_symbols"],
            "output": str(path),
        })
        logger.info(
            "stateful_lifecycle_validation_completed",
            decision=report["summary"]["decision"],
            valid_symbols=report["summary"]["valid_symbols"],
            semantic_hash=report["semantic_hash"],
            output_path=str(path),
        )
        return 0
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("stateful_lifecycle_validation_invalid", reason=str(exc))
        _json_stdout({"error": "validation_invalid", "message": str(exc)})
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("stateful_lifecycle_validation_failed", reason=str(exc))
        _json_stdout({"error": "validation_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _managed_ablation(args: argparse.Namespace) -> int:
    from backtesting.managed_ablation import run_ablation, write_validation_report

    symbols = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    if not symbols or args.cost_bps < 0:
        _json_stdout({
            "error": "argument_invalid",
            "message": "symbols 非空且 cost-bps >= 0",
        })
        return EXIT_ARGUMENT
    bind_log_context(command="backtest_managed_ablation", data_source="cache-only")
    try:
        report = run_ablation(
            symbols, args.start, args.end,
            cost_bps=float(args.cost_bps), cache_db=args.cache_db,
        )
        if args.output == "auto":
            target = Path("artifacts/backtests") / (
                f"managed-ablation-{args.start}-{args.end}-{report['semantic_hash'][:10]}.json"
            )
        else:
            target = Path(args.output)
        path = write_validation_report(target, report)
        _json_stdout({
            "status": "ok",
            "lifecycle": report["lifecycle"],
            "semantic_hash": report["semantic_hash"],
            "symbols_ok": sum(row["status"] == "ok" for row in report["per_symbol"]),
            "output": str(path),
        })
        logger.info(
            "managed_ablation_completed",
            semantic_hash=report["semantic_hash"],
            output_path=str(path),
        )
        return 0
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("managed_ablation_invalid", reason=str(exc))
        _json_stdout({"error": "ablation_invalid", "message": str(exc)})
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("managed_ablation_failed", reason=str(exc))
        _json_stdout({"error": "ablation_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _signal_ablation(args: argparse.Namespace) -> int:
    from backtesting.managed_ablation import run_signal_ablation, write_validation_report

    symbols = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    if not symbols or args.cost_bps < 0:
        _json_stdout({
            "error": "argument_invalid",
            "message": "symbols 非空且 cost-bps >= 0",
        })
        return EXIT_ARGUMENT
    bind_log_context(command="backtest_signal_ablation", data_source="cache-only")
    try:
        report = run_signal_ablation(
            symbols, args.start, args.end,
            cost_bps=float(args.cost_bps), cache_db=args.cache_db,
        )
        if args.output == "auto":
            target = Path("artifacts/backtests") / (
                f"signal-ablation-{args.start}-{args.end}-{report['semantic_hash'][:10]}.json"
            )
        else:
            target = Path(args.output)
        path = write_validation_report(target, report)
        _json_stdout({
            "status": "ok",
            "lifecycle": report["lifecycle"],
            "semantic_hash": report["semantic_hash"],
            "symbols_ok": sum(row["status"] == "ok" for row in report["per_symbol"]),
            "output": str(path),
        })
        logger.info(
            "signal_ablation_completed",
            semantic_hash=report["semantic_hash"],
            output_path=str(path),
        )
        return 0
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("signal_ablation_invalid", reason=str(exc))
        _json_stdout({"error": "ablation_invalid", "message": str(exc)})
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("signal_ablation_failed", reason=str(exc))
        _json_stdout({"error": "ablation_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _rotation_study(args: argparse.Namespace) -> int:
    from backtesting.rotation import run_rotation_study, write_validation_report

    if args.cost_bps < 0 or args.timeout < 1:
        _json_stdout({
            "error": "argument_invalid",
            "message": "cost-bps >= 0 且 timeout >= 1",
        })
        return EXIT_ARGUMENT
    bind_log_context(command="backtest_rotation_study", feed=args.feed)
    try:
        report = run_rotation_study(
            feed=args.feed, adjustment=args.adjustment, timeout=args.timeout,
            cache_mode=args.cache, cache_db=args.cache_db,
            cost_bps=float(args.cost_bps),
        )
        if args.output == "auto":
            target = Path("artifacts/backtests") / (
                f"rotation-study-{report['semantic_hash'][:10]}.json"
            )
        else:
            target = Path(args.output)
        path = write_validation_report(target, report)
        _json_stdout({
            "status": "ok",
            "lifecycle": report["lifecycle"],
            "winner": report["selection"]["winner"],
            "decision": report["selection"]["decision"],
            "semantic_hash": report["semantic_hash"],
            "output": str(path),
        })
        logger.info(
            "rotation_study_completed",
            winner=report["selection"]["winner"],
            decision=report["selection"]["decision"],
            semantic_hash=report["semantic_hash"],
            output_path=str(path),
        )
        return 0
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("rotation_study_invalid", reason=str(exc))
        _json_stdout({"error": "rotation_invalid", "message": str(exc)})
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("rotation_study_failed", reason=str(exc))
        _json_stdout({"error": "rotation_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _index_lifecycle_validate(args: argparse.Namespace) -> int:
    bind_log_context(command="backtest_index_lifecycle_validate", data_source="cache-only")
    try:
        report = run_index_lifecycle_validation(cache_db=args.cache_db)
        path = write_validation_report(args.output, report)
        _json_stdout({
            "status": "ok",
            "decision": report["summary"]["decision"],
            "semantic_hash": report["semantic_hash"],
            "valid_symbols": report["summary"]["valid_symbols"],
            "broad_valid_symbols": report["summary"]["broad_valid_symbols"],
            "sector_valid_symbols": report["summary"]["sector_valid_symbols"],
            "output": str(path),
        })
        logger.info(
            "index_etf_lifecycle_validation_completed",
            decision=report["summary"]["decision"],
            valid_symbols=report["summary"]["valid_symbols"],
            semantic_hash=report["semantic_hash"],
            output_path=str(path),
        )
        return 0
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("index_etf_lifecycle_validation_invalid", reason=str(exc))
        _json_stdout({"error": "validation_invalid", "message": str(exc)})
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("index_etf_lifecycle_validation_failed", reason=str(exc))
        _json_stdout({"error": "validation_failed", "message": str(exc)})
        return EXIT_INTERNAL


def _research(args: argparse.Namespace) -> int:
    event, runner = RESEARCH_COMMANDS[args.command]
    if args.timeout < 1:
        logger.warning("backtest_argument_invalid", reason="timeout must be >= 1")
        return EXIT_ARGUMENT
    bind_log_context(command=event, feed=args.feed)
    try:
        runner(
            args.feed,
            args.adjustment,
            args.timeout,
            cache_mode=args.cache,
            cache_db=args.cache_db,
        )
        return 0
    except (TypeError, ValueError) as exc:
        logger.warning("backtest_argument_invalid", reason=str(exc))
        return EXIT_ARGUMENT
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.exception("backtest_research_failed", research=args.command, reason=str(exc))
        return EXIT_INTERNAL


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    configure_logging(level=args.log_level, log_format=args.log_format)
    clear_log_context()
    if args.command == "single":
        return _single(args)
    if args.command == "render":
        return _render(args)
    if args.command == "lifecycle-validate":
        return _lifecycle_validate(args)
    if args.command == "managed-ablation":
        return _managed_ablation(args)
    if args.command == "rotation-study":
        return _rotation_study(args)
    if args.command == "signal-ablation":
        return _signal_ablation(args)
    if args.command == "index-lifecycle-validate":
        return _index_lifecycle_validate(args)
    return _research(args)


if __name__ == "__main__":
    raise SystemExit(main())
