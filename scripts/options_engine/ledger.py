"""Local audit ledger for user-confirmed defined-risk credit structures."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics
from pathlib import Path
from typing import Any

from .portfolio_risk import parse_occ_symbol


def default_ledger_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "options-ledger.sqlite3"


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be finite" + (" and > 0" if positive else ""))
    return result


def _timestamp(value: Any, name: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


_SUPPORTED_STRATEGIES = {"bull_put", "bear_call", "iron_condor", "iron_butterfly"}


def _leg_side(value: Any) -> str:
    side = str(value or "").lower()
    if side in {"short", "sell", "sell_to_open"}:
        return "short"
    if side in {"long", "buy", "buy_to_open"}:
        return "long"
    raise ValueError("each leg side must identify a long or short opening leg")


def _normalize_explicit_legs(position: dict[str, Any], strategy: str) -> list[dict[str, Any]]:
    raw_legs = position.get("legs")
    if not isinstance(raw_legs, list) or len(raw_legs) != 4:
        raise ValueError(f"{strategy} requires exactly four legs")
    legs = []
    for raw in raw_legs:
        if not isinstance(raw, dict):
            raise ValueError("each leg must be an object")
        symbol = str(raw.get("symbol") or "").upper().replace(" ", "")
        meta = parse_occ_symbol(symbol)
        if not meta:
            raise ValueError("all legs must use standard OCC option symbols")
        stated_type = str(raw.get("type") or meta["type"]).lower()
        stated_strike = _number(raw.get("strike", meta["strike"]), "leg strike")
        if stated_type != meta["type"] or stated_strike != meta["strike"]:
            raise ValueError("leg type or strike does not match its OCC symbol")
        legs.append({
            "symbol": symbol,
            "side": _leg_side(raw.get("side")),
            "role": str(raw.get("role") or "").lower(),
            "type": meta["type"],
            "strike": meta["strike"],
            "expiration_date": meta["expiration_date"],
            "underlying_symbol": meta["underlying_symbol"],
            "ratio": 1,
        })
    if len({leg["symbol"] for leg in legs}) != 4:
        raise ValueError("four-leg structures require four distinct OCC symbols")
    if len({(leg["underlying_symbol"], leg["expiration_date"]) for leg in legs}) != 1:
        raise ValueError("all legs must share underlying and expiration")
    return legs


def _role_map(legs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    roles = [str(leg.get("role") or "") for leg in legs]
    if len(set(roles)) != len(roles):
        raise ValueError("leg roles must be unique")
    return {role: leg for role, leg in zip(roles, legs)}


def position_legs(position: dict[str, Any]) -> list[dict[str, Any]]:
    """Return canonical long/short legs for persisted or offline positions."""
    raw = position.get("legs")
    if not isinstance(raw, list):
        encoded = position.get("legs_json")
        if encoded:
            try:
                raw = json.loads(str(encoded))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = None
    if isinstance(raw, list) and raw:
        return [dict(leg) for leg in raw if isinstance(leg, dict)]
    strategy = str(position.get("strategy") or "").lower()
    if strategy not in {"bull_put", "bear_call"}:
        return []
    option_type = "put" if strategy == "bull_put" else "call"
    return [
        {
            "symbol": str(position.get("short_leg_symbol") or "").upper(),
            "side": "short", "role": f"short_{option_type}", "type": option_type,
            "strike": position.get("short_strike"),
            "expiration_date": position.get("expiration_date"),
            "underlying_symbol": position.get("underlying_symbol"), "ratio": 1,
        },
        {
            "symbol": str(position.get("long_leg_symbol") or "").upper(),
            "side": "long", "role": f"long_{option_type}_wing", "type": option_type,
            "strike": position.get("long_strike"),
            "expiration_date": position.get("expiration_date"),
            "underlying_symbol": position.get("underlying_symbol"), "ratio": 1,
        },
    ]


def position_width(position: dict[str, Any]) -> float:
    stored = position.get("structure_width")
    if stored is not None:
        try:
            width = float(stored)
        except (TypeError, ValueError):
            width = 0.0
        if math.isfinite(width) and width > 0:
            return width
    legs = position_legs(position)
    strikes = [float(leg["strike"]) for leg in legs if leg.get("strike") is not None]
    if str(position.get("strategy")) in {"iron_condor", "iron_butterfly"} and len(strikes) == 4:
        by_type = {
            option_type: sorted(float(leg["strike"]) for leg in legs if leg.get("type") == option_type)
            for option_type in ("put", "call")
        }
        return max(by_type["put"][1] - by_type["put"][0], by_type["call"][1] - by_type["call"][0])
    return abs(float(position.get("short_strike")) - float(position.get("long_strike")))


def normalize_ledger_position(position: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(position, dict):
        raise ValueError("position must be an object")
    strategy = str(position.get("strategy") or "").lower()
    if strategy not in _SUPPORTED_STRATEGIES:
        raise ValueError("strategy must be bull_put, bear_call, iron_condor or iron_butterfly")
    if strategy in {"iron_condor", "iron_butterfly"}:
        legs = _normalize_explicit_legs(position, strategy)
        roles = _role_map(legs)
        expected_roles = (
            {"long_put_wing", "short_put", "short_call", "long_call_wing"}
            if strategy == "iron_condor" else
            {"long_put_wing", "short_put_body", "short_call_body", "long_call_wing"}
        )
        if set(roles) != expected_roles:
            raise ValueError(f"{strategy} leg roles are incomplete or invalid")
        short_put = roles["short_put" if strategy == "iron_condor" else "short_put_body"]
        short_call = roles["short_call" if strategy == "iron_condor" else "short_call_body"]
        long_put, long_call = roles["long_put_wing"], roles["long_call_wing"]
        expected = (
            (long_put, "long", "put"), (short_put, "short", "put"),
            (short_call, "short", "call"), (long_call, "long", "call"),
        )
        if any(leg["side"] != side or leg["type"] != option_type for leg, side, option_type in expected):
            raise ValueError("four-leg sides or option types do not match the strategy")
        if not (long_put["strike"] < short_put["strike"] <= short_call["strike"] < long_call["strike"]):
            raise ValueError("four-leg strikes are not ordered as a defined-risk structure")
        if strategy == "iron_condor" and short_put["strike"] >= short_call["strike"]:
            raise ValueError("iron_condor short strikes must define a positive profit zone")
        if strategy == "iron_butterfly" and short_put["strike"] != short_call["strike"]:
            raise ValueError("iron_butterfly short put and call must share the body strike")
        put_width = short_put["strike"] - long_put["strike"]
        call_width = long_call["strike"] - short_call["strike"]
        if strategy == "iron_butterfly" and not math.isclose(put_width, call_width, abs_tol=1e-9):
            raise ValueError("iron_butterfly currently requires symmetric wings")
        width = max(put_width, call_width)
        short_symbol, long_symbol = short_put["symbol"], long_put["symbol"]
        short_meta, long_meta = parse_occ_symbol(short_symbol), parse_occ_symbol(long_symbol)
    else:
        legs = []
        short_symbol = str(position.get("short_leg_symbol") or "").upper().replace(" ", "")
        long_symbol = str(position.get("long_leg_symbol") or "").upper().replace(" ", "")
        short_meta, long_meta = parse_occ_symbol(short_symbol), parse_occ_symbol(long_symbol)
        if not short_meta or not long_meta:
            raise ValueError("both legs must use standard OCC option symbols")
        expected_type = "put" if strategy == "bull_put" else "call"
        comparable = ("underlying_symbol", "expiration_date", "type")
        if any(short_meta[key] != long_meta[key] for key in comparable):
            raise ValueError("spread legs must share underlying, expiration and option type")
        if short_meta["type"] != expected_type:
            raise ValueError("option type does not match strategy")
        if strategy == "bull_put" and long_meta["strike"] >= short_meta["strike"]:
            raise ValueError("bull_put protective strike must be below the short strike")
        if strategy == "bear_call" and long_meta["strike"] <= short_meta["strike"]:
            raise ValueError("bear_call protective strike must be above the short strike")
        width = abs(short_meta["strike"] - long_meta["strike"])
        legs = position_legs({
            **position, "strategy": strategy, "short_leg_symbol": short_symbol,
            "long_leg_symbol": long_symbol, "short_strike": short_meta["strike"],
            "long_strike": long_meta["strike"], "expiration_date": short_meta["expiration_date"],
            "underlying_symbol": short_meta["underlying_symbol"],
        })
    quantity = _number(position.get("quantity"), "quantity", positive=True)
    if not quantity.is_integer():
        raise ValueError("quantity must be a whole number of spreads")
    initial_credit = _number(position.get("initial_credit"), "initial_credit", positive=True)
    if initial_credit >= width:
        raise ValueError("initial_credit must be below structure width")
    opened_at = _timestamp(position.get("opened_at"), "opened_at")
    opened_date = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00")).date()
    expiry = dt.date.fromisoformat(short_meta["expiration_date"])
    if opened_date > expiry:
        raise ValueError("opened_at cannot be after expiration")
    fingerprint = "|".join(str(value) for value in (
        short_meta["underlying_symbol"], strategy,
        *(leg["symbol"] for leg in sorted(legs, key=lambda row: row["role"])), opened_at,
    ))
    position_id = str(position.get("position_id") or "").strip()
    if not position_id:
        position_id = "spread-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    return {
        "position_id": position_id,
        "fingerprint": fingerprint,
        "underlying_symbol": short_meta["underlying_symbol"],
        "strategy": strategy,
        "expiration_date": short_meta["expiration_date"],
        "short_leg_symbol": short_symbol,
        "long_leg_symbol": long_symbol,
        "short_strike": short_meta["strike"],
        "long_strike": long_meta["strike"],
        "legs": legs,
        "legs_json": json.dumps(legs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "structure_width": round(width, 4),
        "quantity": int(quantity),
        "initial_credit": round(initial_credit, 4),
        "opened_at": opened_at,
        "notes": str(position.get("notes") or "")[:500],
    }


def position_from_candidate(
    candidate: dict[str, Any], *, quantity: int, initial_credit: float, opened_at: str
) -> dict[str, Any]:
    """Convert a screened candidate to ledger terms using the user's actual fill."""
    payload = {
        "underlying_symbol": candidate.get("underlying_symbol"),
        "strategy": candidate.get("strategy"),
        "expiration_date": candidate.get("expiration_date"),
        "short_leg_symbol": (candidate.get("short_leg") or {}).get("symbol"),
        "long_leg_symbol": (candidate.get("long_leg") or {}).get("symbol"),
        "short_strike": (candidate.get("short_leg") or {}).get("strike"),
        "long_strike": (candidate.get("long_leg") or {}).get("strike"),
        "quantity": quantity,
        "initial_credit": initial_credit,
        "opened_at": opened_at,
    }
    if isinstance(candidate.get("legs"), list):
        payload["legs"] = candidate["legs"]
    return normalize_ledger_position(payload)


class OptionsLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_ledger_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS option_spreads (
                position_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                underlying_symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                expiration_date TEXT NOT NULL,
                short_leg_symbol TEXT NOT NULL,
                long_leg_symbol TEXT NOT NULL,
                short_strike REAL NOT NULL,
                long_strike REAL NOT NULL,
                quantity INTEGER NOT NULL,
                remaining_quantity INTEGER,
                initial_credit REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at TEXT,
                close_debit REAL,
                notes TEXT NOT NULL DEFAULT '',
                legs_json TEXT NOT NULL DEFAULT '[]',
                structure_width REAL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS option_spreads_open_fingerprint
                ON option_spreads(fingerprint) WHERE status = 'open';
            CREATE TABLE IF NOT EXISTS option_spread_reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(position_id) REFERENCES option_spreads(position_id)
            );
            CREATE TABLE IF NOT EXISTS option_spread_closures (
                closure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                close_debit REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(position_id) REFERENCES option_spreads(position_id)
            );
            CREATE INDEX IF NOT EXISTS option_spread_closures_position
                ON option_spread_closures(position_id, closed_at, closure_id);
        """)
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(option_spreads)").fetchall()
        }
        if "remaining_quantity" not in columns:
            connection.execute("ALTER TABLE option_spreads ADD COLUMN remaining_quantity INTEGER")
        if "legs_json" not in columns:
            connection.execute(
                "ALTER TABLE option_spreads ADD COLUMN legs_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "structure_width" not in columns:
            connection.execute("ALTER TABLE option_spreads ADD COLUMN structure_width REAL")
        connection.execute(
            """UPDATE option_spreads SET structure_width=ABS(short_strike-long_strike)
               WHERE structure_width IS NULL OR structure_width <= 0"""
        )
        connection.execute(
            """UPDATE option_spreads
               SET remaining_quantity=CASE WHEN status='open' THEN quantity ELSE 0 END
               WHERE remaining_quantity IS NULL"""
        )
        # Preserve pre-migration full closes as one actual-fill closure record.
        connection.execute(
            """INSERT INTO option_spread_closures
               (position_id, closed_at, quantity, close_debit, created_at)
               SELECT spread.position_id, spread.closed_at, spread.quantity,
                      spread.close_debit, spread.closed_at
               FROM option_spreads AS spread
               WHERE spread.status='closed' AND spread.closed_at IS NOT NULL
                 AND spread.close_debit IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM option_spread_closures AS closure
                    WHERE closure.position_id=spread.position_id
                 )"""
        )
        return connection

    def add(self, position: dict[str, Any]) -> dict[str, Any]:
        row = normalize_ledger_position(position)
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO option_spreads
                    (position_id, fingerprint, underlying_symbol, strategy, expiration_date,
                     short_leg_symbol, long_leg_symbol, short_strike, long_strike,
                     quantity, remaining_quantity, initial_credit, opened_at, notes,
                     legs_json, structure_width, created_at)
                    VALUES (:position_id, :fingerprint, :underlying_symbol, :strategy,
                     :expiration_date, :short_leg_symbol, :long_leg_symbol, :short_strike,
                     :long_strike, :quantity, :quantity, :initial_credit, :opened_at, :notes,
                     :legs_json, :structure_width, :created_at)""",
                    {**row, "created_at": now},
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("position already exists in the open ledger") from exc
        return {
            key: value for key, value in row.items() if key not in {"fingerprint", "legs_json"}
        } | {"status": "open", "remaining_quantity": row["quantity"]}

    def list(self, status: str = "open") -> list[dict[str, Any]]:
        if status not in {"open", "closed", "all"}:
            raise ValueError("status must be open, closed or all")
        where, params = ("", ()) if status == "all" else ("WHERE status = ?", (status,))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM option_spreads {where} ORDER BY opened_at, position_id", params
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["legs"] = position_legs(item)
            item.pop("legs_json", None)
            item["opened_quantity"] = int(item["quantity"])
            if item.get("status") == "open":
                item["quantity"] = int(item.get("remaining_quantity") or 0)
            out.append(item)
        return out

    def close(
        self, position_id: str, *, closed_at: str, close_debit: float,
        quantity: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _timestamp(closed_at, "closed_at")
        debit = _number(close_debit, "close_debit")
        if debit < 0:
            raise ValueError("close_debit must be >= 0")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM option_spreads WHERE position_id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
            if row is None:
                raise ValueError("open position_id not found")
            width = position_width(dict(row))
            if debit > width:
                raise ValueError("close_debit must not exceed spread width")
            if timestamp < str(row["opened_at"]):
                raise ValueError("closed_at cannot be before opened_at")
            remaining = int(row["remaining_quantity"] or 0)
            close_quantity = remaining if quantity is None else _number(
                quantity, "quantity", positive=True,
            )
            if not float(close_quantity).is_integer():
                raise ValueError("quantity must be a whole number of spreads")
            close_quantity = int(close_quantity)
            if close_quantity > remaining:
                raise ValueError("quantity cannot exceed remaining open spreads")
            created_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            connection.execute(
                """INSERT INTO option_spread_closures
                   (position_id, closed_at, quantity, close_debit, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (position_id, timestamp, close_quantity, round(debit, 4), created_at),
            )
            new_remaining = remaining - close_quantity
            if new_remaining:
                connection.execute(
                    """UPDATE option_spreads SET remaining_quantity=?
                       WHERE position_id=? AND status='open'""",
                    (new_remaining, position_id),
                )
                status = "open"
            else:
                closure_summary = connection.execute(
                    """SELECT SUM(quantity * close_debit) / SUM(quantity), MAX(closed_at)
                       FROM option_spread_closures WHERE position_id=?""",
                    (position_id,),
                ).fetchone()
                weighted, final_closed_at = closure_summary
                connection.execute(
                    """UPDATE option_spreads SET status='closed', remaining_quantity=0,
                       closed_at=?, close_debit=? WHERE position_id=? AND status='open'""",
                    (final_closed_at, round(float(weighted), 4), position_id),
                )
                status = "closed"
        return {
            "position_id": position_id,
            "status": status,
            "closed_at": timestamp,
            "closed_quantity": close_quantity,
            "remaining_quantity": new_remaining,
            "close_debit": round(debit, 4),
        }

    def record_reviews(self, reviewed_at: str, rows: list[dict[str, Any]]) -> int:
        timestamp = _timestamp(reviewed_at, "reviewed_at")
        values = [
            (
                str(row.get("position_id") or ""), timestamp,
                str(row.get("action") or "数据不足"),
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            for row in rows if row.get("position_id")
        ]
        with self._connect() as connection:
            connection.executemany(
                """INSERT INTO option_spread_reviews
                   (position_id, reviewed_at, action, payload_json) VALUES (?, ?, ?, ?)""",
                values,
            )
        return len(values)

    def reviews(self, position_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT reviewed_at, action, payload_json FROM option_spread_reviews
                   WHERE position_id=? ORDER BY reviewed_at DESC, review_id DESC LIMIT ?""",
                (position_id, max(1, int(limit))),
            ).fetchall()
        return [
            {"reviewed_at": row["reviewed_at"], "action": row["action"], "review": json.loads(row["payload_json"])}
            for row in rows
        ]

    @staticmethod
    def _performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        pnl = [float(row["gross_pnl"]) for row in rows]
        returns = [float(row["return_on_risk_pct"]) for row in rows]
        captures = [float(row["credit_capture_pct"]) for row in rows]
        holding_days = [float(row["holding_days"]) for row in rows]
        wins = sum(value > 0 for value in pnl)
        losses = sum(value < 0 for value in pnl)
        breakeven = len(pnl) - wins - losses
        position_ids = {str(row["position_id"]) for row in rows}
        fully_closed = {
            str(row["position_id"]) for row in rows if row.get("position_status") == "closed"
        }
        partially_open = {
            str(row["position_id"]) for row in rows if row.get("position_status") == "open"
        }
        return {
            "realized_close_records": len(rows),
            "closed_positions": len(fully_closed),
            "positions_with_realized_pnl": len(position_ids),
            "partial_open_positions": len(partially_open),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate_pct": round(wins / len(rows) * 100, 2) if rows else None,
            "win_rate_basis": "realized_close_records",
            "net_pnl": round(sum(pnl), 2),
            "average_pnl": round(statistics.mean(pnl), 2) if rows else None,
            "median_pnl": round(statistics.median(pnl), 2) if rows else None,
            "best_pnl": round(max(pnl), 2) if rows else None,
            "worst_pnl": round(min(pnl), 2) if rows else None,
            "average_return_on_risk_pct": round(statistics.mean(returns), 2) if rows else None,
            "average_credit_capture_pct": round(statistics.mean(captures), 2) if rows else None,
            "average_holding_days": round(statistics.mean(holding_days), 2) if rows else None,
        }

    def performance(self) -> dict[str, Any]:
        """Summarize actual closed fills and pre-close review coverage.

        This is descriptive evidence, not a backtest and not causal validation of
        lifecycle actions. Realized partial closes are included; remaining
        mark-to-market estimates are excluded.
        """
        with self._connect() as connection:
            positions = connection.execute(
                """SELECT spread.*, closure.closure_id, closure.closed_at AS fill_closed_at,
                          closure.quantity AS fill_quantity,
                          closure.close_debit AS fill_close_debit
                   FROM option_spread_closures AS closure
                   JOIN option_spreads AS spread USING(position_id)
                   ORDER BY closure.closed_at, closure.closure_id"""
            ).fetchall()
            reviews = connection.execute(
                """SELECT position_id, reviewed_at, action FROM option_spread_reviews
                   ORDER BY reviewed_at, review_id"""
            ).fetchall()
        reviews_by_position: dict[str, list[sqlite3.Row]] = {}
        for review in reviews:
            reviews_by_position.setdefault(str(review["position_id"]), []).append(review)
        trades: list[dict[str, Any]] = []
        actionable = {"立即退出评估", "计划退出", "止盈退出"}
        for position in positions:
            initial_credit = float(position["initial_credit"])
            close_debit = float(position["fill_close_debit"])
            quantity = int(position["fill_quantity"])
            width = position_width(dict(position))
            max_risk = (width - initial_credit) * 100 * quantity
            gross_pnl = (initial_credit - close_debit) * 100 * quantity
            opened = dt.datetime.fromisoformat(str(position["opened_at"]).replace("Z", "+00:00"))
            closed_at = str(position["fill_closed_at"])
            closed = dt.datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            prior_reviews = [
                review for review in reviews_by_position.get(str(position["position_id"]), [])
                if str(review["reviewed_at"]) <= closed_at
            ]
            first_action = next(
                (review for review in prior_reviews if str(review["action"]) in actionable), None
            )
            latest_review = prior_reviews[-1] if prior_reviews else None
            lead_hours = None
            if first_action is not None:
                action_at = dt.datetime.fromisoformat(
                    str(first_action["reviewed_at"]).replace("Z", "+00:00")
                )
                lead_hours = max(0.0, (closed - action_at).total_seconds() / 3600)
            trades.append({
                "position_id": position["position_id"],
                "closure_id": position["closure_id"],
                "underlying_symbol": position["underlying_symbol"],
                "strategy": position["strategy"],
                "quantity": quantity,
                "opened_at": position["opened_at"],
                "closed_at": closed_at,
                "position_status": position["status"],
                "remaining_quantity": int(position["remaining_quantity"] or 0),
                "initial_credit": round(initial_credit, 4),
                "close_debit": round(close_debit, 4),
                "gross_pnl": round(gross_pnl, 2),
                "max_risk": round(max_risk, 2),
                "return_on_risk_pct": round(gross_pnl / max_risk * 100, 2),
                "credit_capture_pct": round((initial_credit - close_debit) / initial_credit * 100, 2),
                "holding_days": round((closed - opened).total_seconds() / 86400, 2),
                "review_count_before_close": len(prior_reviews),
                "latest_action_before_close": latest_review["action"] if latest_review else None,
                "first_exit_action_before_close": first_action["action"] if first_action else None,
                "first_exit_action_lead_hours": round(lead_hours, 2) if lead_hours is not None else None,
            })
        by_strategy = {
            strategy: self._performance_summary([
                row for row in trades if row["strategy"] == strategy
            ])
            for strategy in ("bull_put", "bear_call", "iron_condor", "iron_butterfly")
            if any(row["strategy"] == strategy for row in trades)
        }
        with_reviews = [row for row in trades if row["review_count_before_close"] > 0]
        with_action = [row for row in trades if row["first_exit_action_before_close"]]
        leads = [float(row["first_exit_action_lead_hours"]) for row in with_action]
        closed_count = len(trades)
        if closed_count >= 30:
            evidence_status = "descriptive_sample_available"
        elif closed_count >= 10:
            evidence_status = "collecting_more_history"
        else:
            evidence_status = "insufficient_history"
        action_counts = {
            action: sum(row["first_exit_action_before_close"] == action for row in with_action)
            for action in sorted({row["first_exit_action_before_close"] for row in with_action})
        }
        summary = self._performance_summary(trades)
        return {
            "status": "ok",
            "method": "actual_confirmed_fills_descriptive",
            "summary": summary,
            "by_strategy": by_strategy,
            "management_reviews": {
                "realized_close_records_with_reviews": len(with_reviews),
                "review_coverage_pct": round(len(with_reviews) / closed_count * 100, 2) if closed_count else None,
                "realized_close_records_with_exit_action": len(with_action),
                "exit_action_coverage_pct": round(len(with_action) / closed_count * 100, 2) if closed_count else None,
                "first_exit_action_counts": action_counts,
                "average_first_exit_action_lead_hours": round(statistics.mean(leads), 2) if leads else None,
            },
            "evidence": {
                "status": evidence_status,
                "minimum_descriptive_sample": 30,
                "realized_close_records": closed_count,
                "closed_positions": summary["closed_positions"],
                "realized_close_records_with_reviews": len(with_reviews),
                "limitations": [
                    "actual_fills_only",
                    "unrealized_remaining_quantity_excluded",
                    "review_association_is_not_causal_validation",
                    "fees_and_slippage_outside_recorded_net_fills_are_excluded",
                ],
            },
            "trades": trades,
        }
