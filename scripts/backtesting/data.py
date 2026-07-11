"""Final-only data adapters for deterministic historical research.

``cache-only`` opens SQLite with ``mode=ro`` and never invokes the normal
store initializer, so a backtest cannot create, migrate or write a database.
``auto`` is an explicit opt-in to the production read-through facade.
``input`` consumes a multi-bars JSON artifact and remains hard offline.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from fetching import fetch_bars
from market_data_store import SCHEMA_VERSION, missing_date_ranges, resolve_db_path

from .contracts import WARMUP_LOAD_BUFFER_SESSIONS, WARMUP_SESSIONS


class BacktestDataError(RuntimeError):
    """Structured data-boundary failure surfaced by the CLI."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class DataBundle:
    bars: dict[str, list[dict[str, Any]]]
    mode: str
    feed: str
    adjustment: str
    requested_start: str
    requested_end: str
    load_start: str
    actual_start: str
    actual_end: str
    warmup_sessions: int
    data_hash: str
    coverage: dict[str, dict[str, Any]]
    notes: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()
    db_path: str | None = None

    def provenance(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "feed": self.feed,
            "adjustment": self.adjustment,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "load_start": self.load_start,
            "actual_start": self.actual_start,
            "actual_end": self.actual_end,
            "warmup_sessions": self.warmup_sessions,
            "data_hash": self.data_hash,
            "coverage": self.coverage,
            "notes": list(self.notes),
            "quality_flags": list(self.quality_flags),
            "db_path": self.db_path,
        }


def _date(value: str, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise BacktestDataError("invalid_date", f"{label} 必须是 YYYY-MM-DD", value=value) from exc


def _symbols(values: Iterable[str]) -> list[str]:
    clean = list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))
    if not clean:
        raise BacktestDataError("symbols_empty", "至少需要一个 symbol")
    return clean


def _number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BacktestDataError("bar_invalid", f"{label} 必须是数值", value=value)
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise BacktestDataError("bar_invalid", f"{label} 超出有效范围", value=value)
    return number


def normalize_bars(
    raw: Mapping[str, Sequence[Mapping[str, Any]]], symbols: Iterable[str]
) -> dict[str, list[dict[str, Any]]]:
    """Normalize Alpaca bar objects, reject conflicting sessions and sort."""

    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in _symbols(symbols):
        per_date: dict[str, dict[str, Any]] = {}
        for item in raw.get(symbol, []) or []:
            if not isinstance(item, Mapping):
                raise BacktestDataError("bar_invalid", f"{symbol} bar 必须是对象")
            timestamp = str(item.get("t") or item.get("timestamp") or "")
            try:
                session_date = dt.date.fromisoformat(timestamp[:10]).isoformat()
            except (TypeError, ValueError) as exc:
                raise BacktestDataError(
                    "bar_invalid", f"{symbol} bar 缺少有效时间", timestamp=timestamp
                ) from exc
            o = _number(item.get("o", item.get("open")), f"{symbol}.{session_date}.open")
            h = _number(item.get("h", item.get("high")), f"{symbol}.{session_date}.high")
            low = _number(item.get("l", item.get("low")), f"{symbol}.{session_date}.low")
            c = _number(item.get("c", item.get("close")), f"{symbol}.{session_date}.close")
            v = _number(
                item.get("v", item.get("volume")),
                f"{symbol}.{session_date}.volume",
                allow_zero=True,
            )
            if low > min(o, c) or h < max(o, c) or low > h:
                raise BacktestDataError(
                    "bar_invalid",
                    f"{symbol} {session_date} OHLC 关系无效",
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                )
            bar: dict[str, Any] = {
                "t": timestamp,
                "o": o,
                "h": h,
                "l": low,
                "c": c,
                "v": v,
            }
            vw = item.get("vw", item.get("vwap"))
            n = item.get("n", item.get("trade_count"))
            if isinstance(vw, (int, float)) and not isinstance(vw, bool):
                bar["vw"] = float(vw)
            if isinstance(n, (int, float)) and not isinstance(n, bool):
                bar["n"] = int(n)
            previous = per_date.get(session_date)
            if previous and any(previous[key] != bar[key] for key in ("o", "h", "l", "c", "v")):
                raise BacktestDataError(
                    "bar_conflict", f"{symbol} {session_date} 存在冲突日线"
                )
            per_date[session_date] = bar
        if per_date:
            out[symbol] = [per_date[key] for key in sorted(per_date)]
    return out


def bars_content_hash(
    bars: Mapping[str, Sequence[Mapping[str, Any]]], *, feed: str, adjustment: str
) -> str:
    payload: list[list[Any]] = []
    for symbol in sorted(bars):
        for bar in sorted(bars[symbol], key=lambda row: str(row["t"])):
            payload.append([
                symbol,
                str(bar["t"])[:10],
                bar["o"], bar["h"], bar["l"], bar["c"], bar["v"],
                bar.get("vw"), bar.get("n"),
                feed, adjustment,
            ])
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coverage_summary(
    bars: Mapping[str, Sequence[Mapping[str, Any]]], symbols: Sequence[str]
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        rows = list(bars.get(symbol, []))
        summary[symbol] = {
            "rows": len(rows),
            "earliest": rows[0]["t"][:10] if rows else None,
            "latest": rows[-1]["t"][:10] if rows else None,
        }
    return summary


# A properly split-adjusted series has no jump at the split date; a close-to-
# close ratio in 2:1 split territory therefore signals rows cached under a
# stale adjustment baseline (or a source error), not a real market move.  The
# probe is read-only and warning-only, so moderate false positives on extreme
# real crashes/squeezes are acceptable.
DRIFT_RATIO_LOW = 0.60
DRIFT_RATIO_HIGH = 1.70


def adjustment_drift_flags(
    bars: Mapping[str, Sequence[Mapping[str, Any]]]
) -> list[str]:
    flags: list[str] = []
    for symbol in sorted(bars):
        rows = bars[symbol]
        for index in range(1, len(rows)):
            previous_close = float(rows[index - 1]["c"])
            close = float(rows[index]["c"])
            if not previous_close:
                continue
            ratio = close / previous_close
            if ratio <= DRIFT_RATIO_LOW or ratio >= DRIFT_RATIO_HIGH:
                flags.append(
                    f"possible_adjustment_drift: {symbol} "
                    f"{str(rows[index]['t'])[:10]} close/prev_close={ratio:.3f}；"
                    "疑似缓存复权基准过期，建议 cache_admin refresh 该 symbol"
                )
    return flags


def _validate_warmup(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    symbols: Sequence[str],
    start: str,
    warmup_sessions: int,
) -> None:
    failures: dict[str, int] = {}
    for symbol in symbols:
        count = sum(1 for bar in bars.get(symbol, []) if str(bar["t"])[:10] < start)
        if count < warmup_sessions:
            failures[symbol] = count
    if failures:
        raise BacktestDataError(
            "warmup_insufficient",
            f"评估开始日前需要每个标的至少 {warmup_sessions} 根完成日线",
            counts=failures,
            required=warmup_sessions,
            suggestion=(
                "python3 scripts/cache_admin.py warm --symbols "
                + ",".join(symbols)
                + f" --start {start}"
            ),
        )


def _validate_benchmark_sessions(
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    expected_sessions: Sequence[str] = (),
) -> None:
    """Reject missing SPY/QQQ sessions that would shift the research calendar.

    ``sync_ranges`` records successful request coverage, not row-level
    completeness.  SPY and QQQ are liquid calendar anchors, so their union plus
    any cached XNYS sessions is the minimum set both benchmarks must contain.
    The research symbol is intentionally excluded: its missing sessions are
    handled by replay exclusions and terminal-event semantics.
    """

    benchmark_dates = {
        symbol: {str(bar["t"])[:10] for bar in bars.get(symbol, [])}
        for symbol in ("SPY", "QQQ")
    }
    expected = {str(value)[:10] for value in expected_sessions if str(value)}
    expected.update(benchmark_dates["SPY"])
    expected.update(benchmark_dates["QQQ"])
    missing = {
        symbol: sorted(expected - dates)
        for symbol, dates in benchmark_dates.items()
        if expected - dates
    }
    if missing:
        raise BacktestDataError(
            "benchmark_session_gap",
            "SPY/QQQ 存在已完成交易日日线缺口，无法保持回测窗口语义",
            missing_sessions=missing,
        )


def _bundle(
    raw: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
    mode: str,
    feed: str,
    adjustment: str,
    start: str,
    end: str,
    load_start: str,
    warmup_sessions: int,
    notes: Sequence[str] = (),
    db_path: str | None = None,
    coverage_extra: Mapping[str, Mapping[str, Any]] | None = None,
    benchmark_sessions: Sequence[str] = (),
) -> DataBundle:
    normalized = normalize_bars(raw, symbols)
    bars = {
        symbol: [
            bar for bar in normalized.get(symbol, [])
            if load_start <= str(bar["t"])[:10] <= end
        ]
        for symbol in symbols
    }
    missing = [symbol for symbol in symbols if not bars.get(symbol)]
    if missing:
        raise BacktestDataError("symbols_missing", "核心标的缺少日线", symbols=missing)
    _validate_benchmark_sessions(bars, benchmark_sessions)
    _validate_warmup(bars, symbols, start, warmup_sessions)
    spy_eval = [bar["t"][:10] for bar in bars["SPY"] if start <= bar["t"][:10] <= end]
    if not spy_eval:
        raise BacktestDataError("evaluation_empty", "评估区间没有 SPY 完成交易日")
    actual_start, actual_end = spy_eval[0], spy_eval[-1]
    quality_flags = adjustment_drift_flags(bars)
    coverage = _coverage_summary(bars, symbols)
    for symbol, extra in (coverage_extra or {}).items():
        coverage.setdefault(symbol, {}).update(extra)
    return DataBundle(
        bars=bars,
        mode=mode,
        feed=feed,
        adjustment=adjustment,
        requested_start=start,
        requested_end=end,
        load_start=load_start,
        actual_start=actual_start,
        actual_end=actual_end,
        warmup_sessions=warmup_sessions,
        data_hash=bars_content_hash(bars, feed=feed, adjustment=adjustment),
        coverage=coverage,
        notes=tuple(str(note) for note in notes if note),
        quality_flags=tuple(quality_flags),
        db_path=db_path,
    )


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()))}?mode=ro"


def _cached_completed_end(conn: sqlite3.Connection, requested_end: str) -> str | None:
    """Return the latest completed cached XNYS session on/before the request."""

    now = dt.datetime.now(dt.timezone.utc)
    grace = dt.timedelta(minutes=15)
    rows = conn.execute(
        """
        SELECT session_date, close_at FROM market_sessions
        WHERE session_date<=? ORDER BY session_date DESC
        """,
        (requested_end,),
    ).fetchall()
    for row in rows:
        try:
            close_at = dt.datetime.fromisoformat(
                str(row["close_at"]).replace("Z", "+00:00")
            )
            if close_at.tzinfo is None:
                close_at = close_at.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if close_at.astimezone(dt.timezone.utc) + grace <= now:
            return str(row["session_date"])
    return None


def _load_cache_only(
    symbols: Sequence[str],
    start: str,
    end: str,
    *,
    feed: str,
    adjustment: str,
    cache_db: str | None,
    warmup_sessions: int,
) -> DataBundle:
    path = resolve_db_path(cache_db)
    if not path.is_file():
        raise BacktestDataError("cache_missing", "SQLite 行情库不存在", path=str(path))
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise BacktestDataError(
                "cache_schema_incompatible",
                "cache-only 不执行 schema 迁移",
                found=version,
                required=SCHEMA_VERSION,
                suggestion="先运行 python3 scripts/cache_admin.py status，再由管理员入口完成迁移",
            )
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"daily_bars", "sync_ranges", "market_sessions"}.issubset(tables):
            raise BacktestDataError("cache_schema_incompatible", "SQLite 缺少回测所需表")
        completed_end = _cached_completed_end(conn, end)
        query_end = min(end, completed_end) if completed_end else end
        prior = conn.execute(
            """
            SELECT session_date FROM daily_bars
            WHERE symbol='SPY' AND feed=? AND adjustment=? AND session_date<?
            ORDER BY session_date DESC LIMIT ?
            """,
            (feed, adjustment, start, warmup_sessions + WARMUP_LOAD_BUFFER_SESSIONS),
        ).fetchall()
        if len(prior) < warmup_sessions:
            raise BacktestDataError(
                "warmup_insufficient",
                f"SPY 在评估开始日前只有 {len(prior)} 根缓存日线",
                required=warmup_sessions,
                path=str(path),
            )
        load_start = min(str(row["session_date"]) for row in prior)
        covered: dict[str, list[tuple[str, str]]] = {symbol: [] for symbol in symbols}
        marks = ",".join("?" for _ in symbols)
        for row in conn.execute(
            f"""
            SELECT symbol, start_date, end_date FROM sync_ranges
            WHERE symbol IN ({marks}) AND feed=? AND adjustment=?
            """,
            [*symbols, feed, adjustment],
        ).fetchall():
            covered[str(row["symbol"])].append((str(row["start_date"]), str(row["end_date"])))
        gaps = {
            symbol: missing_date_ranges(covered[symbol], load_start, query_end)
            for symbol in symbols
        }
        missing_coverage = {symbol: ranges for symbol, ranges in gaps.items() if ranges}
        if missing_coverage:
            raise BacktestDataError(
                "cache_coverage_incomplete",
                "SQLite coverage 未覆盖完整回测范围",
                gaps=missing_coverage,
                load_start=load_start,
                end=query_end,
                suggestion=(
                    "python3 scripts/cache_admin.py warm --symbols "
                    + ",".join(symbols)
                    + f" --start {load_start} --end {query_end}"
                ),
            )
        rows = conn.execute(
            f"""
            SELECT symbol, session_date, open, high, low, close, volume,
                   vwap, trade_count, source_timestamp
            FROM daily_bars
            WHERE symbol IN ({marks}) AND session_date BETWEEN ? AND ?
              AND feed=? AND adjustment=?
            ORDER BY symbol, session_date
            """,
            [*symbols, load_start, query_end, feed, adjustment],
        ).fetchall()
        benchmark_sessions = [
            str(row["session_date"])
            for row in conn.execute(
                """
                SELECT session_date FROM market_sessions
                WHERE session_date BETWEEN ? AND ?
                ORDER BY session_date
                """,
                (load_start, query_end),
            ).fetchall()
        ]
        raw: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for row in rows:
            raw[str(row["symbol"])].append({
                "t": str(row["source_timestamp"]),
                "o": row["open"], "h": row["high"], "l": row["low"],
                "c": row["close"], "v": row["volume"],
                "vw": row["vwap"], "n": row["trade_count"],
            })
        return _bundle(
            raw,
            symbols=symbols,
            mode="cache-only",
            feed=feed,
            adjustment=adjustment,
            start=start,
            end=end,
            load_start=load_start,
            warmup_sessions=warmup_sessions,
            notes=(
                [f"requested end {end} clamped to completed session {query_end}"]
                if query_end != end else []
            ),
            db_path=str(path),
            coverage_extra={
                symbol: {
                    "missing_ranges": [],
                    **({"effective_end": query_end} if query_end != end else {}),
                }
                for symbol in symbols
            },
            benchmark_sessions=benchmark_sessions,
        )
    except sqlite3.Error as exc:
        raise BacktestDataError("cache_read_failed", "SQLite 只读加载失败", reason=str(exc)) from exc
    finally:
        if "conn" in locals():
            conn.close()


def _read_input(path: str) -> Mapping[str, Sequence[Mapping[str, Any]]]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestDataError("input_invalid", "无法读取 bars 输入", reason=str(exc)) from exc
    bars = payload.get("bars") if isinstance(payload, Mapping) else None
    if not isinstance(bars, Mapping):
        raise BacktestDataError("input_invalid", "输入 JSON 缺少 bars 对象")
    return bars


def _calendar_load_start(
    raw: Mapping[str, Sequence[Mapping[str, Any]]], start: str, warmup_sessions: int
) -> str:
    spy = normalize_bars(raw, ["SPY"]).get("SPY", [])
    prior = [bar["t"][:10] for bar in spy if bar["t"][:10] < start]
    if len(prior) < warmup_sessions:
        raise BacktestDataError(
            "warmup_insufficient",
            f"SPY 在评估开始日前只有 {len(prior)} 根输入日线",
            required=warmup_sessions,
        )
    return prior[-min(len(prior), warmup_sessions + WARMUP_LOAD_BUFFER_SESSIONS)]


def load_backtest_data(
    symbols: Iterable[str],
    start: str,
    end: str,
    *,
    mode: str = "cache-only",
    feed: str = "iex",
    adjustment: str = "split",
    cache_db: str | None = None,
    input_path: str | None = None,
    warmup_sessions: int = WARMUP_SESSIONS,
    timeout: int = 60,
) -> DataBundle:
    clean = _symbols(symbols)
    start, end = _date(start, "start"), _date(end, "end")
    if start > end:
        raise BacktestDataError("invalid_range", "start 不能晚于 end")
    if warmup_sessions < WARMUP_SESSIONS:
        raise BacktestDataError(
            "invalid_warmup", f"warmup_sessions 不能少于 {WARMUP_SESSIONS}"
        )
    mode = str(mode).strip().lower()
    if mode == "cache-only":
        return _load_cache_only(
            clean, start, end,
            feed=feed, adjustment=adjustment, cache_db=cache_db,
            warmup_sessions=warmup_sessions,
        )
    if mode == "input":
        if not input_path:
            raise BacktestDataError("input_required", "input 模式必须提供 --input")
        raw = _read_input(input_path)
        load_start = _calendar_load_start(raw, start, warmup_sessions)
        return _bundle(
            raw,
            symbols=clean,
            mode="input",
            feed=feed,
            adjustment=adjustment,
            start=start,
            end=end,
            load_start=load_start,
            warmup_sessions=warmup_sessions,
        )
    if mode == "auto":
        load_start = (dt.date.fromisoformat(start) - dt.timedelta(days=max(730, warmup_sessions * 3))).isoformat()
        raw, used_feed, note = fetch_bars(
            clean, load_start, end, feed, adjustment, 10000, timeout,
            cache_mode="auto", cache_db=cache_db,
        )
        normalized = normalize_bars(raw, clean)
        exact_load_start = _calendar_load_start(normalized, start, warmup_sessions)
        trimmed = {
            symbol: [bar for bar in rows if exact_load_start <= bar["t"][:10] <= end]
            for symbol, rows in normalized.items()
        }
        return _bundle(
            trimmed,
            symbols=clean,
            mode="auto",
            feed=used_feed,
            adjustment=adjustment,
            start=start,
            end=end,
            load_start=exact_load_start,
            warmup_sessions=warmup_sessions,
            notes=[note] if note else [],
            db_path=str(resolve_db_path(cache_db)),
        )
    raise BacktestDataError(
        "invalid_data_source", "data source 必须是 cache-only、auto 或 input", mode=mode
    )
