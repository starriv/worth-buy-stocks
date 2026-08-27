"""Stable JSON contract for option-seller inputs and results."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

CONTRACT_VERSION = "worth-buy-stocks.options-seller.v1"
RESULT_KIND = "options_seller_result"


class OptionsContractError(ValueError):
    """Raised when an offline artifact violates the option-seller contract."""


def _datetime(value: Any, path: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise OptionsContractError(f"{path}: invalid ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _number(value: Any, path: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OptionsContractError(f"{path}: must be numeric") from None
    if not math.isfinite(number):
        raise OptionsContractError(f"{path}: must be finite")
    if minimum is not None and number < minimum:
        raise OptionsContractError(f"{path}: must be >= {minimum:g}")
    return number


def _validate_bar_rows(symbol: str, rows: Any) -> int:
    path = f"$.bars.{symbol}"
    if not isinstance(rows, list) or not rows:
        raise OptionsContractError(f"{path}: bars must be a non-empty list")
    previous = None
    for idx, row in enumerate(rows):
        row_path = f"{path}[{idx}]"
        if not isinstance(row, dict) or not all(k in row for k in ("t", "h", "l", "c", "v")):
            raise OptionsContractError(f"{row_path}: invalid OHLCV row")
        timestamp = _datetime(row["t"], f"{row_path}.t")
        if previous is not None and timestamp <= previous:
            raise OptionsContractError(f"{row_path}.t: timestamps must be unique and ascending")
        previous = timestamp
        high = _number(row["h"], f"{row_path}.h", minimum=0)
        low = _number(row["l"], f"{row_path}.l", minimum=0)
        close = _number(row["c"], f"{row_path}.c", minimum=0)
        _number(row["v"], f"{row_path}.v", minimum=0)
        if high < low or not low <= close <= high:
            raise OptionsContractError(f"{row_path}: OHLC price relationship is invalid")
    return len(rows)


def _validate_chain(symbol: str, chain: Any, as_of: dt.datetime) -> int:
    path = f"$.option_chains.{symbol}"
    if not isinstance(chain, dict):
        raise OptionsContractError(f"{path}: must be an object")
    if str(chain.get("feed") or "").lower() not in {"opra", "indicative"}:
        raise OptionsContractError(f"{path}.feed: must be opra or indicative")
    rows = chain.get("contracts")
    if not isinstance(rows, list):
        raise OptionsContractError(f"{path}.contracts: must be a list")
    for idx, row in enumerate(rows):
        row_path = f"{path}.contracts[{idx}]"
        if not isinstance(row, dict):
            raise OptionsContractError(f"{row_path}: must be an object")
        required = (
            "symbol", "underlying_symbol", "root_symbol", "expiration_date", "type",
            "style", "strike_price", "tradable", "open_interest", "multiplier",
            "quote", "greeks", "implied_volatility",
        )
        missing = [key for key in required if key not in row]
        if missing:
            raise OptionsContractError(f"{row_path}: missing {','.join(missing)}")
        if str(row["underlying_symbol"]).upper() != symbol:
            raise OptionsContractError(f"{row_path}.underlying_symbol: must match {symbol}")
        try:
            dt.date.fromisoformat(str(row["expiration_date"])[:10])
        except ValueError:
            raise OptionsContractError(f"{row_path}.expiration_date: invalid date") from None
        if row["type"] not in {"call", "put"}:
            raise OptionsContractError(f"{row_path}.type: must be call or put")
        _number(row["strike_price"], f"{row_path}.strike_price", minimum=0.01)
        _number(row["open_interest"], f"{row_path}.open_interest", minimum=0)
        _number(row["multiplier"], f"{row_path}.multiplier", minimum=1)
        quote = row["quote"]
        quote_keys = ("bid", "ask", "bid_size", "ask_size", "timestamp")
        if not isinstance(quote, dict) or not all(key in quote for key in quote_keys):
            raise OptionsContractError(
                f"{row_path}.quote: bid, ask, sizes and timestamp are required"
            )
        bid = _number(quote["bid"], f"{row_path}.quote.bid", minimum=0)
        ask = _number(quote["ask"], f"{row_path}.quote.ask", minimum=0)
        _number(quote["bid_size"], f"{row_path}.quote.bid_size", minimum=0)
        _number(quote["ask_size"], f"{row_path}.quote.ask_size", minimum=0)
        if ask < bid:
            raise OptionsContractError(f"{row_path}.quote: ask must be >= bid")
        quoted_at = _datetime(quote["timestamp"], f"{row_path}.quote.timestamp")
        if quoted_at > as_of + dt.timedelta(minutes=1):
            raise OptionsContractError(f"{row_path}.quote.timestamp: cannot be after as_of")
        greeks = row["greeks"]
        if not isinstance(greeks, dict) or "delta" not in greeks:
            raise OptionsContractError(f"{row_path}.greeks.delta: required")
        _number(greeks["delta"], f"{row_path}.greeks.delta")
        if row["implied_volatility"] is not None:
            _number(row["implied_volatility"], f"{row_path}.implied_volatility", minimum=0)
    return len(rows)


def _validate_events(symbol: str, payload: Any) -> None:
    path = f"$.events.{symbol}"
    if not isinstance(payload, dict):
        raise OptionsContractError(f"{path}: must be an object")
    status = payload.get("status")
    if status not in {"ok", "unavailable"}:
        raise OptionsContractError(f"{path}.status: must be ok or unavailable")
    if status == "ok":
        for key in ("earnings_dates", "ex_dividend_dates"):
            values = payload.get(key)
            if not isinstance(values, list):
                raise OptionsContractError(f"{path}.{key}: must be a list when status is ok")
            for idx, value in enumerate(values):
                try:
                    dt.date.fromisoformat(str(value)[:10])
                except ValueError:
                    raise OptionsContractError(f"{path}.{key}[{idx}]: invalid date") from None
        sources = payload.get("sources") or []
        if not isinstance(sources, list):
            raise OptionsContractError(f"{path}.sources: must be a list")
        source_ids = {
            str(value.get("id"))
            for value in sources
            if isinstance(value, dict) and value.get("id")
        }
        red_flags = payload.get("red_flags") or []
        if not isinstance(red_flags, list):
            raise OptionsContractError(f"{path}.red_flags: must be a list")
        for idx, value in enumerate(red_flags):
            source_id = str(value.get("source_id") or "") if isinstance(value, dict) else ""
            if not source_id or source_id not in source_ids:
                raise OptionsContractError(
                    f"{path}.red_flags[{idx}].source_id: must reference a source"
                )


def _validate_optional_overlays(
    payload: dict[str, Any],
    bars: dict[str, Any],
    *,
    validate_account_context: bool = True,
) -> None:
    account = payload.get("account_context")
    if validate_account_context and account is not None:
        if not isinstance(account, dict) or account.get("status") not in {"ok", "unavailable"}:
            raise OptionsContractError("$.account_context.status: must be ok or unavailable")
        if account.get("status") == "ok":
            account_row = account.get("account")
            positions = account.get("positions")
            if not isinstance(account_row, dict) or not isinstance(positions, (dict, list)):
                raise OptionsContractError("$.account_context: account and positions are required")
            equity = account_row.get("equity")
            if equity is None:
                equity = account_row.get("portfolio_value")
            if equity is not None:
                _number(equity, "$.account_context.account.equity", minimum=0.01)
    history = payload.get("iv_history")
    if history is not None:
        if not isinstance(history, dict):
            raise OptionsContractError("$.iv_history: must be an object")
        extras = set(history) - set(bars)
        if extras:
            raise OptionsContractError(f"$.iv_history: unknown symbols: {','.join(sorted(extras))}")
        for symbol, row in history.items():
            if not isinstance(row, dict) or row.get("status") not in {
                "ok", "insufficient_history", "unavailable",
            }:
                raise OptionsContractError(f"$.iv_history.{symbol}.status: invalid")
            for key in ("iv_rank", "iv_percentile"):
                if row.get(key) is not None:
                    value = _number(row[key], f"$.iv_history.{symbol}.{key}", minimum=0)
                    if value > 100:
                        raise OptionsContractError(f"$.iv_history.{symbol}.{key}: must be <= 100")


def validate_offline_input(
    payload: Any,
    *,
    required_symbols: list[str] | tuple[str, ...] | None = None,
    validate_account_context: bool = True,
) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise OptionsContractError("$: offline input must be an object")
    bars = payload.get("bars")
    chains = payload.get("option_chains")
    events = payload.get("events")
    if "as_of" not in payload:
        raise OptionsContractError("$.as_of: required")
    as_of = _datetime(payload.get("as_of"), "$.as_of")
    if not isinstance(bars, dict) or not bars:
        raise OptionsContractError("$.bars: must be a non-empty object")
    if not isinstance(chains, dict):
        raise OptionsContractError("$.option_chains: must be an object")
    if not isinstance(events, dict):
        raise OptionsContractError("$.events: must be an object")
    bar_count = 0
    contract_count = 0
    for symbol, rows in bars.items():
        if str(symbol) != str(symbol).upper():
            raise OptionsContractError(f"$.bars.{symbol}: symbol must be uppercase")
        bar_count += _validate_bar_rows(symbol, rows)
    for symbol, chain in chains.items():
        if symbol in bars:
            contract_count += _validate_chain(symbol, chain, as_of)
    for symbol, event_payload in events.items():
        if symbol in bars:
            _validate_events(symbol, event_payload)
    extras = (set(chains) | set(events)) - set(bars)
    if extras:
        raise OptionsContractError(f"$: option_chains/events contain unknown symbols: {','.join(sorted(extras))}")
    _validate_optional_overlays(
        payload, bars, validate_account_context=validate_account_context
    )
    for symbol in required_symbols or ():
        normalized = str(symbol).strip().upper()
        if normalized not in bars:
            raise OptionsContractError(f"$.bars.{normalized}: required")
        if normalized not in chains:
            raise OptionsContractError(f"$.option_chains.{normalized}: required")
        if normalized not in events:
            raise OptionsContractError(f"$.events.{normalized}: required")
    return {
        "symbols_count": len(bars),
        "bars_count": bar_count,
        "chains_count": len(chains),
        "contracts_count": contract_count,
    }


def validate_result(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise OptionsContractError("$: result must be an object")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise OptionsContractError("$.contract_version: unsupported version")
    if payload.get("kind") != RESULT_KIND:
        raise OptionsContractError("$.kind: must be options_seller_result")
    if payload.get("status") != "ok":
        raise OptionsContractError("$.status: must be ok")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise OptionsContractError("$.symbols: must be an object")
    candidates = 0
    for symbol, row in symbols.items():
        if str(symbol) != str(symbol).upper() or not isinstance(row, dict):
            raise OptionsContractError(f"$.symbols.{symbol}: invalid symbol result")
        if not isinstance(row.get("underlying"), dict):
            raise OptionsContractError(f"$.symbols.{symbol}.underlying: must be an object")
        rows = row.get("candidates")
        if not isinstance(rows, list):
            raise OptionsContractError(f"$.symbols.{symbol}.candidates: must be a list")
        for idx, candidate in enumerate(rows):
            score = candidate.get("score") if isinstance(candidate, dict) else None
            if not isinstance(score, dict) or not score.get("verdict"):
                raise OptionsContractError(f"$.symbols.{symbol}.candidates[{idx}].score: invalid")
            candidates += 1
    return {"symbols_count": len(symbols), "candidates_count": candidates}
