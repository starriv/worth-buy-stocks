#!/usr/bin/env python3
"""SQLite repository for completed daily bars and same-session market data.

This module owns persistence only.  It deliberately does not decide whether the
market is open or whether a remote refresh is required; that read-through policy
lives in ``market_data_cache.py`` (task-016).  Keeping this store below the
cache-policy layer lets ``pipeline.py`` and ``scoring/`` remain deterministic
and unaware of SQLite.

The default database is ``<skill-root>/data/market-data.sqlite3``.  Tests and
operators may override it with ``WORTH_BUY_STOCKS_DB`` or an explicit path.
Only public market data and synchronization metadata are stored here.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any


try:
    from zoneinfo import ZoneInfo
    _MARKET_TZ: Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - hosts without tzdata degrade to UTC dates
    _MARKET_TZ = None

SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000
ENV_DB_PATH = "WORTH_BUY_STOCKS_DB"
SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = SKILL_ROOT / "data" / "market-data.sqlite3"
_SYMBOL_CHUNK = 400
ASSET_CATALOG = "alpaca_us_equity_active"
IEX_ELIGIBLE_EXCHANGES = frozenset({"AMEX", "ARCA", "BATS", "NASDAQ", "NYSE"})
_SENSITIVE_PAYLOAD_KEYS = {
    "api_key", "apikey", "secret", "token", "access_token", "refresh_token",
    "account_id", "chat_id", "password", "authorization",
}


class MarketDataCacheError(RuntimeError):
    """Base class for cache failures that callers may safely degrade around."""


class CacheSchemaError(MarketDataCacheError):
    """The database schema is newer than this code or failed migration."""


class CacheBusyError(MarketDataCacheError):
    """SQLite remained locked beyond the configured busy timeout."""


# latest_snapshots stores KB-sized JSON payloads; SQLite recommends WITHOUT ROWID
# only for rows far below the page size, so it stays an ordinary rowid table.
_LATEST_SNAPSHOTS_DDL = """
    CREATE TABLE IF NOT EXISTS latest_snapshots (
        symbol TEXT NOT NULL,
        feed TEXT NOT NULL,
        session_date TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        normalized_payload_json TEXT NOT NULL,
        PRIMARY KEY (symbol, feed)
    )
    """
_LATEST_SNAPSHOTS_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_latest_snapshots_session
    ON latest_snapshots (session_date, symbol)
    """
_FEED_FALLBACKS_DDL = """
    CREATE TABLE IF NOT EXISTS feed_fallbacks (
        requested_feed TEXT NOT NULL PRIMARY KEY,
        actual_feed TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) WITHOUT ROWID
    """

_ASSETS_DDL = """
    CREATE TABLE IF NOT EXISTS assets (
        asset_id TEXT NOT NULL PRIMARY KEY,
        symbol TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        exchange TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        tradable INTEGER NOT NULL CHECK (tradable IN (0, 1)),
        fractionable INTEGER NOT NULL CHECK (fractionable IN (0, 1)),
        marginable INTEGER NOT NULL CHECK (marginable IN (0, 1)),
        shortable INTEGER NOT NULL CHECK (shortable IN (0, 1)),
        easy_to_borrow INTEGER NOT NULL CHECK (easy_to_borrow IN (0, 1)),
        is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
        data_eligible INTEGER NOT NULL CHECK (data_eligible IN (0, 1)),
        excluded_reason TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    ) WITHOUT ROWID
    """
_ASSETS_SYMBOL_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_assets_symbol ON assets (symbol, is_current)
    """
_ASSETS_ELIGIBLE_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_assets_eligible
    ON assets (is_current, data_eligible, symbol)
    """
_ASSET_CATALOG_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS asset_catalog_state (
        catalog TEXT NOT NULL PRIMARY KEY,
        synced_at TEXT NOT NULL,
        source_count INTEGER NOT NULL,
        current_count INTEGER NOT NULL,
        tradable_count INTEGER NOT NULL,
        eligible_count INTEGER NOT NULL,
        unsupported_count INTEGER NOT NULL
    ) WITHOUT ROWID
    """
_ASSET_SCHEMA = (
    _ASSETS_DDL,
    _ASSETS_SYMBOL_INDEX_DDL,
    _ASSETS_ELIGIBLE_INDEX_DDL,
    _ASSET_CATALOG_STATE_DDL,
)

_SCHEMA_V2 = (
    """
    CREATE TABLE IF NOT EXISTS daily_bars (
        symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,
        feed TEXT NOT NULL,
        adjustment TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL,
        vwap REAL,
        trade_count INTEGER,
        source_timestamp TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (symbol, session_date, feed, adjustment)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_daily_bars_date
    ON daily_bars (session_date, symbol)
    """,
    """
    CREATE TABLE IF NOT EXISTS live_daily_bars (
        symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,
        feed TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL,
        vwap REAL,
        trade_count INTEGER,
        source_timestamp TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        PRIMARY KEY (symbol, session_date, feed)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_live_daily_bars_date
    ON live_daily_bars (session_date, symbol)
    """,
    _LATEST_SNAPSHOTS_DDL,
    _LATEST_SNAPSHOTS_INDEX_DDL,
    """
    CREATE TABLE IF NOT EXISTS sync_ranges (
        symbol TEXT NOT NULL,
        feed TEXT NOT NULL,
        adjustment TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (symbol, feed, adjustment, start_date, end_date)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS market_sessions (
        session_date TEXT NOT NULL PRIMARY KEY,
        open_at TEXT NOT NULL,
        close_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    _FEED_FALLBACKS_DDL,
)

_SCHEMA_V3 = (*_SCHEMA_V2, *_ASSET_SCHEMA)

# v1 shipped a duplicate index over the sync_ranges primary key and stored
# snapshot JSON in a WITHOUT ROWID table; v2 also adds feed_fallbacks.
_MIGRATE_V1_TO_V2 = (
    "DROP INDEX IF EXISTS idx_sync_ranges_lookup",
    "DROP INDEX IF EXISTS idx_latest_snapshots_session",
    "ALTER TABLE latest_snapshots RENAME TO latest_snapshots_v1",
    _LATEST_SNAPSHOTS_DDL,
    """
    INSERT INTO latest_snapshots (
        symbol, feed, session_date, captured_at, normalized_payload_json
    )
    SELECT symbol, feed, session_date, captured_at, normalized_payload_json
    FROM latest_snapshots_v1
    """,
    "DROP TABLE latest_snapshots_v1",
    _LATEST_SNAPSHOTS_INDEX_DDL,
    _FEED_FALLBACKS_DDL,
)
_MIGRATE_V2_TO_V3 = _ASSET_SCHEMA


def resolve_db_path(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit path -> environment override -> skill ``data/`` default."""
    if path is not None:
        return Path(path).expanduser().resolve()
    env = os.environ if environ is None else environ
    override = str(env.get(ENV_DB_PATH) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_DB_PATH.resolve()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(value: str, label: str = "date") -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD): {value!r}") from exc


def _date_text(value: str, label: str = "date") -> str:
    return _parse_date(value, label).isoformat()


def _session_date_from_value(value: Any) -> str | None:
    """Plain dates pass through; aware timestamps map to their ET session date."""
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and _MARKET_TZ is not None:
            return parsed.astimezone(_MARKET_TZ).date().isoformat()
    try:
        return _date_text(text[:10], "snapshot session date")
    except ValueError:
        return None


def _clean_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("symbol must not be empty")
    return symbol


def _clean_symbols(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        symbol = _clean_symbol(value)
        if symbol not in out:
            out.append(symbol)
    return out


def _clean_namespace(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError(f"{label} must not be empty")
    return text


def asset_data_eligibility(asset: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Classify whether an Alpaca asset can be reconciled through Basic IEX bars."""
    status = str(asset.get("status") or "").strip().lower()
    asset_class = str(asset.get("class") or asset.get("asset_class") or "").strip().lower()
    exchange = str(asset.get("exchange") or "").strip().upper()
    symbol = str(asset.get("symbol") or "").strip().upper()
    if status != "active":
        return False, "inactive"
    if asset_class != "us_equity":
        return False, "unsupported_asset_class"
    if not bool(asset.get("tradable")):
        return False, "not_tradable"
    if exchange == "OTC":
        return False, "unsupported_iex_otc"
    if exchange not in IEX_ELIGIBLE_EXCHANGES:
        return False, "unsupported_exchange"
    if not symbol or "," in symbol or any(char.isspace() for char in symbol):
        return False, "unsafe_symbol_format"
    return True, None


def _normalize_asset_row(asset: Mapping[str, Any], seen_at: str) -> tuple[Any, ...]:
    if not isinstance(asset, Mapping):
        raise ValueError("asset catalog entry must be an object")
    asset_id = str(asset.get("id") or asset.get("asset_id") or "").strip()
    symbol = str(asset.get("symbol") or "").strip().upper()
    if not asset_id or not symbol:
        raise ValueError("asset catalog entry requires id and symbol")
    normalized = {
        **asset,
        "symbol": symbol,
        "class": str(asset.get("class") or asset.get("asset_class") or "").strip().lower(),
        "exchange": str(asset.get("exchange") or "").strip().upper(),
        "status": str(asset.get("status") or "").strip().lower(),
    }
    eligible, reason = asset_data_eligibility(normalized)
    return (
        asset_id,
        symbol,
        normalized["class"],
        normalized["exchange"],
        str(asset.get("name") or "").strip(),
        normalized["status"],
        int(bool(asset.get("tradable"))),
        int(bool(asset.get("fractionable"))),
        int(bool(asset.get("marginable"))),
        int(bool(asset.get("shortable"))),
        int(bool(asset.get("easy_to_borrow"))),
        1,
        int(eligible),
        reason,
        seen_at,
        seen_at,
    )


def _first_present(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _reject_sensitive_payload(value: Any, path: str = "snapshot") -> None:
    """Reject accidental credential-like fields before serializing snapshots."""
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _SENSITIVE_PAYLOAD_KEYS:
                raise ValueError(f"{path} contains forbidden credential field {raw_key!r}")
            _reject_sensitive_payload(child, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_sensitive_payload(child, f"{path}[{index}]")


def _number(value: Any, label: str, *, required: bool = False) -> float | int | None:
    if value is None:
        if required:
            raise ValueError(f"bar is missing {label}")
        return None
    if isinstance(value, bool):
        raise ValueError(f"bar {label} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bar {label} must be numeric: {value!r}") from exc


def _normalize_bar(bar: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize Alpaca short keys or normalized snapshot daily-bar keys."""
    if not isinstance(bar, Mapping):
        raise ValueError("bar must be an object")
    nested = bar.get("daily_bar")
    if isinstance(nested, Mapping):
        bar = nested

    timestamp = _first_present(bar, "t", "source_timestamp", "timestamp", "date")
    if timestamp is None:
        raise ValueError("bar is missing timestamp/date")
    timestamp_text = str(timestamp)
    session_value = _first_present(bar, "session_date", "date") or timestamp_text[:10]
    session_date = _date_text(str(session_value), "bar session date")

    trade_count = _first_present(bar, "n", "trade_count")
    if trade_count is not None:
        try:
            trade_count = int(trade_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bar trade_count must be an integer: {trade_count!r}") from exc

    return {
        "session_date": session_date,
        "source_timestamp": timestamp_text,
        "open": _number(_first_present(bar, "o", "open"), "open", required=True),
        "high": _number(_first_present(bar, "h", "high"), "high", required=True),
        "low": _number(_first_present(bar, "l", "low"), "low", required=True),
        "close": _number(_first_present(bar, "c", "close"), "close", required=True),
        "volume": _number(_first_present(bar, "v", "volume"), "volume"),
        "vwap": _number(_first_present(bar, "vw", "vwap"), "vwap"),
        "trade_count": trade_count,
    }


def _bar_from_row(row: sqlite3.Row, timestamp_column: str) -> dict[str, Any]:
    bar: dict[str, Any] = {
        "t": row[timestamp_column],
        "o": row["open"],
        "h": row["high"],
        "l": row["low"],
        "c": row["close"],
    }
    if row["volume"] is not None:
        bar["v"] = row["volume"]
    if row["vwap"] is not None:
        bar["vw"] = row["vwap"]
    if row["trade_count"] is not None:
        bar["n"] = row["trade_count"]
    return bar


def merge_date_ranges(ranges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge inclusive overlapping or adjacent calendar-date ranges."""
    parsed = sorted((_parse_date(a, "range start"), _parse_date(b, "range end")) for a, b in ranges)
    for start, end in parsed:
        if start > end:
            raise ValueError(f"range start {start} is after end {end}")
    merged: list[tuple[dt.date, dt.date]] = []
    one_day = dt.timedelta(days=1)
    for start, end in parsed:
        if not merged or start > merged[-1][1] + one_day:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return [(a.isoformat(), b.isoformat()) for a, b in merged]


def missing_date_ranges(
    covered: Iterable[tuple[str, str]], start_date: str, end_date: str
) -> list[tuple[str, str]]:
    """Return inclusive calendar-date gaps within ``start_date..end_date``."""
    wanted_start = _parse_date(start_date, "requested start")
    wanted_end = _parse_date(end_date, "requested end")
    if wanted_start > wanted_end:
        raise ValueError("requested start is after requested end")
    merged = merge_date_ranges(covered)
    cursor = wanted_start
    one_day = dt.timedelta(days=1)
    missing: list[tuple[str, str]] = []
    for raw_start, raw_end in merged:
        current_start = max(_parse_date(raw_start), wanted_start)
        current_end = min(_parse_date(raw_end), wanted_end)
        if current_end < wanted_start or current_start > wanted_end:
            continue
        if current_start > cursor:
            missing.append((cursor.isoformat(), (current_start - one_day).isoformat()))
        cursor = max(cursor, current_end + one_day)
        if cursor > wanted_end:
            break
    if cursor <= wanted_end:
        missing.append((cursor.isoformat(), wanted_end.isoformat()))
    return missing


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


class MarketDataStore:
    """Connection-per-operation SQLite repository safe for scanner worker threads."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now_fn: Callable[[], dt.datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if int(busy_timeout_ms) < 0:
            raise ValueError("busy_timeout_ms must be >= 0")
        self.path = resolve_db_path(path, environ=environ)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._now_fn = now_fn or _utc_now
        self._init_lock = threading.Lock()
        self._initialized = False

    def now_utc(self) -> dt.datetime:
        value = self._now_fn()
        if not isinstance(value, dt.datetime):
            raise TypeError("now_fn must return datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def _now_text(self) -> str:
        return _iso_utc(self.now_utc())

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def _open_raw(self, *, set_wal: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if set_wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn: sqlite3.Connection | None = None
            try:
                self._prepare_path()
                conn = self._open_raw(set_wal=True)
                self._migrate(conn)
            except sqlite3.Error as exc:
                raise self._translate_sqlite_error(exc) from exc
            finally:
                if conn is not None:
                    conn.close()
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._initialized = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise CacheSchemaError(
                f"market-data cache schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            statements = _SCHEMA_V3
        elif version == 1:
            statements = (*_MIGRATE_V1_TO_V2, *_MIGRATE_V2_TO_V3)
        elif version == 2:
            statements = _MIGRATE_V2_TO_V3
        else:
            raise CacheSchemaError(f"no migration path from schema v{version}")
        try:
            with _transaction(conn):
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise CacheSchemaError(
                f"failed to migrate market-data cache to v{SCHEMA_VERSION}: {exc}"
            ) from exc

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure_initialized()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_raw()
            yield conn
        except sqlite3.Error as exc:
            raise self._translate_sqlite_error(exc) from exc
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _translate_sqlite_error(exc: sqlite3.Error) -> MarketDataCacheError:
        text = str(exc)
        if isinstance(exc, sqlite3.OperationalError) and (
            "locked" in text.lower() or "busy" in text.lower()
        ):
            return CacheBusyError(f"market-data cache remained locked: {text}")
        return MarketDataCacheError(f"market-data cache failure: {text}")

    def schema_version(self) -> int:
        with self.connection() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def table_names(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def integrity_check(self) -> str:
        with self.connection() as conn:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])

    def upsert_daily_bars(
        self,
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        feed: str,
        adjustment: str,
        fetched_at: str | None = None,
        finalize_live: bool = True,
    ) -> int:
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        fetched_at = str(fetched_at or self._now_text())
        rows, finalized = self._daily_rows(bars, feed, adjustment, fetched_at)
        if not rows:
            return 0
        with self.connection() as conn, _transaction(conn):
            self._upsert_daily_rows(conn, rows, finalized if finalize_live else [])
        return len(rows)

    @staticmethod
    def _daily_rows(
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        feed: str,
        adjustment: str,
        fetched_at: str,
    ) -> tuple[list[tuple[Any, ...]], list[tuple[str, str, str]]]:
        rows: list[tuple[Any, ...]] = []
        finalized: list[tuple[str, str, str]] = []
        for raw_symbol, symbol_bars in (bars or {}).items():
            symbol = _clean_symbol(raw_symbol)
            for raw_bar in symbol_bars or []:
                bar = _normalize_bar(raw_bar)
                rows.append((
                    symbol, bar["session_date"], feed, adjustment,
                    bar["open"], bar["high"], bar["low"], bar["close"],
                    bar["volume"], bar["vwap"], bar["trade_count"],
                    bar["source_timestamp"], fetched_at,
                ))
                finalized.append((symbol, bar["session_date"], feed))
        return rows, finalized

    @staticmethod
    def _upsert_daily_rows(
        conn: sqlite3.Connection,
        rows: Sequence[tuple[Any, ...]],
        finalized: Sequence[tuple[str, str, str]],
    ) -> None:
        conn.executemany("""
            INSERT INTO daily_bars (
                symbol, session_date, feed, adjustment, open, high, low, close,
                volume, vwap, trade_count, source_timestamp, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, session_date, feed, adjustment) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, vwap=excluded.vwap,
                trade_count=excluded.trade_count,
                source_timestamp=excluded.source_timestamp,
                fetched_at=excluded.fetched_at
        """, rows)
        if finalized:
            conn.executemany(
                "DELETE FROM live_daily_bars WHERE symbol=? AND session_date=? AND feed=?",
                list(dict.fromkeys(finalized)),
            )

    def get_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        start_date: str,
        end_date: str,
        feed: str,
        adjustment: str,
    ) -> dict[str, list[dict[str, Any]]]:
        clean = _clean_symbols(symbols)
        if not clean:
            return {}
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        found: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in clean}
        with self.connection() as conn:
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT * FROM daily_bars
                    WHERE symbol IN ({marks}) AND session_date BETWEEN ? AND ?
                      AND feed=? AND adjustment=?
                    ORDER BY symbol, session_date
                    """,
                    [*chunk, start_date, end_date, feed, adjustment],
                ).fetchall()
                for row in rows:
                    found[str(row["symbol"])].append(_bar_from_row(row, "source_timestamp"))
        return {symbol: found[symbol] for symbol in clean if found[symbol]}

    def get_latest_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        feed: str,
        adjustment: str,
        on_or_before: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return one latest completed bar per symbol from the final-only table."""
        clean = _clean_symbols(symbols)
        if not clean:
            return {}
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        if on_or_before is not None:
            on_or_before = _date_text(on_or_before, "on_or_before")
        result: dict[str, dict[str, Any]] = {}
        with self.connection() as conn:
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                date_clause = " AND session_date<=?" if on_or_before else ""
                params: list[Any] = [*chunk, feed, adjustment]
                if on_or_before:
                    params.append(on_or_before)
                params.extend([feed, adjustment])
                rows = conn.execute(
                    f"""
                    SELECT bars.* FROM daily_bars AS bars
                    JOIN (
                        SELECT symbol, MAX(session_date) AS session_date
                        FROM daily_bars
                        WHERE symbol IN ({marks}) AND feed=? AND adjustment=?{date_clause}
                        GROUP BY symbol
                    ) AS latest
                      ON latest.symbol = bars.symbol
                     AND latest.session_date = bars.session_date
                    WHERE bars.feed=? AND bars.adjustment=?
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    result[str(row["symbol"])] = _bar_from_row(row, "source_timestamp")
        return {symbol: result[symbol] for symbol in clean if symbol in result}

    def delete_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        start_date: str,
        end_date: str,
        feed: str,
        adjustment: str,
    ) -> int:
        """Delete a completed-bar range before an authoritative full refresh."""
        clean = _clean_symbols(symbols)
        if not clean:
            return 0
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        total = 0
        with self.connection() as conn, _transaction(conn):
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"""
                    DELETE FROM daily_bars
                    WHERE symbol IN ({marks}) AND session_date BETWEEN ? AND ?
                      AND feed=? AND adjustment=?
                    """,
                    [*chunk, start_date, end_date, feed, adjustment],
                )
                total += cur.rowcount
        return total

    def upsert_live_daily_bars(
        self,
        bars: Mapping[str, Mapping[str, Any]],
        *,
        feed: str,
        captured_at: str | None = None,
    ) -> int:
        feed = _clean_namespace(feed, "feed")
        captured_at = str(captured_at or self._now_text())
        rows = self._live_rows(bars, feed, captured_at)
        if not rows:
            return 0
        with self.connection() as conn, _transaction(conn):
            self._upsert_live_rows(conn, rows)
        return len(rows)

    @staticmethod
    def _live_rows(
        bars: Mapping[str, Mapping[str, Any]], feed: str, captured_at: str
    ) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for raw_symbol, raw_bar in (bars or {}).items():
            symbol = _clean_symbol(raw_symbol)
            bar = _normalize_bar(raw_bar)
            rows.append((
                symbol, bar["session_date"], feed,
                bar["open"], bar["high"], bar["low"], bar["close"],
                bar["volume"], bar["vwap"], bar["trade_count"],
                bar["source_timestamp"], captured_at,
            ))
        return rows

    @staticmethod
    def _upsert_live_rows(conn: sqlite3.Connection, rows: Sequence[tuple[Any, ...]]) -> None:
        conn.executemany(
            """
            INSERT INTO live_daily_bars (
                symbol, session_date, feed, open, high, low, close, volume,
                vwap, trade_count, source_timestamp, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, session_date, feed) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, vwap=excluded.vwap,
                trade_count=excluded.trade_count,
                source_timestamp=excluded.source_timestamp,
                captured_at=excluded.captured_at
            """,
            rows,
        )

    def get_live_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        feed: str,
        session_date: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        clean = _clean_symbols(symbols)
        if not clean:
            return {}
        feed = _clean_namespace(feed, "feed")
        if session_date is not None:
            session_date = _date_text(session_date, "session_date")
        result: dict[str, dict[str, Any]] = {}
        with self.connection() as conn:
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                date_clause = " AND session_date=?" if session_date else ""
                params: list[Any] = [*chunk, feed]
                if session_date:
                    params.append(session_date)
                rows = conn.execute(
                    f"""
                    SELECT * FROM live_daily_bars
                    WHERE symbol IN ({marks}) AND feed=?{date_clause}
                    ORDER BY session_date DESC
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    symbol = str(row["symbol"])
                    if symbol not in result:
                        result[symbol] = _bar_from_row(row, "source_timestamp")
        return {symbol: result[symbol] for symbol in clean if symbol in result}

    def delete_live_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        feed: str,
        session_date: str | None = None,
    ) -> int:
        clean = _clean_symbols(symbols)
        if not clean:
            return 0
        feed = _clean_namespace(feed, "feed")
        if session_date is not None:
            session_date = _date_text(session_date, "session_date")
        total = 0
        with self.connection() as conn, _transaction(conn):
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                clause = " AND session_date=?" if session_date else ""
                params: list[Any] = [*chunk, feed]
                if session_date:
                    params.append(session_date)
                cur = conn.execute(
                    f"DELETE FROM live_daily_bars WHERE symbol IN ({marks}) AND feed=?{clause}",
                    params,
                )
                total += cur.rowcount
        return total

    def upsert_snapshot_context(
        self,
        context: Mapping[str, Any],
        *,
        include_live: bool = True,
        live_session_date: str | None = None,
    ) -> tuple[int, int]:
        """Atomically persist latest normalized snapshots and any contained daily bars."""
        if not isinstance(context, Mapping) or context.get("status") != "ok":
            return 0, 0
        feed = _clean_namespace(context.get("feed"), "snapshot feed")
        captured_at = str(context.get("as_of") or self._now_text())
        if live_session_date is not None:
            live_session_date = _date_text(live_session_date, "live_session_date")
        symbols = context.get("symbols") or {}
        if not isinstance(symbols, Mapping):
            raise ValueError("snapshot symbols must be an object")
        snapshot_rows: list[tuple[str, str, str, str, str]] = []
        live_input: dict[str, Mapping[str, Any]] = {}
        for raw_symbol, payload in symbols.items():
            symbol = _clean_symbol(raw_symbol)
            if not isinstance(payload, Mapping):
                raise ValueError(f"snapshot for {symbol} must be an object")
            _reject_sensitive_payload(payload, f"snapshot.{symbol}")
            session_date = self._snapshot_session_date(payload, captured_at)
            normalized = dict(payload)
            normalized.setdefault("symbol", symbol)
            snapshot_rows.append((
                symbol, feed, session_date, captured_at,
                json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            if (
                include_live
                and isinstance(payload.get("daily_bar"), Mapping)
                and (live_session_date is None or session_date == live_session_date)
            ):
                live_input[symbol] = payload
        live_rows = self._live_rows(live_input, feed, captured_at)
        if not snapshot_rows:
            return 0, 0
        with self.connection() as conn, _transaction(conn):
            conn.executemany(
                """
                INSERT INTO latest_snapshots (
                    symbol, feed, session_date, captured_at, normalized_payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, feed) DO UPDATE SET
                    session_date=excluded.session_date,
                    captured_at=excluded.captured_at,
                    normalized_payload_json=excluded.normalized_payload_json
                """,
                snapshot_rows,
            )
            if live_rows:
                self._upsert_live_rows(conn, live_rows)
        return len(snapshot_rows), len(live_rows)

    @staticmethod
    def _snapshot_session_date(payload: Mapping[str, Any], captured_at: str) -> str:
        daily = payload.get("daily_bar") if isinstance(payload.get("daily_bar"), Mapping) else {}
        candidates = (
            daily.get("date"), daily.get("t"),
            (payload.get("minute_bar") or {}).get("time") if isinstance(payload.get("minute_bar"), Mapping) else None,
            (payload.get("latest_trade") or {}).get("trade_time") if isinstance(payload.get("latest_trade"), Mapping) else None,
            (payload.get("quote") or {}).get("quote_time") if isinstance(payload.get("quote"), Mapping) else None,
            captured_at,
        )
        for value in candidates:
            session = _session_date_from_value(value)
            if session:
                return session
        raise ValueError("snapshot has no usable session date")

    def get_latest_snapshots(self, symbols: Iterable[str], *, feed: str) -> dict[str, Any]:
        clean = _clean_symbols(symbols)
        if not clean:
            return {"status": "unavailable", "reason": "没有可查询的 symbol"}
        feed = _clean_namespace(feed, "feed")
        payloads: dict[str, dict[str, Any]] = {}
        meta: dict[str, dict[str, str]] = {}
        captured: list[str] = []
        with self.connection() as conn:
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM latest_snapshots WHERE symbol IN ({marks}) AND feed=?",
                    [*chunk, feed],
                ).fetchall()
                for row in rows:
                    symbol = str(row["symbol"])
                    try:
                        payload = json.loads(str(row["normalized_payload_json"]))
                    except json.JSONDecodeError as exc:
                        raise MarketDataCacheError(
                            f"cached snapshot JSON is corrupt for {symbol}: {exc}"
                        ) from exc
                    payloads[symbol] = payload
                    meta[symbol] = {
                        "captured_at": str(row["captured_at"]),
                        "session_date": str(row["session_date"]),
                    }
                    captured.append(str(row["captured_at"]))
        ordered = {symbol: payloads[symbol] for symbol in clean if symbol in payloads}
        if not ordered:
            return {"status": "unavailable", "reason": "数据库中没有 snapshot"}
        return {
            "status": "ok",
            "as_of": max(captured),
            "feed": feed,
            "symbols": ordered,
            "meta": {symbol: meta[symbol] for symbol in ordered},
        }

    def add_sync_range(
        self,
        symbols: Iterable[str],
        *,
        feed: str,
        adjustment: str,
        start_date: str,
        end_date: str,
        synced_at: str | None = None,
    ) -> None:
        clean = _clean_symbols(symbols)
        if not clean:
            return
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        synced_at = str(synced_at or self._now_text())
        with self.connection() as conn, _transaction(conn):
            self._merge_sync_ranges(
                conn, clean, feed, adjustment, start_date, end_date, synced_at
            )

    @staticmethod
    def _merge_sync_ranges(
        conn: sqlite3.Connection,
        symbols: Sequence[str],
        feed: str,
        adjustment: str,
        start_date: str,
        end_date: str,
        synced_at: str,
    ) -> None:
        for symbol in symbols:
            existing = [
                (str(row["start_date"]), str(row["end_date"]))
                for row in conn.execute(
                    """
                    SELECT start_date, end_date FROM sync_ranges
                    WHERE symbol=? AND feed=? AND adjustment=?
                    ORDER BY start_date
                    """,
                    (symbol, feed, adjustment),
                ).fetchall()
            ]
            merged = merge_date_ranges([*existing, (start_date, end_date)])
            conn.execute(
                "DELETE FROM sync_ranges WHERE symbol=? AND feed=? AND adjustment=?",
                (symbol, feed, adjustment),
            )
            conn.executemany(
                """
                INSERT INTO sync_ranges (
                    symbol, feed, adjustment, start_date, end_date, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(symbol, feed, adjustment, a, b, synced_at) for a, b in merged],
            )

    def commit_daily_sync(
        self,
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        covered_symbols: Iterable[str],
        *,
        feed: str,
        adjustment: str,
        start_date: str,
        end_date: str,
        replace: bool = False,
        synced_at: str | None = None,
    ) -> int:
        """Atomically replace/upsert final bars and record successful source coverage."""
        clean = _clean_symbols(covered_symbols)
        if not clean:
            return 0
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        synced_at = str(synced_at or self._now_text())
        rows, finalized = self._daily_rows(bars, feed, adjustment, synced_at)
        with self.connection() as conn, _transaction(conn):
            if replace:
                for offset in range(0, len(clean), _SYMBOL_CHUNK):
                    chunk = clean[offset:offset + _SYMBOL_CHUNK]
                    marks = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"""
                        DELETE FROM daily_bars
                        WHERE symbol IN ({marks}) AND session_date BETWEEN ? AND ?
                          AND feed=? AND adjustment=?
                        """,
                        [*chunk, start_date, end_date, feed, adjustment],
                    )
            if rows:
                self._upsert_daily_rows(conn, rows, finalized)
            self._merge_sync_ranges(
                conn, clean, feed, adjustment, start_date, end_date, synced_at
            )
        return len(rows)

    def get_sync_ranges(
        self, symbol: str, *, feed: str, adjustment: str
    ) -> list[dict[str, str]]:
        symbol = _clean_symbol(symbol)
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT start_date, end_date, synced_at FROM sync_ranges
                WHERE symbol=? AND feed=? AND adjustment=? ORDER BY start_date
                """,
                (symbol, feed, adjustment),
            ).fetchall()
        return [dict(row) for row in rows]

    def missing_ranges(
        self,
        symbol: str,
        *,
        feed: str,
        adjustment: str,
        start_date: str,
        end_date: str,
    ) -> list[tuple[str, str]]:
        ranges = self.get_sync_ranges(symbol, feed=feed, adjustment=adjustment)
        return missing_date_ranges(
            [(row["start_date"], row["end_date"]) for row in ranges],
            start_date,
            end_date,
        )

    def missing_ranges_bulk(
        self,
        symbols: Iterable[str],
        *,
        feed: str,
        adjustment: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, list[tuple[str, str]]]:
        """Per-symbol gaps for a whole batch with one connection and one query."""
        clean = _clean_symbols(symbols)
        if not clean:
            return {}
        feed = _clean_namespace(feed, "feed")
        adjustment = _clean_namespace(adjustment, "adjustment")
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        covered: dict[str, list[tuple[str, str]]] = {symbol: [] for symbol in clean}
        with self.connection() as conn:
            for offset in range(0, len(clean), _SYMBOL_CHUNK):
                chunk = clean[offset:offset + _SYMBOL_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT symbol, start_date, end_date FROM sync_ranges
                    WHERE symbol IN ({marks}) AND feed=? AND adjustment=?
                    """,
                    [*chunk, feed, adjustment],
                ).fetchall()
                for row in rows:
                    covered[str(row["symbol"])].append(
                        (str(row["start_date"]), str(row["end_date"]))
                    )
        return {
            symbol: missing_date_ranges(covered[symbol], start_date, end_date)
            for symbol in clean
        }

    def replace_asset_catalog(
        self,
        assets: Iterable[Mapping[str, Any]],
        *,
        catalog: str = ASSET_CATALOG,
        synced_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace the current full asset view without deleting history."""
        catalog = _clean_namespace(catalog, "asset catalog")
        synced_at = str(synced_at or self._now_text())
        by_id: dict[str, tuple[Any, ...]] = {}
        for asset in assets or []:
            row = _normalize_asset_row(asset, synced_at)
            by_id[str(row[0])] = row
        if not by_id:
            raise ValueError("asset catalog response must not be empty")
        rows = list(by_id.values())
        current_ids = set(by_id)
        tradable_count = sum(int(row[6]) for row in rows)
        eligible_count = sum(int(row[12]) for row in rows)
        with self.connection() as conn, _transaction(conn):
            previous_ids = {
                str(row["asset_id"])
                for row in conn.execute(
                    "SELECT asset_id FROM assets WHERE is_current=1"
                ).fetchall()
            }
            conn.execute(
                """
                UPDATE assets
                SET is_current=0, data_eligible=0,
                    excluded_reason='not_seen_in_latest_sync'
                WHERE is_current=1
                """
            )
            conn.executemany(
                """
                INSERT INTO assets (
                    asset_id, symbol, asset_class, exchange, name, status,
                    tradable, fractionable, marginable, shortable, easy_to_borrow,
                    is_current, data_eligible, excluded_reason,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    asset_class=excluded.asset_class,
                    exchange=excluded.exchange,
                    name=excluded.name,
                    status=excluded.status,
                    tradable=excluded.tradable,
                    fractionable=excluded.fractionable,
                    marginable=excluded.marginable,
                    shortable=excluded.shortable,
                    easy_to_borrow=excluded.easy_to_borrow,
                    is_current=1,
                    data_eligible=excluded.data_eligible,
                    excluded_reason=excluded.excluded_reason,
                    last_seen_at=excluded.last_seen_at
                """,
                rows,
            )
            state = {
                "catalog": catalog,
                "synced_at": synced_at,
                "source_count": len(rows),
                "current_count": len(rows),
                "tradable_count": tradable_count,
                "eligible_count": eligible_count,
                "unsupported_count": len(rows) - eligible_count,
            }
            conn.execute(
                """
                INSERT INTO asset_catalog_state (
                    catalog, synced_at, source_count, current_count,
                    tradable_count, eligible_count, unsupported_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(catalog) DO UPDATE SET
                    synced_at=excluded.synced_at,
                    source_count=excluded.source_count,
                    current_count=excluded.current_count,
                    tradable_count=excluded.tradable_count,
                    eligible_count=excluded.eligible_count,
                    unsupported_count=excluded.unsupported_count
                """,
                (
                    state["catalog"], state["synced_at"], state["source_count"],
                    state["current_count"], state["tradable_count"],
                    state["eligible_count"], state["unsupported_count"],
                ),
            )
        return {
            **state,
            "new_count": len(current_ids - previous_ids),
            "disappeared_count": len(previous_ids - current_ids),
        }

    def get_asset_catalog_state(
        self, catalog: str = ASSET_CATALOG
    ) -> dict[str, Any] | None:
        catalog = _clean_namespace(catalog, "asset catalog")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM asset_catalog_state WHERE catalog=?", (catalog,)
            ).fetchone()
        return dict(row) if row else None

    def asset_catalog_is_fresh_today(self, catalog: str = ASSET_CATALOG) -> bool:
        state = self.get_asset_catalog_state(catalog)
        if not state:
            return False
        try:
            synced = dt.datetime.fromisoformat(
                str(state["synced_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return False
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=dt.timezone.utc)
        return synced.astimezone(dt.timezone.utc).date() == self.now_utc().date()

    def get_asset_symbols(self, *, data_eligible: bool = True) -> list[str]:
        clause = "AND data_eligible=1" if data_eligible else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT symbol FROM assets
                WHERE is_current=1 {clause}
                ORDER BY symbol
                """
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def get_asset_exclusion_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(excluded_reason, 'eligible') AS reason, count(*) AS count
                FROM assets WHERE is_current=1
                GROUP BY COALESCE(excluded_reason, 'eligible') ORDER BY reason
                """
            ).fetchall()
        return {str(row["reason"]): int(row["count"]) for row in rows}

    def get_assets(self, *, current_only: bool = True) -> list[dict[str, Any]]:
        clause = "WHERE is_current=1" if current_only else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM assets {clause} ORDER BY symbol, asset_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_feed_fallback(self, requested_feed: str) -> str | None:
        requested_feed = _clean_namespace(requested_feed, "requested feed")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT actual_feed FROM feed_fallbacks WHERE requested_feed=?",
                (requested_feed,),
            ).fetchone()
        return str(row["actual_feed"]) if row else None

    def set_feed_fallback(
        self, requested_feed: str, actual_feed: str, *, updated_at: str | None = None
    ) -> None:
        requested_feed = _clean_namespace(requested_feed, "requested feed")
        actual_feed = _clean_namespace(actual_feed, "actual feed")
        updated_at = str(updated_at or self._now_text())
        with self.connection() as conn, _transaction(conn):
            conn.execute(
                """
                INSERT INTO feed_fallbacks (requested_feed, actual_feed, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(requested_feed) DO UPDATE SET
                    actual_feed=excluded.actual_feed,
                    updated_at=excluded.updated_at
                """,
                (requested_feed, actual_feed, updated_at),
            )

    def clear_feed_fallback(self, requested_feed: str) -> None:
        requested_feed = _clean_namespace(requested_feed, "requested feed")
        with self.connection() as conn, _transaction(conn):
            conn.execute(
                "DELETE FROM feed_fallbacks WHERE requested_feed=?", (requested_feed,)
            )

    def upsert_market_sessions(
        self,
        sessions: Iterable[Mapping[str, Any]],
        *,
        fetched_at: str | None = None,
    ) -> int:
        fetched_at = str(fetched_at or self._now_text())
        rows: list[tuple[str, str, str, str]] = []
        for session in sessions or []:
            if not isinstance(session, Mapping):
                raise ValueError("market session must be an object")
            session_date = _date_text(
                str(_first_present(session, "session_date", "date")), "session date"
            )
            open_at = str(_first_present(session, "open_at", "open") or "").strip()
            close_at = str(_first_present(session, "close_at", "close") or "").strip()
            if not open_at or not close_at:
                raise ValueError(f"market session {session_date} is missing open/close")
            rows.append((session_date, open_at, close_at, fetched_at))
        if not rows:
            return 0
        with self.connection() as conn, _transaction(conn):
            conn.executemany(
                """
                INSERT INTO market_sessions (session_date, open_at, close_at, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_date) DO UPDATE SET
                    open_at=excluded.open_at,
                    close_at=excluded.close_at,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def get_market_session(self, session_date: str) -> dict[str, str] | None:
        session_date = _date_text(session_date, "session_date")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_sessions WHERE session_date=?", (session_date,)
            ).fetchone()
        return dict(row) if row else None

    def get_market_sessions(self, start_date: str, end_date: str) -> list[dict[str, str]]:
        start_date = _date_text(start_date, "start_date")
        end_date = _date_text(end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date is after end_date")
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM market_sessions
                WHERE session_date BETWEEN ? AND ? ORDER BY session_date
                """,
                (start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "ASSET_CATALOG",
    "CacheBusyError",
    "CacheSchemaError",
    "DEFAULT_DB_PATH",
    "ENV_DB_PATH",
    "MarketDataStore",
    "MarketDataCacheError",
    "SCHEMA_VERSION",
    "asset_data_eligibility",
    "merge_date_ranges",
    "missing_date_ranges",
    "resolve_db_path",
]
