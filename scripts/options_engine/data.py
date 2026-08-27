"""Read-only Alpaca CLI transports and option-chain normalization."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fetching import _is_feed_limit, _run_json


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _boolean(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _page_token(payload: Any) -> str:
    return str((payload or {}).get("next_page_token") or "") if isinstance(payload, dict) else ""


def fetch_option_contracts(
    symbol: str,
    expiration_gte: str,
    expiration_lte: str,
    *,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Fetch every active contract page for one underlying."""
    contracts: list[dict] = []
    token = ""
    while True:
        cmd = [
            "alpaca", "option", "contracts",
            "--underlying-symbols", symbol,
            "--expiration-date-gte", expiration_gte,
            "--expiration-date-lte", expiration_lte,
            "--status", "active",
            "--limit", "10000",
            "--quiet",
        ]
        if strike_gte is not None:
            cmd += ["--strike-price-gte", str(round(strike_gte, 4))]
        if strike_lte is not None:
            cmd += ["--strike-price-lte", str(round(strike_lte, 4))]
        if token:
            cmd += ["--page-token", token]
        payload = _run_json(cmd, timeout)
        rows = (payload or {}).get("option_contracts") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            contracts.extend(row for row in rows if isinstance(row, dict))
        token = _page_token(payload)
        if not token:
            return contracts


def _fetch_option_chain_pages(
    symbol: str,
    expiration_gte: str,
    expiration_lte: str,
    feed: str,
    *,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    timeout: int = 30,
) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    token = ""
    while True:
        cmd = [
            "alpaca", "data", "option", "chain",
            "--underlying-symbol", symbol,
            "--expiration-date-gte", expiration_gte,
            "--expiration-date-lte", expiration_lte,
            "--feed", feed,
            "--limit", "1000",
            "--quiet",
        ]
        if strike_gte is not None:
            cmd += ["--strike-price-gte", str(round(strike_gte, 4))]
        if strike_lte is not None:
            cmd += ["--strike-price-lte", str(round(strike_lte, 4))]
        if token:
            cmd += ["--page-token", token]
        payload = _run_json(cmd, timeout)
        rows = (payload or {}).get("snapshots") if isinstance(payload, dict) else None
        if isinstance(rows, dict):
            snapshots.update({str(k): v for k, v in rows.items() if isinstance(v, dict)})
        token = _page_token(payload)
        if not token:
            return snapshots


def fetch_option_chain(
    symbol: str,
    expiration_gte: str,
    expiration_lte: str,
    *,
    feed: str = "auto",
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    timeout: int = 30,
) -> tuple[dict[str, dict], str, list[str]]:
    """Fetch every chain page and conservatively fall back to indicative data."""
    requested = str(feed or "auto").lower()
    if requested not in {"auto", "opra", "indicative"}:
        raise ValueError("option feed must be auto, opra, or indicative")
    first = "opra" if requested == "auto" else requested
    flags: list[str] = []
    try:
        return _fetch_option_chain_pages(
            symbol, expiration_gte, expiration_lte, first,
            strike_gte=strike_gte, strike_lte=strike_lte, timeout=timeout,
        ), first, flags
    except RuntimeError as exc:
        if requested != "auto" or not _is_feed_limit(str(exc)):
            raise
        flags.append("OPRA 不可用，已回退 indicative；候选最多维持观察")
        return _fetch_option_chain_pages(
            symbol, expiration_gte, expiration_lte, "indicative",
            strike_gte=strike_gte, strike_lte=strike_lte, timeout=timeout,
        ), "indicative", flags


def _quote(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    bid = _num(value.get("bp", value.get("bid")))
    ask = _num(value.get("ap", value.get("ask")))
    timestamp = value.get("t", value.get("timestamp"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread_pct = (
        (ask - bid) / mid * 100
        if mid is not None and mid > 0 and ask is not None and bid is not None
        else None
    )
    return {
        "bid": round(bid, 4) if bid is not None else None,
        "ask": round(ask, 4) if ask is not None else None,
        "bid_size": _integer(value.get("bs", value.get("bid_size"))),
        "ask_size": _integer(value.get("as", value.get("ask_size"))),
        "bid_exchange": value.get("bx", value.get("bid_exchange")),
        "ask_exchange": value.get("ax", value.get("ask_exchange")),
        "conditions": value.get("c", value.get("conditions")) or [],
        "mid": round(mid, 4) if mid is not None else None,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "timestamp": str(timestamp) if timestamp else None,
    }


def normalize_option_chain(
    symbol: str,
    contracts: list[dict],
    snapshots: dict[str, dict],
    *,
    feed: str,
    as_of: str | None = None,
    data_flags: list[str] | None = None,
) -> dict[str, Any]:
    """Join contract metadata with latest quote, IV, and Greeks."""
    normalized = []
    by_symbol = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in contracts or [] if isinstance(row, dict)
    }
    for contract_symbol in sorted(set(by_symbol) | set(snapshots or {})):
        meta = by_symbol.get(contract_symbol, {})
        snap = (snapshots or {}).get(contract_symbol) or {}
        quote_raw = snap.get("latestQuote") or snap.get("latest_quote") or meta.get("quote") or {}
        greeks = snap.get("greeks") or meta.get("greeks") or {}
        expiration = meta.get("expiration_date") or meta.get("expiration")
        option_type = meta.get("type") or meta.get("option_type")
        strike = _num(meta.get("strike_price", meta.get("strike")))
        if not expiration or option_type not in {"call", "put"} or strike is None:
            continue
        iv = _num(
            snap.get("impliedVolatility", snap.get("implied_volatility", meta.get("implied_volatility")))
        )
        normalized.append({
            "symbol": contract_symbol,
            "underlying_symbol": str(meta.get("underlying_symbol") or symbol).strip().upper(),
            "root_symbol": str(meta.get("root_symbol") or symbol).strip().upper(),
            "expiration_date": str(expiration),
            "type": str(option_type),
            "style": str(meta.get("style") or "american"),
            "strike_price": round(strike, 4),
            "tradable": _boolean(meta.get("tradable"), True),
            "open_interest": _integer(meta.get("open_interest")),
            "open_interest_date": meta.get("open_interest_date"),
            "multiplier": _integer(meta.get("multiplier", meta.get("size", 100))) or 100,
            "quote": _quote(quote_raw),
            "implied_volatility": round(iv, 6) if iv is not None else None,
            "greeks": {
                key: _num(greeks.get(key))
                for key in ("delta", "gamma", "theta", "vega", "rho")
            },
        })
    status = "ok" if normalized else "unavailable"
    out = {
        "status": status,
        "symbol": str(symbol).strip().upper(),
        "feed": feed,
        "as_of": as_of or _utc_now(),
        "contracts": normalized,
        "data_flags": list(data_flags or []),
    }
    if status != "ok":
        out["reason"] = "期权链没有可归一化合约"
    return out


def normalize_offline_chain(symbol: str, payload: Any) -> dict[str, Any]:
    """Normalize either canonical rows or raw Alpaca contract/snapshot payloads."""
    if not isinstance(payload, dict):
        return {"status": "unavailable", "symbol": symbol, "reason": "离线期权链必须是 object"}
    rows = payload.get("contracts")
    if isinstance(rows, list) and rows and all(isinstance(r.get("quote"), dict) for r in rows if isinstance(r, dict)):
        # Re-normalize canonical rows through a synthetic snapshot so numeric and
        # naming rules stay identical to the live path.
        contracts = []
        snapshots = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            contracts.append(row)
            snapshots[str(row.get("symbol") or "").strip().upper()] = {
                "latestQuote": row.get("quote"),
                "greeks": row.get("greeks"),
                "implied_volatility": row.get("implied_volatility"),
            }
    else:
        contracts = payload.get("option_contracts") or rows or []
        snapshots = payload.get("snapshots") or {}
    return normalize_option_chain(
        symbol, contracts, snapshots,
        feed=str(payload.get("feed") or "offline"),
        as_of=payload.get("as_of"),
        data_flags=payload.get("data_flags") or [],
    )


def fetch_normalized_chain(
    symbol: str,
    expiration_gte: str,
    expiration_lte: str,
    *,
    feed: str = "auto",
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Fetch contracts and snapshots concurrently, then return canonical rows."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        contracts_future = pool.submit(
            fetch_option_contracts, symbol, expiration_gte, expiration_lte,
            strike_gte=strike_gte, strike_lte=strike_lte, timeout=timeout,
        )
        chain_future = pool.submit(
            fetch_option_chain, symbol, expiration_gte, expiration_lte,
            feed=feed, strike_gte=strike_gte, strike_lte=strike_lte, timeout=timeout,
        )
        contracts = contracts_future.result()
        snapshots, used_feed, flags = chain_future.result()
    return normalize_option_chain(
        symbol, contracts, snapshots, feed=used_feed, data_flags=flags
    )
