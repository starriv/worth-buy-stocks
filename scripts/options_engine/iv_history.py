"""Point-in-time daily 30D IV history backed by a dedicated SQLite file."""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_MIN_OBSERVATIONS = 20
DEFAULT_LOOKBACK = 252


def default_history_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "options-history.sqlite3"


def constant_maturity_iv(surface: dict[str, Any], target_dte: int = 30) -> dict[str, Any]:
    """Interpolate total variance to a constant maturity when possible."""
    points = sorted(
        (
            int(row["dte"]), float(row["atm_iv"]), expiration
        )
        for expiration, row in (surface.get("expirations") or {}).items()
        if row.get("dte") and row.get("atm_iv")
    )
    if not points:
        return {"status": "unavailable", "target_dte": target_dte}
    lower = max((point for point in points if point[0] <= target_dte), default=None)
    upper = min((point for point in points if point[0] >= target_dte), default=None)
    if lower and upper and lower[0] != upper[0]:
        low_t, low_iv, _ = lower
        high_t, high_iv, _ = upper
        low_var = low_iv * low_iv * low_t / 365.0
        high_var = high_iv * high_iv * high_t / 365.0
        weight = (target_dte - low_t) / (high_t - low_t)
        target_var = low_var + weight * (high_var - low_var)
        value = math.sqrt(max(0.0, target_var) / (target_dte / 365.0))
        method = "total_variance_interpolation"
        source_expirations = [lower[2], upper[2]]
    else:
        nearest = min(points, key=lambda point: abs(point[0] - target_dte))
        value = nearest[1]
        method = "nearest_expiration_proxy"
        source_expirations = [nearest[2]]
    return {
        "status": "ok",
        "target_dte": target_dte,
        "iv": round(value, 8),
        "iv_pct": round(value * 100, 2),
        "method": method,
        "source_expirations": source_expirations,
    }


def _session_date(value: str) -> str:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(_NEW_YORK).date().isoformat()


class IVHistoryStore:
    """Persist one quality-aware 30D IV observation per symbol/session."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_history_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS option_iv_daily (
                symbol TEXT NOT NULL,
                session_date TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                iv_30d REAL NOT NULL,
                method TEXT NOT NULL,
                feed TEXT NOT NULL,
                feed_quality INTEGER NOT NULL,
                PRIMARY KEY (symbol, session_date)
            )
        """)
        return connection

    def history_context(
        self,
        symbol: str,
        current_iv: float,
        *,
        current_session: str,
        lookback: int = DEFAULT_LOOKBACK,
        min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT session_date, iv_30d FROM option_iv_daily
                   WHERE symbol = ? AND session_date < ?
                   ORDER BY session_date DESC LIMIT ?""",
                (symbol.upper(), current_session, int(lookback)),
            ).fetchall()
        values = [float(row["iv_30d"]) for row in rows]
        if len(values) < min_observations:
            return {
                "status": "insufficient_history",
                "observations": len(values),
                "minimum_observations": min_observations,
                "lookback_sessions": lookback,
            }
        low, high = min(values), max(values)
        rank = 50.0 if high == low else (current_iv - low) / (high - low) * 100
        percentile = sum(value <= current_iv for value in values) / len(values) * 100
        return {
            "status": "ok",
            "observations": len(values),
            "lookback_sessions": lookback,
            "iv_rank": round(max(0.0, min(100.0, rank)), 2),
            "iv_percentile": round(percentile, 2),
            "history_min_iv_pct": round(low * 100, 2),
            "history_max_iv_pct": round(high * 100, 2),
        }

    def record(self, symbol: str, surface: dict[str, Any], *, feed: str) -> dict[str, Any]:
        constant = constant_maturity_iv(surface)
        captured_at = str(surface.get("as_of") or "")
        if constant.get("status") != "ok" or not captured_at:
            return {"status": "skipped", "reason": "30D_IV_or_timestamp_unavailable"}
        session = _session_date(captured_at)
        quality = 2 if str(feed).lower() == "opra" else 1
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO option_iv_daily
                   (symbol, session_date, captured_at, iv_30d, method, feed, feed_quality)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, session_date) DO UPDATE SET
                     captured_at=excluded.captured_at,
                     iv_30d=excluded.iv_30d,
                     method=excluded.method,
                     feed=excluded.feed,
                     feed_quality=excluded.feed_quality
                   WHERE excluded.feed_quality >= option_iv_daily.feed_quality
                     AND excluded.captured_at >= option_iv_daily.captured_at""",
                (
                    symbol.upper(), session, captured_at, constant["iv"],
                    constant["method"], str(feed).lower(), quality,
                ),
            )
            connection.commit()
        return {"status": "recorded", "session_date": session, **constant}


def attach_history(surface: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(surface)
    out["constant_maturity_30d"] = constant_maturity_iv(surface)
    out["historical_iv"] = context or {"status": "unavailable"}
    return out
