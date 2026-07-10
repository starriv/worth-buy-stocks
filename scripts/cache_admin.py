#!/usr/bin/env python3
"""Inspect, warm, refresh, verify, and safely purge the local market-data cache."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(__file__))
from fetching import fetch_bars, fetch_snapshots  # noqa: E402
from market_data_store import MarketDataStore, resolve_db_path  # noqa: E402


TABLES = (
    "daily_bars", "live_daily_bars", "latest_snapshots",
    "sync_ranges", "market_sessions", "feed_fallbacks",
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQLite 行情缓存治理")
    parser.add_argument(
        "--cache-db", help="SQLite 文件路径；默认 skill 根目录 data/market-data.sqlite3"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读显示路径、容量、行数、coverage 与 integrity")
    sub.add_parser("integrity-check", help="只读运行 PRAGMA integrity_check")
    for command in ("warm", "refresh"):
        cmd = sub.add_parser(command, help=f"{command} 指定 symbols 的日线与 snapshot")
        cmd.add_argument("--symbols", required=True, help="逗号分隔")
        cmd.add_argument("--start")
        cmd.add_argument("--end")
        cmd.add_argument("--feed", default="iex")
        cmd.add_argument("--adjustment", default="split")
        cmd.add_argument("--timeout", type=int, default=30)
    purge = sub.add_parser("purge", help="按显式范围清理；无范围默认拒绝")
    purge.add_argument("--all", action="store_true", help="清空全部缓存表")
    purge.add_argument("--symbols", help="逗号分隔；可单独作为范围")
    purge.add_argument("--start", help="范围起日；必须与 --end 同时提供")
    purge.add_argument("--end", help="范围止日；必须与 --start 同时提供")
    purge.add_argument("--feed", default="iex")
    purge.add_argument("--adjustment", default="split")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        result = status_summary(args.cache_db)
    elif args.command == "integrity-check":
        result = integrity_summary(args.cache_db)
    elif args.command in ("warm", "refresh"):
        result = warm_or_refresh(args, "auto" if args.command == "warm" else "refresh")
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
        json.dump({"status": "error", "error": str(exc)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        raise SystemExit(1)
