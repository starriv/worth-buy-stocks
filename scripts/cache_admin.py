#!/usr/bin/env python3
"""Inspect, warm, refresh, verify, and safely purge the local market-data cache."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(__file__))
from asset_catalog import ensure_daily_asset_catalog  # noqa: E402
from fetching import fetch_bars, fetch_snapshots  # noqa: E402
from market_data_cache import classify_session  # noqa: E402
from market_data_rate_limit import (  # noqa: E402
    PageRateLimiter,
    WarmupAlreadyRunningError,
    WarmupFileLock,
)
from market_data_store import (  # noqa: E402
    MarketDataStore,
    missing_date_ranges,
    resolve_db_path,
)
from market_warmup import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_RPM,
    DEFAULT_MAX_RUNTIME,
    DEFAULT_WORKERS,
    DEFAULT_YEARS,
    load_symbols_file,
    run_market_warmup,
    start_date_for_years,
)
from market_reconcile import run_market_reconcile  # noqa: E402
from skill_logging import (  # noqa: E402
    add_logging_arguments,
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)


TABLES = (
    "daily_bars", "live_daily_bars", "latest_snapshots",
    "sync_ranges", "market_sessions", "feed_fallbacks",
    "assets", "asset_catalog_state",
)

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 3
EXIT_LOCKED = 4
EXIT_SIGINT = 130
logger = get_logger("cache_admin")


def _symbols(value: str | Iterable[str] | None) -> list[str]:
    values = value.split(",") if isinstance(value, str) else list(value or [])
    clean: list[str] = []
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in clean:
            clean.append(symbol)
    return clean


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _warm_target_summary(conn: sqlite3.Connection, names: set[str]) -> dict[str, Any] | None:
    """Read-only three-year coverage for symbols already tracked in SQLite."""
    if "market_sessions" not in names or "sync_ranges" not in names:
        return None
    sessions = [
        dict(row) for row in conn.execute(
            "SELECT session_date, open_at, close_at FROM market_sessions ORDER BY session_date"
        ).fetchall()
    ]
    if not sessions:
        return None
    try:
        state = classify_session(dt.datetime.now(dt.timezone.utc), sessions)
        end = state.latest_completed_session
        if not end:
            return None
        start = start_date_for_years(end, DEFAULT_YEARS)
    except (TypeError, ValueError):
        return None
    rows = conn.execute(
        """
        SELECT symbol, start_date, end_date FROM sync_ranges
        WHERE feed='iex' AND adjustment='split' AND symbol<>?
        ORDER BY symbol, start_date
        """,
        ("__MARKET_CALENDAR_XNYS__",),
    ).fetchall()
    ranges: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        ranges.setdefault(str(row["symbol"]), []).append(
            (str(row["start_date"]), str(row["end_date"]))
        )
    covered = sum(
        1 for symbol_ranges in ranges.values()
        if not missing_date_ranges(symbol_ranges, start, end)
    )
    tracked = len(ranges)
    return {
        "years": DEFAULT_YEARS,
        "feed": "iex",
        "adjustment": "split",
        "start": start,
        "latest_completed_session": end,
        "tracked_symbols": tracked,
        "covered_symbols": covered,
        "pending_tracked_symbols": tracked - covered,
        "coverage_pct": round((covered * 100.0 / tracked), 2) if tracked else 0.0,
        "scope": "tracked SQLite symbols only; market universe is discovered per warm-market run",
    }


def _asset_catalog_summary(
    conn: sqlite3.Connection, names: set[str]
) -> dict[str, Any] | None:
    if "assets" not in names or "asset_catalog_state" not in names:
        return None
    state = conn.execute(
        "SELECT * FROM asset_catalog_state ORDER BY catalog LIMIT 1"
    ).fetchone()
    if state is None:
        return None
    result = dict(state)
    exclusions = conn.execute(
        """
        SELECT COALESCE(excluded_reason, 'eligible') AS reason, count(*) AS count
        FROM assets WHERE is_current=1
        GROUP BY COALESCE(excluded_reason, 'eligible') ORDER BY reason
        """
    ).fetchall()
    result["exclusions"] = {
        str(row["reason"]): int(row["count"]) for row in exclusions
    }
    if "market_sessions" not in names or "sync_ranges" not in names:
        return result
    sessions = [
        dict(row) for row in conn.execute(
            "SELECT session_date, open_at, close_at FROM market_sessions ORDER BY session_date"
        ).fetchall()
    ]
    try:
        session_state = classify_session(dt.datetime.now(dt.timezone.utc), sessions)
        end = session_state.latest_completed_session
        if not end:
            return result
        start = start_date_for_years(end, DEFAULT_YEARS)
    except (TypeError, ValueError):
        return result
    symbols = [
        str(row["symbol"])
        for row in conn.execute(
            """
            SELECT DISTINCT symbol FROM assets
            WHERE is_current=1 AND data_eligible=1 ORDER BY symbol
            """
        ).fetchall()
    ]
    coverage_rows = conn.execute(
        """
        SELECT ranges.symbol, ranges.start_date, ranges.end_date
        FROM sync_ranges AS ranges
        JOIN assets ON assets.symbol=ranges.symbol
        WHERE assets.is_current=1 AND assets.data_eligible=1
          AND ranges.feed='iex' AND ranges.adjustment='split'
        ORDER BY ranges.symbol, ranges.start_date
        """
    ).fetchall()
    coverage: dict[str, list[tuple[str, str]]] = {symbol: [] for symbol in symbols}
    for row in coverage_rows:
        coverage.setdefault(str(row["symbol"]), []).append(
            (str(row["start_date"]), str(row["end_date"]))
        )
    covered = sum(
        1 for symbol in symbols
        if not missing_date_ranges(coverage.get(symbol, []), start, end)
    )
    result["reconcile_target"] = {
        "years": DEFAULT_YEARS,
        "feed": "iex",
        "adjustment": "split",
        "start": start,
        "latest_completed_session": end,
        "eligible_symbols": len(symbols),
        "covered_symbols": covered,
        "pending_symbols": len(symbols) - covered,
    }
    return result


def status_summary(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read cache status without creating or migrating a database."""
    db_path = resolve_db_path(path)
    result: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "recent_errors": {
            "status": "not_persisted",
            "reason": "缓存错误仅随当前调用返回，不在 SQLite 中持久化",
        },
    }
    if not db_path.exists():
        return result
    try:
        with _read_only_connection(db_path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            names = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            counts = {
                table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in TABLES if table in names
            }
            result.update({
                "schema_version": version,
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
                # quick_check keeps status fast on GB-sized caches; the
                # integrity-check subcommand runs the exhaustive scan.
                "integrity": str(conn.execute("PRAGMA quick_check").fetchone()[0]),
                "rows": counts,
            })
            if "daily_bars" in names:
                row = conn.execute(
                    "SELECT min(session_date), max(session_date) FROM daily_bars"
                ).fetchone()
                result["final_sessions"] = {"earliest": row[0], "latest": row[1]}
            if "latest_snapshots" in names:
                row = conn.execute(
                    "SELECT max(session_date), max(captured_at) FROM latest_snapshots"
                ).fetchone()
                result["latest_snapshot"] = {
                    "session_date": row[0], "captured_at": row[1]
                }
            if "sync_ranges" in names:
                rows = conn.execute(
                    """
                    SELECT feed, adjustment, count(*) AS ranges,
                           min(start_date) AS earliest, max(end_date) AS latest
                    FROM sync_ranges
                    GROUP BY feed, adjustment
                    ORDER BY feed, adjustment
                    """
                ).fetchall()
                result["coverage"] = [dict(row) for row in rows]
            warm_target = _warm_target_summary(conn, names)
            if warm_target is not None:
                result["warm_market_target"] = warm_target
            asset_catalog = _asset_catalog_summary(conn, names)
            if asset_catalog is not None:
                result["asset_catalog"] = asset_catalog
    except sqlite3.Error as exc:
        result["status"] = "unavailable"
        result["error"] = str(exc)
    return result


def integrity_summary(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Run SQLite integrity_check read-only; never create a missing cache."""
    db_path = resolve_db_path(path)
    if not db_path.exists():
        return {"path": str(db_path), "exists": False, "integrity": "missing"}
    try:
        with _read_only_connection(db_path) as conn:
            rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        return {
            "path": str(db_path), "exists": True,
            "integrity": "ok" if rows == ["ok"] else "failed",
            "details": rows,
        }
    except sqlite3.Error as exc:
        return {
            "path": str(db_path), "exists": True,
            "integrity": "unavailable", "error": str(exc),
        }


def _where_symbols(symbols: list[str]) -> tuple[str, list[str]]:
    if not symbols:
        return "", []
    return f" AND symbol IN ({','.join('?' for _ in symbols)})", symbols


def _subtract_range(start: str, end: str, cut_start: str, cut_end: str) -> list[tuple[str, str]]:
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    cut_first, cut_last = dt.date.fromisoformat(cut_start), dt.date.fromisoformat(cut_end)
    if cut_last < first or cut_first > last:
        return [(start, end)]
    out: list[tuple[str, str]] = []
    if first < cut_first:
        out.append((first.isoformat(), (cut_first - dt.timedelta(days=1)).isoformat()))
    if cut_last < last:
        out.append(((cut_last + dt.timedelta(days=1)).isoformat(), last.isoformat()))
    return out


def purge_cache(
    store: MarketDataStore,
    *,
    purge_all: bool = False,
    symbols: Iterable[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    feed: str = "iex",
    adjustment: str = "split",
) -> dict[str, int]:
    """Purge an explicit scope and keep coverage ranges honest."""
    clean = _symbols(symbols)
    if bool(start_date) != bool(end_date):
        raise ValueError("--start 与 --end 必须同时提供")
    if start_date and end_date:
        start_date = dt.date.fromisoformat(start_date).isoformat()
        end_date = dt.date.fromisoformat(end_date).isoformat()
        if start_date > end_date:
            raise ValueError("--start 不能晚于 --end")
    if not purge_all and not clean and not start_date:
        raise ValueError("purge 必须指定 --symbols、--start/--end，或显式 --all")
    feed = str(feed or "").strip().lower()
    adjustment = str(adjustment or "").strip().lower()
    affected = {table: 0 for table in TABLES}
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if purge_all:
                for table in TABLES:
                    affected[table] = conn.execute(f"DELETE FROM {table}").rowcount
            else:
                symbol_sql, symbol_params = _where_symbols(clean)
                if start_date:
                    affected["daily_bars"] = conn.execute(
                        f"""
                        DELETE FROM daily_bars
                        WHERE feed=? AND adjustment=?
                          AND session_date BETWEEN ? AND ?{symbol_sql}
                        """,
                        [feed, adjustment, start_date, end_date, *symbol_params],
                    ).rowcount
                    for table in ("live_daily_bars", "latest_snapshots"):
                        affected[table] = conn.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE feed=? AND session_date BETWEEN ? AND ?{symbol_sql}
                            """,
                            [feed, start_date, end_date, *symbol_params],
                        ).rowcount
                    coverage = conn.execute(
                        f"""
                        SELECT * FROM sync_ranges
                        WHERE feed=? AND adjustment=?
                          AND NOT (end_date<? OR start_date>?){symbol_sql}
                        """,
                        [feed, adjustment, start_date, end_date, *symbol_params],
                    ).fetchall()
                    for row in coverage:
                        conn.execute(
                            """
                            DELETE FROM sync_ranges
                            WHERE symbol=? AND feed=? AND adjustment=?
                              AND start_date=? AND end_date=?
                            """,
                            (
                                row["symbol"], row["feed"], row["adjustment"],
                                row["start_date"], row["end_date"],
                            ),
                        )
                        affected["sync_ranges"] += 1
                        for left, right in _subtract_range(
                            str(row["start_date"]), str(row["end_date"]),
                            start_date, end_date,
                        ):
                            conn.execute(
                                """
                                INSERT INTO sync_ranges (
                                    symbol, feed, adjustment, start_date, end_date, synced_at
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    row["symbol"], row["feed"], row["adjustment"],
                                    left, right, row["synced_at"],
                                ),
                            )
                else:
                    marks = ",".join("?" for _ in clean)
                    affected["daily_bars"] = conn.execute(
                        f"DELETE FROM daily_bars WHERE symbol IN ({marks}) AND feed=? AND adjustment=?",
                        [*clean, feed, adjustment],
                    ).rowcount
                    affected["sync_ranges"] = conn.execute(
                        f"DELETE FROM sync_ranges WHERE symbol IN ({marks}) AND feed=? AND adjustment=?",
                        [*clean, feed, adjustment],
                    ).rowcount
                    for table in ("live_daily_bars", "latest_snapshots"):
                        affected[table] = conn.execute(
                            f"DELETE FROM {table} WHERE symbol IN ({marks}) AND feed=?",
                            [*clean, feed],
                        ).rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if purge_all:
        # Reclaim file space after emptying every table; VACUUM must run
        # outside the purge transaction.
        with store.connection() as conn:
            conn.execute("VACUUM")
    return affected


def warm_or_refresh(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    symbols = _symbols(args.symbols)
    if not symbols:
        raise ValueError("warm/refresh 需要 --symbols")
    today = dt.datetime.now(dt.timezone.utc).date()
    start = args.start or (today - dt.timedelta(days=728)).isoformat()
    end = args.end or today.isoformat()
    bars, used_feed, note = fetch_bars(
        symbols, start, end, args.feed, args.adjustment, 10000, args.timeout,
        cache_mode=mode, cache_db=args.cache_db,
    )
    snapshots = fetch_snapshots(
        symbols, feed=used_feed, timeout=args.timeout, adjustment=args.adjustment,
        cache_mode=mode, cache_db=args.cache_db,
    )
    return {
        "mode": mode,
        "path": str(resolve_db_path(args.cache_db)),
        "symbols": symbols,
        "range": {"start": start, "end": end},
        "feed": used_feed,
        "bars": {"symbols": len(bars), "rows": sum(len(rows) for rows in bars.values())},
        "snapshots": {
            "status": snapshots.get("status"),
            "symbols": len(snapshots.get("symbols") or {}),
            "note": snapshots.get("cache_note") or snapshots.get("reason"),
        },
        "note": note,
    }


def _bounded_int(label: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} 必须是整数") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} 必须在 {minimum}..{maximum} 范围内"
            )
        return parsed
    return parse


def _symbols_file(value: str) -> str:
    try:
        load_symbols_file(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return str(Path(value).expanduser())


class WarmupProgressReporter:
    """Emit structured progress while keeping stdout summary-only."""

    def __init__(
        self, limiter: PageRateLimiter, *, interval: float = 15.0,
        clock=time.monotonic, component: str = "warm_market",
    ) -> None:
        self.limiter = limiter
        self.interval = max(0.1, float(interval))
        self.clock = clock
        self.started_at = float(clock())
        self._latest: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.logger = logger.bind(component=component)

    def _emit(self, payload: dict[str, Any]) -> None:
        fields = dict(payload)
        event_name = str(fields.pop("event", "warm_market_progress"))
        self.logger.info(event_name, **fields)

    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("event") == "planned" and "batches" in event:
            event = {**event, "remaining_batches": event["batches"]}
        self._latest.update(event)
        self._emit(event)

    def start(self) -> None:
        def heartbeat() -> None:
            while not self._stop.wait(self.interval):
                metrics = self.limiter.snapshot()
                payload = {
                    "event": "heartbeat",
                    "completed_batches": self._latest.get("completed_batches", 0),
                    "failed_batches": self._latest.get("failed_batches", 0),
                    "remaining_batches": self._latest.get("remaining_batches"),
                    "pages": metrics["pages"],
                    "effective_rpm": metrics["effective_rpm"],
                    "retries": metrics["retries"],
                    "elapsed": round(float(self.clock()) - self.started_at, 3),
                    "eta": round(
                        (self._latest.get("remaining_batches") or 0)
                        * 60.0 / self.limiter.max_rpm,
                        3,
                    ),
                }
                self._emit(payload)

        self._thread = threading.Thread(
            target=heartbeat, name="warm-market-progress", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _market_job_command(
    args: argparse.Namespace,
    *,
    runner,
    component: str,
    extra: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Run one long market-data job with shared signal and exit semantics."""
    limiter = PageRateLimiter(args.max_rpm)
    reporter = WarmupProgressReporter(limiter, component=component)
    stop_event = threading.Event()
    sigint_seen = threading.Event()
    previous_handler = None
    can_install_signal = threading.current_thread() is threading.main_thread()

    def handle_sigint(_signum, _frame) -> None:
        sigint_seen.set()
        stop_event.set()

    if can_install_signal:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handle_sigint)
    reporter.start()
    try:
        result = runner(
            cache_db=args.cache_db,
            years=args.years,
            workers=args.workers,
            batch_size=args.batch_size,
            max_rpm=args.max_rpm,
            max_runtime=args.max_runtime,
            feed=args.feed,
            adjustment=args.adjustment,
            dry_run=args.dry_run,
            timeout=args.timeout,
            stop_event=stop_event,
            progress=reporter,
            limiter=limiter,
            **extra,
        )
    except WarmupAlreadyRunningError as exc:
        logger.warning(
            f"{component}_already_running",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        result = {
            "status": "error", "stop_reason": "already_running",
            "error": " ".join(str(exc).split())[:300],
        }
        return result, EXIT_LOCKED
    except Exception as exc:  # noqa: BLE001 - CLI must emit one final JSON object
        logger.exception(
            f"{component}_failed",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        result = {
            "status": "error", "stop_reason": "fatal",
            "error": " ".join(str(exc).split())[:300],
        }
        return result, EXIT_FATAL
    finally:
        reporter.close()
        if can_install_signal and previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)

    if sigint_seen.is_set():
        result["stop_reason"] = "sigint"
        return result, EXIT_SIGINT
    if result.get("stop_reason") in ("complete", "dry_run"):
        return result, EXIT_OK
    return result, EXIT_PARTIAL


def warm_market_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Run warm-market with graceful SIGINT and stable exit-code mapping."""
    return _market_job_command(
        args,
        runner=run_market_warmup,
        component="warm_market",
        extra={"symbols_file": args.symbols_file},
    )


def reconcile_market_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Run asset-catalog reconciliation with warm-market safety semantics."""
    return _market_job_command(
        args,
        runner=run_market_reconcile,
        component="reconcile_market",
        extra={
            "force_assets": args.force_assets,
            "symbols_file": args.symbols_file,
        },
    )


def sync_assets_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Refresh the daily asset catalog under the same database singleton lock."""
    store = MarketDataStore(args.cache_db)
    try:
        with WarmupFileLock(store.path):
            catalog = ensure_daily_asset_catalog(
                store, force=args.force, timeout=args.timeout
            )
        return {"path": str(store.path), "asset_catalog": catalog}, EXIT_OK
    except WarmupAlreadyRunningError as exc:
        logger.warning(
            "asset_catalog_already_running",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return {
            "status": "error", "stop_reason": "already_running",
            "error": " ".join(str(exc).split())[:300],
        }, EXIT_LOCKED
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "asset_catalog_sync_failed",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return {
            "status": "error", "stop_reason": "fatal",
            "error": " ".join(str(exc).split())[:300],
        }, EXIT_FATAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQLite 行情缓存治理")
    parser.add_argument(
        "--cache-db", help="SQLite 文件路径；默认 skill 根目录 data/market-data.sqlite3"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读显示路径、容量、行数、coverage 与 integrity")
    sub.add_parser("integrity-check", help="只读运行 PRAGMA integrity_check")
    sync_assets = sub.add_parser(
        "sync-assets", help="每日同步 Alpaca active US-equity 资产目录"
    )
    sync_assets.add_argument(
        "--force", action="store_true", help="即使今日已成功同步也重新拉取"
    )
    sync_assets.add_argument(
        "--timeout", type=_bounded_int("--timeout", 1, 300), default=30
    )
    for command in ("warm", "refresh"):
        cmd = sub.add_parser(command, help=f"{command} 指定 symbols 的日线与 snapshot")
        cmd.add_argument("--symbols", required=True, help="逗号分隔")
        cmd.add_argument("--start")
        cmd.add_argument("--end")
        cmd.add_argument("--feed", default="iex")
        cmd.add_argument("--adjustment", default="split")
        cmd.add_argument("--timeout", type=int, default=30)
    warm_market = sub.add_parser(
        "warm-market",
        help="Basic-safe 全市场最近三年日线预热；只约束本进程请求预算",
    )
    warm_market.add_argument(
        "--years", type=_bounded_int("--years", 1, 10), default=DEFAULT_YEARS
    )
    warm_market.add_argument(
        "--workers", type=_bounded_int("--workers", 1, 8), default=DEFAULT_WORKERS
    )
    warm_market.add_argument(
        "--batch-size", type=_bounded_int("--batch-size", 1, 200),
        default=DEFAULT_BATCH_SIZE,
    )
    warm_market.add_argument(
        "--max-rpm", type=_bounded_int("--max-rpm", 1, 180),
        default=DEFAULT_MAX_RPM,
        help="共享 bars-page 本进程预算；默认 120，Basic 安全上限 180",
    )
    warm_market.add_argument(
        "--max-runtime", type=_bounded_int("--max-runtime", 1, 86_400),
        default=DEFAULT_MAX_RUNTIME,
        help="维护窗口秒数；到期安全停止并可直接复跑",
    )
    warm_market.add_argument("--feed", choices=("iex",), default="iex")
    warm_market.add_argument("--adjustment", choices=("split",), default="split")
    warm_market.add_argument(
        "--symbols-file", type=_symbols_file,
        help="canary/复现 ticker 文件；启用后跳过全市场 asset discovery",
    )
    warm_market.add_argument(
        "--dry-run", action="store_true",
        help="只规划 universe/coverage/batches，不请求 bars",
    )
    warm_market.add_argument(
        "--timeout", type=_bounded_int("--timeout", 1, 300), default=30
    )
    reconcile = sub.add_parser(
        "reconcile-market",
        help="按每日资产目录审计并补齐全部 IEX-eligible 三年日线 coverage",
    )
    reconcile.add_argument(
        "--years", type=_bounded_int("--years", 1, 10), default=DEFAULT_YEARS
    )
    reconcile.add_argument(
        "--workers", type=_bounded_int("--workers", 1, 8), default=DEFAULT_WORKERS
    )
    reconcile.add_argument(
        "--batch-size", type=_bounded_int("--batch-size", 1, 200),
        default=DEFAULT_BATCH_SIZE,
    )
    reconcile.add_argument(
        "--max-rpm", type=_bounded_int("--max-rpm", 1, 180),
        default=DEFAULT_MAX_RPM,
    )
    reconcile.add_argument(
        "--max-runtime", type=_bounded_int("--max-runtime", 1, 86_400),
        default=DEFAULT_MAX_RUNTIME,
    )
    reconcile.add_argument("--feed", choices=("iex",), default="iex")
    reconcile.add_argument("--adjustment", choices=("split",), default="split")
    reconcile.add_argument(
        "--force-assets", action="store_true",
        help="忽略同日资产目录缓存并强制刷新",
    )
    reconcile.add_argument(
        "--symbols-file", type=_symbols_file,
        help="仅对目录中的指定 ticker 做 canary；默认对全部 eligible 资产",
    )
    reconcile.add_argument(
        "--dry-run", action="store_true",
        help="允许刷新资产目录，但只规划 coverage 缺口，不请求 bars",
    )
    reconcile.add_argument(
        "--timeout", type=_bounded_int("--timeout", 1, 300), default=30
    )
    purge = sub.add_parser("purge", help="按显式范围清理；无范围默认拒绝")
    purge.add_argument("--all", action="store_true", help="清空全部缓存表")
    purge.add_argument("--symbols", help="逗号分隔；可单独作为范围")
    purge.add_argument("--start", help="范围起日；必须与 --end 同时提供")
    purge.add_argument("--end", help="范围止日；必须与 --start 同时提供")
    purge.add_argument("--feed", default="iex")
    purge.add_argument("--adjustment", default="split")
    add_logging_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(level=args.log_level, log_format=args.log_format)
    clear_log_context()
    bind_log_context(command=f"cache_admin.{args.command}")
    if args.command == "status":
        result = status_summary(args.cache_db)
    elif args.command == "integrity-check":
        result = integrity_summary(args.cache_db)
    elif args.command in ("warm", "refresh"):
        result = warm_or_refresh(args, "auto" if args.command == "warm" else "refresh")
    elif args.command == "warm-market":
        result, exit_code = warm_market_command(args)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return exit_code
    elif args.command == "reconcile-market":
        result, exit_code = reconcile_market_command(args)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return exit_code
    elif args.command == "sync-assets":
        result, exit_code = sync_assets_command(args)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return exit_code
    else:
        store = MarketDataStore(args.cache_db)
        result = {
            "path": str(store.path),
            "purged": purge_cache(
                store, purge_all=args.all, symbols=_symbols(args.symbols),
                start_date=args.start, end_date=args.end,
                feed=args.feed, adjustment=args.adjustment,
            ),
        }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "cache_admin_failed",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        json.dump({"status": "error", "error": str(exc)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        raise SystemExit(1)
