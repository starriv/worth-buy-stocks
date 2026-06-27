#!/usr/bin/env python3
"""Read-only Alpaca account/position context for practical exposure advice.

This module only calls `alpaca account get` and `alpaca position list`.
It never creates/modifies orders and it strips account identifiers from output.
"""
import datetime
import json
import subprocess

from utils import to_num as _num


def _round_num(value, ndigits=4):
    n = _num(value)
    return round(n, ndigits) if n is not None else None


def _pct_from_factor(value):
    n = _num(value)
    return round(n * 100, 2) if n is not None else None


def _run_json(cmd, timeout):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        detail = (res.stderr.strip() or res.stdout.strip() or "unknown error").replace("\n", " ")
        raise RuntimeError(detail[:300])
    text = res.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"无法解析 alpaca 输出为 JSON: {e}; 输出前 200 字符: {text[:200]!r}")


def _normalize_account(account):
    account = account if isinstance(account, dict) else {}
    equity = _round_num(account.get("equity") or account.get("portfolio_value"), 2)
    cash = _round_num(account.get("cash"), 2)
    buying_power = _round_num(account.get("buying_power"), 2)
    long_market_value = _round_num(account.get("long_market_value"), 2)
    short_market_value = _round_num(account.get("short_market_value"), 2)
    out = {
        "equity": equity,
        "portfolio_value": _round_num(account.get("portfolio_value"), 2),
        "cash": cash,
        "buying_power": buying_power,
        "long_market_value": long_market_value,
        "short_market_value": short_market_value,
        "cash_pct": round(cash / equity * 100, 2) if equity and cash is not None else None,
        "long_exposure_pct": (
            round(long_market_value / equity * 100, 2)
            if equity and long_market_value is not None else None
        ),
        "pattern_day_trader": account.get("pattern_day_trader"),
        "trading_blocked": account.get("trading_blocked"),
        "account_blocked": account.get("account_blocked"),
        "status": account.get("status"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _positions_iter(raw_positions):
    if raw_positions is None:
        return []
    if isinstance(raw_positions, list):
        return raw_positions
    if isinstance(raw_positions, dict):
        if raw_positions.get("symbol"):
            return [raw_positions]
        out = []
        for sym, pos in raw_positions.items():
            if isinstance(pos, dict):
                p = dict(pos)
                p.setdefault("symbol", sym)
                out.append(p)
        return out
    return []


def _normalize_position(position):
    position = position if isinstance(position, dict) else {}
    symbol = str(position.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    current_price = _round_num(position.get("current_price"), 4)
    market_value = _round_num(position.get("market_value"), 2)
    out = {
        "symbol": symbol,
        "asset_class": position.get("asset_class"),
        "side": str(position.get("side") or "long").strip().lower(),
        "qty": _round_num(position.get("qty"), 6),
        "qty_available": _round_num(position.get("qty_available"), 6),
        "avg_entry_price": _round_num(position.get("avg_entry_price"), 4),
        "current_price": current_price,
        "lastday_price": _round_num(position.get("lastday_price"), 4),
        "cost_basis": _round_num(position.get("cost_basis"), 2),
        "market_value": market_value,
        "unrealized_pl": _round_num(position.get("unrealized_pl"), 2),
        "unrealized_pl_pct": _pct_from_factor(position.get("unrealized_plpc")),
        "unrealized_intraday_pl": _round_num(position.get("unrealized_intraday_pl"), 2),
        "unrealized_intraday_pl_pct": _pct_from_factor(position.get("unrealized_intraday_plpc")),
        "change_today_pct": _pct_from_factor(position.get("change_today")),
    }
    return {k: v for k, v in out.items() if v is not None}


def normalize_account_context(raw):
    """Normalize Alpaca account + positions JSON into a compact, safe context.

    Accepted shapes:
      {"account": {...}, "positions": [{...}, ...]}
      {"account": {...}, "positions": {"AAPL": {...}}}
      {"status": "unavailable", "reason": "..."}
    """
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "账户上下文 JSON 必须是 object"}
    if raw.get("status") == "unavailable":
        return {
            "status": "unavailable",
            "reason": str(raw.get("reason") or "Alpaca 账户上下文不可用")[:300],
        }

    account = _normalize_account(raw.get("account") or raw)
    positions = {}
    for p in _positions_iter(raw.get("positions")):
        normalized = _normalize_position(p)
        if normalized:
            positions[normalized["symbol"]] = normalized
    return {
        "status": "ok",
        "as_of": raw.get("as_of") or datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "account": account,
        "positions": positions,
        "positions_count": len(positions),
    }


def fetch_account_context(timeout=30):
    """Fetch account and open positions through Alpaca CLI, then normalize."""
    account = _run_json(
        ["alpaca", "account", "get", "--quiet", "--timeout", str(timeout)],
        timeout + 5,
    )
    positions = _run_json(
        ["alpaca", "position", "list", "--quiet", "--timeout", str(timeout)],
        timeout + 5,
    )
    return normalize_account_context({"account": account, "positions": positions})


def context_for_symbol(account_context, symbol):
    """Return a per-symbol account context for scoring."""
    if not isinstance(account_context, dict):
        return None
    if account_context.get("status") != "ok":
        return {
            "status": "unavailable",
            "reason": account_context.get("reason") or "Alpaca 账户上下文不可用",
        }
    sym = str(symbol or "").strip().upper()
    return {
        "status": "ok",
        "as_of": account_context.get("as_of"),
        "account": account_context.get("account") or {},
        "position": (account_context.get("positions") or {}).get(sym),
        "positions_count": account_context.get("positions_count"),
    }


def summarize_account_context(account_context):
    """Small top-level summary for indicators.py output."""
    if not isinstance(account_context, dict):
        return None
    if account_context.get("status") != "ok":
        return {
            "status": "unavailable",
            "reason": account_context.get("reason") or "Alpaca 账户上下文不可用",
        }
    account = account_context.get("account") or {}
    return {
        "status": "ok",
        "as_of": account_context.get("as_of"),
        "equity": account.get("equity"),
        "cash_pct": account.get("cash_pct"),
        "long_exposure_pct": account.get("long_exposure_pct"),
        "positions_count": account_context.get("positions_count"),
        "trading_blocked": account.get("trading_blocked"),
        "account_blocked": account.get("account_blocked"),
    }
