#!/usr/bin/env python3
"""Validation helpers for multi-agent JSON artifacts.

The validators are intentionally lightweight and standard-library only. They
check that sub-agent handoff files are structurally safe before the main agent
passes them to ``indicators.py``.
"""
from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "worth-buy-stocks.agent.v1"

KIND_NEWS = "news_context"
KIND_ACCOUNT = "account_context"
KIND_FINNHUB = "finnhub_context"
KIND_BARS = "bars"
KIND_SNAPSHOT = "snapshot_context"
KIND_RESULT = "result"

KINDS = (KIND_NEWS, KIND_ACCOUNT, KIND_FINNHUB, KIND_BARS, KIND_SNAPSHOT, KIND_RESULT)
UNAVAILABLE_STATUSES = {"unavailable", "unauthorized", "rate_limited"}
META_KEYS = {
    "contract_version", "kind", "status", "reason", "as_of", "provider",
    "feed", "adjustment", "account", "positions", "bars",
}


class ContractError(ValueError):
    """Raised when an agent artifact violates the expected shape."""


def _fail(path: str, message: str) -> None:
    raise ContractError(f"{path}: {message}")


def _expect(condition: bool, path: str, message: str) -> None:
    if not condition:
        _fail(path, message)


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _status(payload: dict[str, Any], path: str, allowed_extra: set[str] | None = None) -> str:
    status = str(payload.get("status") or "ok")
    allowed = {"ok"} | UNAVAILABLE_STATUSES | (allowed_extra or set())
    _expect(status in allowed, path, f"status must be one of {sorted(allowed)}")
    if status != "ok":
        _expect(bool(str(payload.get("reason") or "").strip()), path, "non-ok status requires reason")
    return status


def _symbols_from_direct_or_wrapper(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("symbols"), dict):
        return payload["symbols"]
    return {
        str(k).strip().upper(): v
        for k, v in payload.items()
        if k not in META_KEYS and isinstance(v, dict)
    }


def _validate_symbol_key(symbol: str, path: str) -> None:
    _expect(bool(symbol.strip()), path, "symbol key must be non-empty")
    _expect(symbol == symbol.upper(), path, "symbol key must be uppercase")


def _validate_sources(sources: Any, path: str) -> None:
    if sources is None:
        return
    _expect(isinstance(sources, list), path, "sources must be a list")
    for i, source in enumerate(sources):
        sp = f"{path}[{i}]"
        _expect(_is_obj(source), sp, "source must be an object")
        if "id" in source:
            _expect(bool(str(source["id"]).strip()), f"{sp}.id", "id must be non-empty")
        if "url" in source:
            _expect(str(source["url"]).startswith(("http://", "https://")), f"{sp}.url", "url must be http(s)")


def _validate_red_flags(red_flags: Any, path: str) -> None:
    if red_flags is None:
        return
    _expect(isinstance(red_flags, list), path, "red_flags must be a list")
    for i, flag in enumerate(red_flags):
        fp = f"{path}[{i}]"
        _expect(_is_obj(flag), fp, "red flag must be an object")
        sev = str(flag.get("severity") or "").lower()
        _expect(sev in {"high", "medium", "low"}, f"{fp}.severity", "severity must be high, medium, or low")
        _expect(bool(str(flag.get("type") or "").strip()), f"{fp}.type", "type is required")
        _expect(bool(str(flag.get("note") or "").strip()), f"{fp}.note", "note is required")


def validate_news_context(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "news_context must be an object")
    status = _status(payload, "$")
    if status != "ok":
        return {"symbols_count": 0}
    symbols = _symbols_from_direct_or_wrapper(payload)
    for symbol, ctx in symbols.items():
        sp = f"$.symbols.{symbol}"
        _validate_symbol_key(symbol, sp)
        _expect(_is_obj(ctx), sp, "symbol context must be an object")
        data_trust = ctx.get("data_trust")
        if data_trust is not None:
            _expect(
                str(data_trust).lower() in {"ok", "suspect", "unverified", "stale", "bad", "unknown"},
                f"{sp}.data_trust",
                "data_trust must be ok/suspect/unverified/stale/bad/unknown",
            )
        _validate_sources(ctx.get("sources"), f"{sp}.sources")
        _validate_red_flags(ctx.get("red_flags"), f"{sp}.red_flags")
    return {"symbols_count": len(symbols)}


def validate_account_context(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "account_context must be an object")
    status = _status(payload, "$")
    if status != "ok":
        return {"positions_count": 0}
    _expect(_is_obj(payload.get("account")), "$.account", "account must be an object")
    positions = payload.get("positions", [])
    _expect(isinstance(positions, (list, dict)), "$.positions", "positions must be a list or object")
    items = positions if isinstance(positions, list) else list(positions.values())
    for i, pos in enumerate(items):
        pp = f"$.positions[{i}]"
        _expect(_is_obj(pos), pp, "position must be an object")
        if pos:
            _expect(bool(str(pos.get("symbol") or "").strip()), f"{pp}.symbol", "symbol is required")
    return {"positions_count": len(items)}


def validate_finnhub_context(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "finnhub_context must be an object")
    status = _status(payload, "$")
    if status != "ok":
        return {"symbols_count": 0}
    symbols = payload.get("symbols")
    _expect(isinstance(symbols, dict), "$.symbols", "symbols must be an object")
    for symbol, ctx in symbols.items():
        sp = f"$.symbols.{symbol}"
        _validate_symbol_key(str(symbol), sp)
        _expect(_is_obj(ctx), sp, "symbol context must be an object")
        _status(ctx, sp)
        for key in ("news", "earnings", "data_flags"):
            if key in ctx:
                _expect(isinstance(ctx[key], list), f"{sp}.{key}", f"{key} must be a list")
        for key in ("quote", "profile"):
            if key in ctx:
                _expect(_is_obj(ctx[key]), f"{sp}.{key}", f"{key} must be an object")
    return {"symbols_count": len(symbols)}


def _validate_number(value: Any, path: str) -> None:
    _expect(isinstance(value, (int, float)) and not isinstance(value, bool), path, "must be a number")


def validate_bars(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "bars artifact must be an object")
    status = _status(payload, "$")
    _expect(status == "ok", "$.status", "bars artifact must have status=ok")
    bars = payload.get("bars")
    _expect(isinstance(bars, dict) and bars, "$.bars", "bars must be a non-empty object")
    rows = 0
    for symbol, blist in bars.items():
        sp = f"$.bars.{symbol}"
        _validate_symbol_key(str(symbol), sp)
        _expect(isinstance(blist, list), sp, "symbol bars must be a list")
        for i, bar in enumerate(blist):
            bp = f"{sp}[{i}]"
            _expect(_is_obj(bar), bp, "bar must be an object")
            for key in ("t", "o", "h", "l", "c", "v"):
                _expect(key in bar, f"{bp}.{key}", "required bar field is missing")
            _expect(bool(str(bar["t"]).strip()), f"{bp}.t", "timestamp is required")
            for key in ("o", "h", "l", "c", "v"):
                _validate_number(bar[key], f"{bp}.{key}")
            rows += 1
    return {"symbols_count": len(bars), "bars_count": rows}


def validate_result(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "result must be an object")
    if "error" in payload:
        _fail("$.error", "result contains top-level error")
    symbols = payload.get("symbols")
    _expect(isinstance(symbols, dict) and symbols, "$.symbols", "symbols must be a non-empty object")
    scored = 0
    for symbol, ctx in symbols.items():
        sp = f"$.symbols.{symbol}"
        _validate_symbol_key(str(symbol), sp)
        _expect(_is_obj(ctx), sp, "symbol result must be an object")
        if "error" in ctx:
            continue
        score = ctx.get("score")
        _expect(_is_obj(score), f"{sp}.score", "non-error symbol requires score")
        _expect(bool(str(score.get("verdict") or "").strip()), f"{sp}.score.verdict", "verdict is required")
        _expect("composite" in score, f"{sp}.score.composite", "composite is required")
        _expect(isinstance(score.get("blocking_reasons"), list), f"{sp}.score.blocking_reasons", "blocking_reasons must be a list")
        _expect(_is_obj(score.get("trade_plan")), f"{sp}.score.trade_plan", "trade_plan must be an object")
        _expect(_is_obj(score.get("account_overlay")), f"{sp}.score.account_overlay", "account_overlay must be an object")
        scored += 1
    return {"symbols_count": len(symbols), "scored_symbols_count": scored}


def validate_snapshot(payload: Any) -> dict[str, Any]:
    _expect(_is_obj(payload), "$", "snapshot_context must be an object")
    status = _status(payload, "$")
    if status != "ok":
        return {"symbols_count": 0}
    symbols = payload.get("symbols")
    _expect(isinstance(symbols, dict), "$.symbols", "symbols must be an object")
    for symbol, ctx in symbols.items():
        sp = f"$.symbols.{symbol}"
        _validate_symbol_key(str(symbol), sp)
        _expect(_is_obj(ctx), sp, "symbol snapshot must be an object")
        _expect(
            str(ctx.get("symbol") or symbol).strip().upper() == str(symbol).strip().upper(),
            f"{sp}.symbol",
            "symbol field must match key",
        )
        for key in ("daily_bar", "quote", "latest_trade", "minute_bar"):
            if key in ctx:
                _expect(_is_obj(ctx[key]), f"{sp}.{key}", f"{key} must be an object")
    return {"symbols_count": len(symbols)}


VALIDATORS = {
    KIND_NEWS: validate_news_context,
    KIND_ACCOUNT: validate_account_context,
    KIND_FINNHUB: validate_finnhub_context,
    KIND_BARS: validate_bars,
    KIND_SNAPSHOT: validate_snapshot,
    KIND_RESULT: validate_result,
}


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate an artifact and return a compact summary."""
    _expect(kind in VALIDATORS, "$.kind", f"kind must be one of {', '.join(KINDS)}")
    if _is_obj(payload) and payload.get("contract_version") is not None:
        _expect(
            payload["contract_version"] == CONTRACT_VERSION,
            "$.contract_version",
            f"contract_version must be {CONTRACT_VERSION}",
        )
    if _is_obj(payload) and payload.get("kind") is not None:
        _expect(payload["kind"] == kind, "$.kind", f"kind must be {kind}")
    details = VALIDATORS[kind](payload)
    version = payload.get("contract_version") if _is_obj(payload) else None
    return {
        "status": "ok",
        "kind": kind,
        "contract_version": version or CONTRACT_VERSION,
        **details,
    }
