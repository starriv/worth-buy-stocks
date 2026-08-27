"""Normalize earnings and ex-dividend dates into deterministic risk gates."""
from __future__ import annotations

import datetime as dt
from typing import Any


_MACRO_EVENT_TYPES = {"cpi", "pce", "employment", "fomc", "gdp"}


def _date(value: Any) -> str | None:
    text = str(value or "").strip()[:10]
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_events(
    payload: Any,
    symbol: str,
    *,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Accept compact offline events or a Finnhub symbol context."""
    raw = payload if isinstance(payload, dict) else {}
    wrapper_status = str(raw.get("status") or "")
    if isinstance(raw.get("symbols"), dict):
        raw = raw["symbols"].get(str(symbol).upper()) or {}
    status = str(raw.get("status") or wrapper_status or "unavailable")
    earnings_present = "earnings_dates" in raw or "earnings" in raw
    dividends_present = "ex_dividend_dates" in raw or "dividends" in raw
    earnings_values = raw.get("earnings_dates", raw.get("earnings", []))
    dividend_values = raw.get("ex_dividend_dates", raw.get("dividends", []))
    red_flag_values = raw.get("red_flags") or []
    source_values = raw.get("sources") or []
    cutoff_end = _date(as_of_date) if as_of_date else None
    cutoff_start = (
        (dt.date.fromisoformat(cutoff_end) - dt.timedelta(days=30)).isoformat()
        if cutoff_end else None
    )
    source_ids = set()
    for value in source_values:
        if not isinstance(value, dict) or not value.get("id"):
            continue
        published = _date(value.get("published_at"))
        if cutoff_end and (not published or published < cutoff_start or published > cutoff_end):
            continue
        source_ids.add(str(value["id"]))

    earnings = []
    for value in earnings_values if isinstance(earnings_values, list) else []:
        parsed = _date(value.get("date") if isinstance(value, dict) else value)
        if parsed and parsed not in earnings:
            earnings.append(parsed)
    ex_dividends = []
    for value in dividend_values if isinstance(dividend_values, list) else []:
        if isinstance(value, dict):
            candidate = value.get("ex_date", value.get("exDate", value.get("date")))
        else:
            candidate = value
        parsed = _date(candidate)
        if parsed and parsed not in ex_dividends:
            ex_dividends.append(parsed)
    red_flags = []
    for value in red_flag_values if isinstance(red_flag_values, list) else []:
        if not isinstance(value, dict):
            continue
        severity = str(value.get("severity") or "").lower()
        kind = str(value.get("type") or "").strip()
        source_id = str(value.get("source_id") or "")
        if severity in {"medium", "high"} and kind and source_id in source_ids:
            red_flags.append({
                "type": kind,
                "severity": severity,
                "source_id": source_id,
            })
    complete = (
        status == "ok"
        and earnings_present
        and dividends_present
        and isinstance(earnings_values, list)
        and isinstance(dividend_values, list)
    )
    return {
        "status": "ok" if complete else "unavailable",
        "earnings_dates": sorted(earnings),
        "ex_dividend_dates": sorted(ex_dividends),
        "red_flags": red_flags,
        "sources": [
            value for value in source_values
            if isinstance(value, dict) and str(value.get("id") or "") in source_ids
        ],
    }


def event_gates(
    strategy: str,
    expiration_date: str,
    events: dict[str, Any],
    *,
    as_of_date: str | None = None,
) -> list[str]:
    """Return hard event gates that occur on or before spread expiration."""
    expiry = _date(expiration_date)
    if not expiry:
        return ["invalid_expiration_date"]
    start = _date(as_of_date) if as_of_date else None

    def in_window(value: Any) -> bool:
        day = _date(value)
        return bool(day and (start is None or start <= day) and day <= expiry)

    reasons = []
    if any(in_window(day) for day in events.get("earnings_dates") or []):
        reasons.append("earnings_before_expiration")
    if strategy in {"bear_call", "iron_condor", "iron_butterfly"} and any(
        in_window(day) for day in events.get("ex_dividend_dates") or []
    ):
        reasons.append("ex_dividend_before_expiration")
    if any(
        flag.get("severity") in {"medium", "high"}
        and flag.get("type") != "earnings_event"
        for flag in events.get("red_flags") or []
        if isinstance(flag, dict)
    ):
        reasons.append("material_event_risk")
    return reasons


def normalize_macro_events(payload: Any) -> dict[str, Any]:
    """Normalize a compact US high-impact macro calendar."""
    raw = payload if isinstance(payload, dict) else {}
    status = str(raw.get("status") or "unavailable")
    values = raw.get("events")
    if not isinstance(values, list):
        return {"status": "unavailable", "events": []}
    events = []
    for value in values:
        if not isinstance(value, dict):
            continue
        day = _date(value.get("date"))
        event_type = str(value.get("type") or "").strip().lower()
        country = str(value.get("country") or "US").strip().upper()
        if day and country == "US" and event_type in _MACRO_EVENT_TYPES:
            events.append({
                "date": day,
                "type": event_type,
                "name": str(value.get("name") or event_type).strip(),
                "importance": str(value.get("importance") or "high").strip().lower(),
                "source": value.get("source"),
            })
    events.sort(key=lambda row: (row["date"], row["type"], row["name"]))
    return {"status": "ok" if status == "ok" else "unavailable", "events": events}


def _weekdays_until(start: dt.date, end: dt.date) -> int:
    if end <= start:
        return 0 if end == start else -1
    cursor, count = start, 0
    while cursor < end:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


def macro_event_gates(
    macro_events: Any,
    *,
    as_of_date: str,
    blackout_days: int,
) -> tuple[list[str], list[str]]:
    """Block new credit during a same-day/next-trading-day macro window."""
    if macro_events is None:
        return [], []
    normalized = normalize_macro_events(macro_events)
    if normalized.get("status") != "ok":
        return [], ["macro_event_calendar_unavailable"]
    start_text = _date(as_of_date)
    if not start_text:
        return [], ["macro_event_as_of_date_invalid"]
    start = dt.date.fromisoformat(start_text)
    reasons = []
    for event in normalized["events"]:
        event_date = dt.date.fromisoformat(event["date"])
        sessions = _weekdays_until(start, event_date)
        if 0 <= sessions <= blackout_days:
            reasons.append(f"macro_event_blackout:{event['type']}:{event['date']}")
    return sorted(set(reasons)), []


def extract_corporate_action_events(payload: Any) -> dict[str, dict[str, list[str]]]:
    """Best-effort extraction across Alpaca corporate-action response shapes."""
    out: dict[str, dict[str, list[str]]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        symbol = str(
            value.get("symbol") or value.get("initiating_symbol") or value.get("initiatingSymbol") or ""
        ).strip().upper()
        ex_date = _date(value.get("ex_date", value.get("exDate")))
        if symbol and ex_date:
            events = out.setdefault(symbol, {"earnings_dates": [], "ex_dividend_dates": []})
            if ex_date not in events["ex_dividend_dates"]:
                events["ex_dividend_dates"].append(ex_date)
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(payload)
    for events in out.values():
        events["ex_dividend_dates"].sort()
    return out
