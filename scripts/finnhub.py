#!/usr/bin/env python3
"""Read-only Finnhub supplemental data client.

Finnhub is optional. It supplements Alpaca with quote cross-checks, company
profile, company news, and earnings-calendar context. It never replaces the
Alpaca price/volume scoring chain and never prints the API token.
"""
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

from local_env import get_env
from utils import to_num as _num

BASE_URL = "https://finnhub.io/api/v1"
TOKEN_ENV = "FINNHUB_API_KEY"
BENCHMARK_SYMBOLS = {"SPY", "QQQ"}
EARNINGS_EVENT_WINDOW_DAYS = 7

NEWS_RED_FLAG_RULES = [
    ("bankruptcy", "high", (
        "bankruptcy", "chapter 11", "insolvency", "liquidation",
    )),
    ("delisting", "high", (
        "delisting", "delist", "trading halt", "halted trading", "suspended trading",
    )),
    ("going_concern", "high", (
        "going concern", "substantial doubt", "continue as a going concern",
    )),
    ("fraud", "high", (
        "accounting fraud", "securities fraud", "fraud charges", "charged with fraud",
        "sec charges", "doj charges",
    )),
    ("restatement", "high", (
        "restate", "restatement", "material weakness",
    )),
    ("dilution", "medium", (
        "secondary offering", "share offering", "stock offering", "public offering",
        "registered direct offering", "at-the-market offering", "atm offering",
        "dilution", "dilutive",
    )),
    ("investigation", "medium", (
        "sec investigation", "doj investigation", "regulatory investigation",
        "probe", "subpoena",
    )),
    ("litigation", "medium", (
        "class action", "lawsuit", "sued", "settlement", "antitrust",
    )),
    ("operational_risk", "medium", (
        "recall", "data breach", "cyberattack", "production halt",
    )),
    ("guidance_cut", "medium", (
        "cuts guidance", "cut guidance", "lowers guidance", "reduced guidance",
        "warns on profit", "profit warning",
    )),
]


class FinnhubError(RuntimeError):
    """Structured API error with a stable status for callers."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def has_api_key():
    return bool(get_env(TOKEN_ENV).strip())


def _token(token=None):
    return (token if token is not None else get_env(TOKEN_ENV)).strip()


def _status_for_http(code):
    if code == 401 or code == 403:
        return "unauthorized"
    if code == 429:
        return "rate_limited"
    return "unavailable"


def _request_json(path, params, token=None, timeout=15):
    tok = _token(token)
    if not tok:
        raise FinnhubError("unavailable", f"{TOKEN_ENV} 未配置")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"X-Finnhub-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise FinnhubError(_status_for_http(e.code), f"Finnhub HTTP {e.code}: {detail}") from None
    except Exception as e:  # noqa: BLE001
        raise FinnhubError("unavailable", f"Finnhub 请求失败: {e}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FinnhubError("unavailable", f"Finnhub JSON 解析失败: {e}") from None


def _round(value, ndigits=4):
    n = _num(value)
    return round(n, ndigits) if n is not None else None


def _date_from_ts(value):
    n = _num(value)
    if n is None or n <= 0:
        return None
    return datetime.datetime.fromtimestamp(n, datetime.timezone.utc).date().isoformat()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_quote(data):
    data = data if isinstance(data, dict) else {}
    out = {
        "current_price": _round(data.get("c"), 4),
        "change": _round(data.get("d"), 4),
        "change_pct": _round(data.get("dp"), 4),
        "high": _round(data.get("h"), 4),
        "low": _round(data.get("l"), 4),
        "open": _round(data.get("o"), 4),
        "previous_close": _round(data.get("pc"), 4),
        "timestamp": int(data.get("t")) if _num(data.get("t")) is not None else None,
        "date": _date_from_ts(data.get("t")),
    }
    return {k: v for k, v in out.items() if v is not None}


def _normalize_profile(data):
    data = data if isinstance(data, dict) else {}
    keys = (
        "ticker", "name", "exchange", "country", "currency", "finnhubIndustry",
        "ipo", "weburl", "logo",
    )
    out = {k: data.get(k) for k in keys if data.get(k) not in (None, "")}
    if data.get("marketCapitalization") not in (None, ""):
        out["market_cap_millions"] = _round(data.get("marketCapitalization"), 2)
    if data.get("shareOutstanding") not in (None, ""):
        out["share_outstanding_millions"] = _round(data.get("shareOutstanding"), 2)
    return out


def _normalize_news(items, max_items=10):
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {
            "id": str(item.get("id")) if item.get("id") is not None else None,
            "headline": item.get("headline"),
            "source": item.get("source"),
            "summary": item.get("summary"),
            "url": item.get("url"),
            "published_at": _date_from_ts(item.get("datetime")),
            "category": item.get("category"),
        }
        row = {k: v for k, v in row.items() if v not in (None, "")}
        if row.get("headline") or row.get("url"):
            out.append(row)
    out.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    return out[:max_items]


def _normalize_earnings(data):
    data = data if isinstance(data, dict) else {}
    items = data.get("earningsCalendar")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {
            "date": item.get("date"),
            "hour": item.get("hour"),
            "eps_estimate": _round(item.get("epsEstimate"), 4),
            "eps_actual": _round(item.get("epsActual"), 4),
            "revenue_estimate": _round(item.get("revenueEstimate"), 2),
            "revenue_actual": _round(item.get("revenueActual"), 2),
            "quarter": item.get("quarter"),
            "year": item.get("year"),
        }
        row = {k: v for k, v in row.items() if v not in (None, "")}
        if row.get("date"):
            out.append(row)
    return out


def _safe_fetch(name, path, params, token, timeout):
    try:
        return _request_json(path, params, token=token, timeout=timeout), None
    except FinnhubError as e:
        return None, {"endpoint": name, "status": e.status, "reason": str(e)[:200]}


def fetch_symbol_context(symbol, today=None, news_days=30, earnings_days=14,
                         max_news=10, timeout=15, token=None):
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"status": "unavailable", "reason": "缺少 symbol"}
    today_d = today or datetime.date.today()
    news_from = (today_d - datetime.timedelta(days=news_days)).isoformat()
    news_to = today_d.isoformat()
    earnings_to = (today_d + datetime.timedelta(days=earnings_days)).isoformat()

    failures = []
    quote_raw, err = _safe_fetch("quote", "/quote", {"symbol": sym}, token, timeout)
    if err:
        failures.append(err)
    profile_raw, err = _safe_fetch("profile", "/stock/profile2", {"symbol": sym}, token, timeout)
    if err:
        failures.append(err)
    news_raw, err = _safe_fetch(
        "news", "/company-news",
        {"symbol": sym, "from": news_from, "to": news_to},
        token, timeout,
    )
    if err:
        failures.append(err)
    earnings_raw, err = _safe_fetch(
        "earnings", "/calendar/earnings",
        {"symbol": sym, "from": today_d.isoformat(), "to": earnings_to},
        token, timeout,
    )
    if err:
        failures.append(err)

    quote = _normalize_quote(quote_raw)
    profile = _normalize_profile(profile_raw)
    news = _normalize_news(news_raw, max_items=max_news)
    earnings = _normalize_earnings(earnings_raw)
    data_flags = [
        f"Finnhub {e['endpoint']} {e['status']}: {e['reason']}" for e in failures
    ]
    if quote_raw is not None and not quote:
        data_flags.append("Finnhub quote 返回空数据")
    if profile_raw is not None and not profile:
        data_flags.append("Finnhub profile 返回空数据")

    successes = sum(v is not None for v in (quote_raw, profile_raw, news_raw, earnings_raw))
    if successes:
        status = "ok"
    elif failures:
        status = failures[0]["status"]
    else:
        status = "unavailable"
    return {
        "status": status,
        "symbol": sym,
        "as_of": _utc_now(),
        "quote": quote,
        "profile": profile,
        "news": news,
        "earnings": earnings,
        "data_flags": data_flags,
    }


def fetch_finnhub_context(symbols, today=None, news_days=30, earnings_days=14,
                          max_news=10, timeout=15, token=None, include_benchmarks=True):
    tok = _token(token)
    if not tok:
        return {"status": "unavailable", "reason": f"{TOKEN_ENV} 未配置"}
    clean = []
    for sym in symbols or []:
        s = str(sym or "").strip().upper()
        if s and (include_benchmarks or s not in BENCHMARK_SYMBOLS) and s not in clean:
            clean.append(s)
    ctx = {
        "status": "ok",
        "provider": "finnhub",
        "as_of": _utc_now(),
        "symbols": {},
    }
    for sym in clean:
        ctx["symbols"][sym] = fetch_symbol_context(
            sym, today=today, news_days=news_days, earnings_days=earnings_days,
            max_news=max_news, timeout=timeout, token=tok,
        )
    if not ctx["symbols"]:
        ctx["status"] = "unavailable"
        ctx["reason"] = "没有可查询的 symbol"
    elif all(v.get("status") != "ok" for v in ctx["symbols"].values()):
        ctx["status"] = next(iter(ctx["symbols"].values())).get("status") or "unavailable"
    return ctx


def normalize_finnhub_context(raw):
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "Finnhub context JSON 必须是 object"}
    if raw.get("status") in ("unavailable", "unauthorized", "rate_limited"):
        return {
            "status": raw.get("status"),
            "reason": str(raw.get("reason") or "Finnhub context 不可用")[:300],
        }
    symbols_raw = raw.get("symbols") if isinstance(raw.get("symbols"), dict) else {}
    symbols = {}
    for sym, ctx in symbols_raw.items():
        if isinstance(ctx, dict):
            normalized = dict(ctx)
            normalized.setdefault("status", "ok")
            normalized["symbol"] = str(normalized.get("symbol") or sym).strip().upper()
            normalized.setdefault("data_flags", [])
            symbols[normalized["symbol"]] = normalized
    return {
        "status": raw.get("status") or ("ok" if symbols else "unavailable"),
        "provider": raw.get("provider") or "finnhub",
        "as_of": raw.get("as_of") or _utc_now(),
        "symbols": symbols,
        **({"reason": raw.get("reason")} if raw.get("reason") else {}),
    }


def context_for_symbol(finnhub_context, symbol):
    if not isinstance(finnhub_context, dict):
        return None
    if finnhub_context.get("status") not in ("ok",):
        return {
            "status": finnhub_context.get("status") or "unavailable",
            "reason": finnhub_context.get("reason") or "Finnhub context 不可用",
        }
    sym = str(symbol or "").strip().upper()
    return (finnhub_context.get("symbols") or {}).get(sym)


def summarize_finnhub_context(finnhub_context):
    if not isinstance(finnhub_context, dict):
        return None
    if finnhub_context.get("status") != "ok":
        return {
            "status": finnhub_context.get("status") or "unavailable",
            "reason": finnhub_context.get("reason") or "Finnhub context 不可用",
        }
    symbols = finnhub_context.get("symbols") or {}
    flags = []
    for sym, ctx in symbols.items():
        for flag in (ctx or {}).get("data_flags") or []:
            flags.append(f"{sym}: {flag}")
    return {
        "status": "ok",
        "provider": "finnhub",
        "as_of": finnhub_context.get("as_of"),
        "symbols_count": len(symbols),
        "data_flags": flags[:20],
    }


def _match_news_rule(news_item):
    text = " ".join(
        str(news_item.get(k) or "") for k in ("headline", "summary", "source", "category")
    ).lower()
    if not text.strip():
        return None
    for kind, severity, keywords in NEWS_RED_FLAG_RULES:
        if any(kw in text for kw in keywords):
            return kind, severity
    return None


def _source_from_news(news_item, idx):
    return {
        "id": f"finnhub_news_{idx}",
        "title": news_item.get("headline") or "Finnhub company news",
        "published_at": news_item.get("published_at"),
        "url": news_item.get("url"),
        "provider": "Finnhub",
        "source": news_item.get("source"),
    }


def _days_until(date_text, today):
    if not date_text:
        return None
    try:
        d = datetime.date.fromisoformat(str(date_text)[:10])
    except ValueError:
        return None
    return (d - today).days


def llm_context_from_finnhub(finnhub_context, today=None, max_news_flags=5):
    """Convert Finnhub supplemental context into the existing one-way llm_context.

    The mapping is intentionally conservative: explicit negative event keywords can
    produce medium/high red flags; earnings-calendar events are low severity and do
    not cap the score. Positive news is only ignored here, never used to upgrade.
    """
    if not isinstance(finnhub_context, dict) or finnhub_context.get("status") != "ok":
        return {}
    today_d = today or datetime.date.today()
    out = {}
    for sym, ctx in (finnhub_context.get("symbols") or {}).items():
        if not isinstance(ctx, dict) or ctx.get("status") != "ok":
            continue
        sources, red_flags, catalyst_parts = [], [], []
        source_ids = set()

        for idx, item in enumerate(ctx.get("news") or [], start=1):
            if not isinstance(item, dict):
                continue
            matched = _match_news_rule(item)
            if not matched:
                continue
            if len(red_flags) >= max_news_flags:
                break
            kind, severity = matched
            source = _source_from_news(item, idx)
            source_id = source["id"]
            sources.append(source)
            source_ids.add(source_id)
            title = item.get("headline") or kind
            red_flags.append({
                "type": kind,
                "severity": severity,
                "note": f"Finnhub 新闻疑似事件风险：{title}",
                "source_id": source_id,
            })

        for idx, item in enumerate(ctx.get("earnings") or [], start=1):
            if not isinstance(item, dict):
                continue
            days = _days_until(item.get("date"), today_d)
            if days is None or days < 0 or days > EARNINGS_EVENT_WINDOW_DAYS:
                continue
            source_id = f"finnhub_earnings_{idx}"
            if source_id not in source_ids:
                sources.append({
                    "id": source_id,
                    "title": f"Finnhub earnings calendar: {item.get('date')}",
                    "published_at": item.get("date"),
                    "provider": "Finnhub",
                })
                source_ids.add(source_id)
            note = f"{item.get('date')} 财报事件临近"
            if item.get("hour"):
                note += f"（{item.get('hour')}）"
            catalyst_parts.append(note)
            red_flags.append({
                "type": "earnings_event",
                "severity": "low",
                "note": note,
                "source_id": source_id,
            })

        if sources or red_flags or catalyst_parts:
            out[str(sym).strip().upper()] = {
                "as_of": ctx.get("as_of") or finnhub_context.get("as_of"),
                "sources": sources,
                "red_flags": red_flags,
                "data_trust": "ok",
                "catalyst": "；".join(catalyst_parts) if catalyst_parts else None,
            }
    return out


def merge_llm_contexts(primary, supplemental):
    """Merge user-provided llm_context with Finnhub-derived context.

    Primary values win for scalar fields. sources/red_flags are appended so a
    manually supplied context can coexist with automated Finnhub candidates.
    """
    primary = primary if isinstance(primary, dict) else {}
    supplemental = supplemental if isinstance(supplemental, dict) else {}
    merged = {}
    for sym in sorted(set(primary) | set(supplemental)):
        base = dict(primary.get(sym) or {})
        add = supplemental.get(sym) or {}
        sources = []
        if isinstance(base.get("sources"), list):
            sources.extend(base["sources"])
        if isinstance(add.get("sources"), list):
            sources.extend(add["sources"])
        red_flags = []
        if isinstance(base.get("red_flags"), list):
            red_flags.extend(base["red_flags"])
        if isinstance(add.get("red_flags"), list):
            red_flags.extend(add["red_flags"])
        catalyst = base.get("catalyst") or add.get("catalyst")
        data_trust = base.get("data_trust") or add.get("data_trust") or "ok"
        row = {**add, **base}
        row["sources"] = sources
        row["red_flags"] = red_flags
        row["data_trust"] = data_trust
        row["catalyst"] = catalyst
        merged[str(sym).strip().upper()] = row
    return merged or None
