#!/usr/bin/env python3
"""Session-aware read-through policy for completed Alpaca daily bars.

``market_data_store.py`` owns SQLite mechanics.  This module decides which
completed sessions are safe to score, which date ranges are missing, and when a
newly completed session needs a five-session overlap check for split-adjustment
or source corrections.  Remote transports are injected by ``fetching.py`` so
the dependency direction stays acyclic and tests remain fully offline.
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from market_data_store import MarketDataCacheError, MarketDataStore


CACHE_MODES = ("auto", "refresh", "off")
CALENDAR_SYMBOL = "__MARKET_CALENDAR_XNYS__"
CALENDAR_FEED = "calendar"
CALENDAR_ADJUSTMENT = "sessions"
DEFAULT_FINALIZATION_GRACE_MINUTES = 15
DEFAULT_OVERLAP_SESSIONS = 5
MARKET_TZ = ZoneInfo("America/New_York")


class CachePolicyError(RuntimeError):
    """A calendar or policy failure that should trigger a remote fallback."""


@dataclass(frozen=True)
class SessionState:
    phase: str
    now_et: dt.datetime
    today_session: str | None
    open_at: dt.datetime | None
    close_at: dt.datetime | None
    latest_completed_session: str | None


RemoteBarsFetcher = Callable[
    [list[str], str, str, str, str, int, int],
    tuple[dict[str, list[dict[str, Any]]], str, Optional[str]],
]
CalendarFetcher = Callable[[str, str, int], Sequence[Mapping[str, Any]]]
RemoteSnapshotsFetcher = Callable[[list[str], str, int], dict[str, Any]]


_CALENDAR_LOCK = threading.Lock()


def _clean_symbols(symbols: Iterable[Any]) -> list[str]:
    clean: list[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in clean:
            clean.append(symbol)
    return clean


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _parse_timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _session_datetime(session_date: str, value: str) -> dt.datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"session {session_date} has an empty time")
    if "T" in text:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MARKET_TZ)
        return parsed.astimezone(MARKET_TZ)
    parsed_time = dt.time.fromisoformat(text)
    if parsed_time.tzinfo is not None:
        return dt.datetime.combine(_parse_date(session_date), parsed_time).astimezone(MARKET_TZ)
    return dt.datetime.combine(
        _parse_date(session_date), parsed_time, tzinfo=MARKET_TZ
    )


def classify_session(
    now: dt.datetime,
    sessions: Sequence[Mapping[str, Any]],
    *,
    grace_minutes: int = DEFAULT_FINALIZATION_GRACE_MINUTES,
) -> SessionState:
    """Classify regular-session state using cached calendar rows."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now_et = now.astimezone(MARKET_TZ)
    today = now_et.date().isoformat()
    ordered = sorted(
        (dict(row) for row in sessions), key=lambda row: str(row.get("session_date") or row.get("date"))
    )
    today_row = next(
        (row for row in ordered if str(row.get("session_date") or row.get("date")) == today),
        None,
    )
    prior = [
        str(row.get("session_date") or row.get("date"))
        for row in ordered
        if str(row.get("session_date") or row.get("date")) < today
    ]
    latest_prior = prior[-1] if prior else None
    if today_row is None:
        return SessionState("non_trading", now_et, None, None, None, latest_prior)

    open_at = _session_datetime(today, str(today_row.get("open_at") or today_row.get("open")))
    close_at = _session_datetime(today, str(today_row.get("close_at") or today_row.get("close")))
    finalized_at = close_at + dt.timedelta(minutes=int(grace_minutes))
    if now_et < open_at:
        phase, latest = "pre_market", latest_prior
    elif now_et < close_at:
        phase, latest = "open", latest_prior
    elif now_et < finalized_at:
        phase, latest = "post_close_grace", latest_prior
    else:
        phase, latest = "closed", today
    return SessionState(phase, now_et, today, open_at, close_at, latest)


def _bar_date(bar: Mapping[str, Any]) -> str:
    return str(bar.get("t") or "")[:10]


def _completed_only(
    bars: Mapping[str, Sequence[Mapping[str, Any]]], end_date: str
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for raw_symbol, symbol_bars in (bars or {}).items():
        symbol = str(raw_symbol or "").strip().upper()
        kept = [dict(bar) for bar in symbol_bars or [] if _bar_date(bar) <= end_date]
        if kept:
            by_time = {str(bar.get("t")): bar for bar in kept}
            out[symbol] = [by_time[key] for key in sorted(by_time)]
    return out


def _merge_bar_maps(
    base: Mapping[str, Sequence[Mapping[str, Any]]],
    incoming: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    symbols = list(dict.fromkeys([*(base or {}).keys(), *(incoming or {}).keys()]))
    out: dict[str, list[dict[str, Any]]] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).upper()
        merged: dict[str, dict[str, Any]] = {}
        for bar in (base or {}).get(raw_symbol, []) or []:
            merged[str(bar.get("t"))] = dict(bar)
        for bar in (incoming or {}).get(raw_symbol, []) or []:
            merged[str(bar.get("t"))] = dict(bar)
        if merged:
            out[symbol] = [merged[key] for key in sorted(merged)]
    return out


def _replace_symbol_range(
    base: Mapping[str, Sequence[Mapping[str, Any]]],
    symbol: str,
    incoming: Sequence[Mapping[str, Any]],
    start_date: str,
    end_date: str,
) -> dict[str, list[dict[str, Any]]]:
    out = {key: [dict(bar) for bar in bars] for key, bars in (base or {}).items()}
    outside = [
        dict(bar) for bar in out.get(symbol, [])
        if not (start_date <= _bar_date(bar) <= end_date)
    ]
    by_time = {str(bar.get("t")): dict(bar) for bar in [*outside, *incoming]}
    out[symbol] = [by_time[key] for key in sorted(by_time)]
    return out


def _bar_signature(bar: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(bar.get(key) for key in ("o", "h", "l", "c", "v", "vw", "n"))


def overlap_changed(
    cached: Sequence[Mapping[str, Any]], remote: Sequence[Mapping[str, Any]]
) -> bool:
    """Return True when overlapping completed rows were revised by the source."""
    cached_by_date = {_bar_date(bar): bar for bar in cached or []}
    remote_by_date = {_bar_date(bar): bar for bar in remote or []}
    common = set(cached_by_date) & set(remote_by_date)
    if any(_bar_signature(cached_by_date[day]) != _bar_signature(remote_by_date[day]) for day in common):
        return True
    if not remote_by_date:
        return False
    remote_min, remote_max = min(remote_by_date), max(remote_by_date)
    return any(
        remote_min <= day <= remote_max and day not in remote_by_date
        for day in cached_by_date
    )


class MarketDataCache:
    """Read-through coordinator for completed daily bars."""

    def __init__(
        self,
        store: MarketDataStore,
        *,
        remote_bars_fetcher: RemoteBarsFetcher,
        calendar_fetcher: CalendarFetcher,
        remote_snapshots_fetcher: Optional[RemoteSnapshotsFetcher] = None,
        finalization_grace_minutes: int = DEFAULT_FINALIZATION_GRACE_MINUTES,
        overlap_sessions: int = DEFAULT_OVERLAP_SESSIONS,
    ) -> None:
        self.store = store
        self.remote_bars_fetcher = remote_bars_fetcher
        self.calendar_fetcher = calendar_fetcher
        self.remote_snapshots_fetcher = remote_snapshots_fetcher
        self.finalization_grace_minutes = int(finalization_grace_minutes)
        self.overlap_sessions = max(1, int(overlap_sessions))

    def _calendar_window(self) -> tuple[str, str]:
        year = self.store.now_utc().astimezone(MARKET_TZ).year
        return f"{year - 1}-01-01", f"{year + 1}-12-31"

    def ensure_calendar(self, timeout: int) -> list[dict[str, Any]]:
        start_date, end_date = self._calendar_window()
        try:
            # Scanner workers share a database but own separate connections.  A
            # process lock prevents a cold start from fetching the same calendar
            # once per chunk; cross-process races remain harmless/idempotent.
            with _CALENDAR_LOCK:
                existing_sessions = self.store.get_market_sessions(start_date, end_date)
                gaps = self.store.missing_ranges(
                    CALENDAR_SYMBOL,
                    feed=CALENDAR_FEED,
                    adjustment=CALENDAR_ADJUSTMENT,
                    start_date=start_date,
                    end_date=end_date,
                )
                # Self-heal coverage written by an older/broken calendar parser.
                if not gaps and not existing_sessions:
                    gaps = [(start_date, end_date)]
                for gap_start, gap_end in gaps:
                    try:
                        sessions = list(self.calendar_fetcher(gap_start, gap_end, timeout) or [])
                    except Exception as exc:  # noqa: BLE001 - transport boundary
                        raise CachePolicyError(f"Alpaca market calendar unavailable: {exc}") from exc
                    gap_days = (_parse_date(gap_end) - _parse_date(gap_start)).days + 1
                    if not sessions and gap_days >= 7:
                        raise CachePolicyError(
                            f"Alpaca market calendar returned no sessions for {gap_start}..{gap_end}"
                        )
                    self.store.upsert_market_sessions(sessions)
                    self.store.add_sync_range(
                        [CALENDAR_SYMBOL],
                        feed=CALENDAR_FEED,
                        adjustment=CALENDAR_ADJUSTMENT,
                        start_date=gap_start,
                        end_date=gap_end,
                    )
            return self.store.get_market_sessions(start_date, end_date)
        except MarketDataCacheError:
            raise

    def session_state(self, timeout: int) -> tuple[SessionState, list[dict[str, Any]]]:
        sessions = self.ensure_calendar(timeout)
        try:
            state = classify_session(
                self.store.now_utc(), sessions,
                grace_minutes=self.finalization_grace_minutes,
            )
        except (TypeError, ValueError) as exc:
            raise CachePolicyError(f"invalid cached market calendar: {exc}") from exc
        return state, sessions

    def _overlap_start(
        self, sessions: Sequence[Mapping[str, Any]], end_date: str, request_start: str
    ) -> str:
        dates = sorted(
            str(row.get("session_date") or row.get("date"))
            for row in sessions
            if str(row.get("session_date") or row.get("date")) <= end_date
        )
        if not dates:
            return request_start
        candidate = dates[max(0, len(dates) - self.overlap_sessions)]
        return max(candidate, request_start)

    def _cached_extent_start(self, symbol: str, feed: str, adjustment: str) -> str | None:
        try:
            ranges = self.store.get_sync_ranges(symbol, feed=feed, adjustment=adjustment)
        except MarketDataCacheError:
            return None
        return str(ranges[0]["start_date"]) if ranges else None

    def _record_feed_outcome(
        self, requested_feed: str, effective_feed: str, actual_feed: str, mode: str
    ) -> None:
        """Persist feed fallbacks so later runs read the reachable namespace directly."""
        try:
            if actual_feed != effective_feed:
                self.store.set_feed_fallback(requested_feed, actual_feed)
            elif mode == "refresh" and actual_feed == requested_feed:
                self.store.clear_feed_fallback(requested_feed)
        except MarketDataCacheError:
            pass

    def _tail_coverage_end(
        self,
        completed: Mapping[str, Sequence[Mapping[str, Any]]],
        fetch_end: str,
        state: SessionState,
        session_dates: Sequence[str],
        notes: list[str],
    ) -> str | None:
        """Trust coverage of the just-completed session only once its bars exist.

        Right after close+grace Alpaca may not have published the final daily bar
        yet.  Marking the tail session covered then would freeze the miss until
        the next session; instead coverage stops at the prior session so the next
        call re-checks.  Historical fetch ends are always trusted.
        """
        if fetch_end != state.latest_completed_session:
            return fetch_end
        has_tail = any(
            _bar_date(bar) == fetch_end
            for bars in completed.values()
            for bar in bars
        )
        if has_tail:
            return fetch_end
        message = f"{fetch_end} 日线远端尚未发布，暂不标记该日覆盖"
        if message not in notes:
            notes.append(message)
        prior = [day for day in session_dates if day < fetch_end]
        return prior[-1] if prior else None

    def load_bars(
        self,
        symbols: Iterable[str],
        start: str,
        end: str,
        feed: str,
        adjustment: str,
        limit: int,
        timeout: int,
        *,
        mode: str = "auto",
    ) -> tuple[dict[str, list[dict[str, Any]]], str, str | None]:
        if mode not in ("auto", "refresh"):
            raise ValueError("cache policy mode must be auto or refresh")
        clean = _clean_symbols(symbols)
        if not clean:
            return {}, str(feed).lower(), "SQLite 缓存：没有可查询的 symbol"
        requested_start = _parse_date(start).isoformat()
        requested_end = _parse_date(end).isoformat()
        if requested_start > requested_end:
            raise ValueError("start is after end")
        requested_feed = str(feed or "").strip().lower()
        adjustment = str(adjustment or "").strip().lower()

        notes: list[str] = []
        effective_feed = requested_feed
        if mode != "refresh":
            try:
                mapped = self.store.get_feed_fallback(requested_feed)
            except MarketDataCacheError:
                mapped = None
            if mapped and mapped != requested_feed:
                effective_feed = mapped
                notes.append(f"feed {requested_feed} 此前受限，直接使用 {mapped} 缓存")

        state, sessions = self.session_state(timeout)
        if not state.latest_completed_session:
            raise CachePolicyError("market calendar has no completed session before now")
        effective_end = min(requested_end, state.latest_completed_session)
        if effective_end < requested_start:
            return {}, effective_feed, (
                f"SQLite 缓存：{state.phase}，请求范围内没有已完成交易日"
            )
        session_dates = sorted(
            str(row.get("session_date") or row.get("date")) for row in sessions
        )

        cached = self.store.get_daily_bars(
            clean,
            start_date=requested_start,
            end_date=effective_end,
            feed=effective_feed,
            adjustment=adjustment,
        )
        memory = cached
        cache_rows = sum(len(rows) for rows in cached.values())
        overlap_start = self._overlap_start(sessions, effective_end, requested_start)

        grouped: dict[tuple[str, str], list[str]] = {}
        if mode == "refresh":
            grouped[(requested_start, effective_end)] = clean
        else:
            gap_map = self.store.missing_ranges_bulk(
                clean,
                feed=effective_feed,
                adjustment=adjustment,
                start_date=requested_start,
                end_date=effective_end,
            )
            for symbol in clean:
                for gap_start, gap_end in gap_map.get(symbol, []):
                    if gap_end == effective_end:
                        gap_start = min(gap_start, overlap_start)
                    grouped.setdefault((gap_start, gap_end), []).append(symbol)

        used_feed = effective_feed
        remote_rows = 0
        write_failures: list[str] = []
        transport_failures: list[Exception] = []
        feed_outcome_recorded = False
        for (gap_start, gap_end), group_symbols in sorted(grouped.items()):
            group_symbols = list(dict.fromkeys(group_symbols))
            try:
                remote, actual_feed, remote_note = self.remote_bars_fetcher(
                    group_symbols, gap_start, gap_end, effective_feed,
                    adjustment, limit, timeout,
                )
            except Exception as exc:  # noqa: BLE001 - transport boundary
                transport_failures.append(exc)
                notes.append(f"{gap_start}..{gap_end} 远端拉取失败，改用缓存数据：{exc}")
                continue
            actual_feed = str(actual_feed or effective_feed).lower()
            if remote_note:
                notes.append(remote_note)
            if not feed_outcome_recorded:
                feed_outcome_recorded = True
                self._record_feed_outcome(requested_feed, effective_feed, actual_feed, mode)
            if actual_feed != used_feed:
                used_feed = actual_feed
                try:
                    memory = self.store.get_daily_bars(
                        clean,
                        start_date=requested_start,
                        end_date=effective_end,
                        feed=used_feed,
                        adjustment=adjustment,
                    )
                    cache_rows = sum(len(rows) for rows in memory.values())
                except MarketDataCacheError:
                    memory = {}

            completed = _completed_only(remote, effective_end)
            remote_rows += sum(len(rows) for rows in completed.values())
            coverage_end = self._tail_coverage_end(
                completed, gap_end, state, session_dates, notes
            )
            existing_overlap = self.store.get_daily_bars(
                group_symbols,
                start_date=gap_start,
                end_date=gap_end,
                feed=actual_feed,
                adjustment=adjustment,
            )
            # A revision on overlapping completed rows (split/dividend
            # re-adjustment, source correction) invalidates the symbol's whole
            # cached history, not just the requested slice.  In refresh mode
            # the replace below already heals the fetched range, so only
            # history cached before the range still needs the full refetch.
            revised = []
            for symbol in group_symbols:
                if not overlap_changed(
                    existing_overlap.get(symbol, []), completed.get(symbol, [])
                ):
                    continue
                if mode == "refresh":
                    extent = self._cached_extent_start(symbol, actual_feed, adjustment)
                    if extent is None or extent >= gap_start:
                        continue
                revised.append(symbol)
            normal = [symbol for symbol in group_symbols if symbol not in revised]
            normal_bars = {symbol: completed[symbol] for symbol in normal if symbol in completed}
            if normal:
                if coverage_end is not None and coverage_end >= gap_start:
                    try:
                        self.store.commit_daily_sync(
                            normal_bars, normal,
                            feed=actual_feed,
                            adjustment=adjustment,
                            start_date=gap_start,
                            end_date=coverage_end,
                            replace=(mode == "refresh"),
                        )
                    except MarketDataCacheError as exc:
                        write_failures.append(str(exc))
                if mode == "refresh":
                    for symbol in normal:
                        memory = _replace_symbol_range(
                            memory, symbol, normal_bars.get(symbol, []),
                            gap_start, gap_end,
                        )
                else:
                    memory = _merge_bar_maps(memory, normal_bars)

            for symbol in revised:
                extent = self._cached_extent_start(symbol, actual_feed, adjustment)
                refetch_start = min(extent, requested_start) if extent else requested_start
                try:
                    full_remote, full_feed, full_note = self.remote_bars_fetcher(
                        [symbol], refetch_start, effective_end, actual_feed,
                        adjustment, limit, timeout,
                    )
                except Exception as exc:  # noqa: BLE001 - transport boundary
                    transport_failures.append(exc)
                    notes.append(f"{symbol} 重叠修订刷新失败，保留缓存数据：{exc}")
                    continue
                full_feed = str(full_feed or actual_feed).lower()
                if full_note:
                    notes.append(full_note)
                full_completed = _completed_only(full_remote, effective_end)
                remote_rows += sum(len(rows) for rows in full_completed.values())
                full_coverage_end = self._tail_coverage_end(
                    full_completed, effective_end, state, session_dates, notes
                )
                if full_coverage_end is not None and full_coverage_end >= refetch_start:
                    try:
                        self.store.commit_daily_sync(
                            full_completed, [symbol],
                            feed=full_feed,
                            adjustment=adjustment,
                            start_date=refetch_start,
                            end_date=full_coverage_end,
                            replace=True,
                        )
                    except MarketDataCacheError as exc:
                        write_failures.append(str(exc))
                requested_bars = [
                    bar for bar in full_completed.get(symbol, [])
                    if _bar_date(bar) >= requested_start
                ]
                memory = _replace_symbol_range(
                    memory, symbol, requested_bars,
                    requested_start, effective_end,
                )
                used_feed = full_feed
                notes.append(
                    f"{symbol} 重叠日线发生修订，已刷新全部缓存历史（{refetch_start}..{effective_end}）"
                )

        if transport_failures and not memory:
            raise transport_failures[0]

        if grouped:
            # Reconcile with rows other groups/processes persisted meanwhile;
            # a pure cache hit skips this and returns the first read directly.
            try:
                persisted = self.store.get_daily_bars(
                    clean,
                    start_date=requested_start,
                    end_date=effective_end,
                    feed=used_feed,
                    adjustment=adjustment,
                )
                memory = _merge_bar_maps(persisted, memory)
            except MarketDataCacheError as exc:
                write_failures.append(str(exc))

        detail = (
            f"SQLite 缓存：{state.phase}，命中 {cache_rows} 根，远端补充 {remote_rows} 根，"
            f"最近完成交易日 {effective_end}"
        )
        if transport_failures:
            detail += "；远端部分不可用，结果可能缺少最新数据"
        if write_failures:
            detail += "；数据库写入不可用，本轮使用已拉取数据"
        if notes:
            detail += "；" + "；".join(dict.fromkeys(notes))
        return memory, used_feed, detail

    @staticmethod
    def _snapshot_from_final_bar(symbol: str, bar: Mapping[str, Any]) -> dict[str, Any]:
        daily = {
            "date": _bar_date(bar),
            "open": bar.get("o"),
            "high": bar.get("h"),
            "low": bar.get("l"),
            "close": bar.get("c"),
            "volume": bar.get("v"),
            "vwap": bar.get("vw"),
        }
        daily = {key: value for key, value in daily.items() if value is not None}
        out: dict[str, Any] = {"symbol": symbol, "daily_bar": daily}
        try:
            opened, closed = float(bar.get("o")), float(bar.get("c"))
            if opened:
                out["daily_change_pct"] = round((closed / opened - 1) * 100, 2)
        except (TypeError, ValueError):
            pass
        return out

    def _finalized_deadlines(
        self, sessions: Sequence[Mapping[str, Any]]
    ) -> dict[str, dt.datetime]:
        """Map each session date to close time plus the finalization grace."""
        deadlines: dict[str, dt.datetime] = {}
        grace = dt.timedelta(minutes=self.finalization_grace_minutes)
        for row in sessions:
            day = str(row.get("session_date") or row.get("date") or "")
            close_raw = row.get("close_at") or row.get("close")
            if not day or not close_raw:
                continue
            try:
                deadlines[day] = _session_datetime(day, str(close_raw)) + grace
            except (TypeError, ValueError):
                continue
        return deadlines

    @staticmethod
    def _snapshot_is_stale(
        meta: Mapping[str, Any],
        state: SessionState,
        deadlines: Mapping[str, dt.datetime],
    ) -> bool:
        """An intraday capture of a completed session must not pose as final."""
        latest = state.latest_completed_session
        session_date = str((meta or {}).get("session_date") or "")
        if not latest or not session_date:
            return False
        if session_date > latest:
            return False  # today's live session; the open/grace phases own it
        if session_date < latest:
            return True  # older than the newest completed session
        deadline = deadlines.get(session_date)
        captured = _parse_timestamp((meta or {}).get("captured_at"))
        if deadline is None or captured is None:
            return False
        return captured < deadline

    def _cached_or_final_snapshots(
        self,
        symbols: Sequence[str],
        *,
        feed: str,
        adjustment: str,
        state: SessionState,
        sessions: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        """Split cached snapshots into fresh and stale, upgrading via final bars.

        Returns ``(fresh, stale, timestamps)``.  Stale entries are intraday
        captures of sessions that have since finalized; they are only served as
        a last resort when neither the final daily bar nor the remote transport
        can replace them.
        """
        cached = self.store.get_latest_snapshots(symbols, feed=feed)
        fresh: dict[str, Any] = {}
        stale: dict[str, Any] = {}
        stale_sessions: dict[str, str] = {}
        timestamps: list[str] = []
        if cached.get("status") == "ok":
            meta = cached.get("meta") or {}
            deadlines = self._finalized_deadlines(sessions)
            for symbol, payload in (cached.get("symbols") or {}).items():
                row_meta = meta.get(symbol) or {}
                if self._snapshot_is_stale(row_meta, state, deadlines):
                    stale[symbol] = payload
                    stale_sessions[symbol] = str(row_meta.get("session_date") or "")
                else:
                    fresh[symbol] = payload
                    timestamps.append(
                        str(row_meta.get("captured_at") or cached.get("as_of") or "")
                    )
        missing = [symbol for symbol in symbols if symbol not in fresh]
        if missing and state.latest_completed_session:
            final = self.store.get_latest_daily_bars(
                missing,
                feed=feed,
                adjustment=adjustment,
                on_or_before=state.latest_completed_session,
            )
            for symbol in missing:
                bar = final.get(symbol)
                if not bar:
                    continue
                bar_date = _bar_date(bar)
                if symbol in stale_sessions and bar_date < stale_sessions[symbol]:
                    continue  # final bar predates the stale capture; needs remote
                fresh[symbol] = self._snapshot_from_final_bar(symbol, bar)
                stale.pop(symbol, None)
                if bar.get("t"):
                    timestamps.append(str(bar["t"]))
        return fresh, stale, timestamps

    def load_snapshots(
        self,
        symbols: Iterable[str],
        feed: str,
        timeout: int,
        *,
        adjustment: str = "split",
        mode: str = "auto",
    ) -> dict[str, Any]:
        """Load session-aware snapshots while keeping live bars supplemental-only."""
        if mode not in ("auto", "refresh"):
            raise ValueError("cache policy mode must be auto or refresh")
        clean = _clean_symbols(symbols)
        if not clean:
            return {"status": "unavailable", "reason": "没有可查询的 symbol"}
        requested_feed = str(feed or "").strip().lower()
        adjustment = str(adjustment or "").strip().lower()
        state, sessions = self.session_state(timeout)
        should_refresh = mode == "refresh" or state.phase == "open"
        remote_failure: str | None = None

        if should_refresh:
            if self.remote_snapshots_fetcher is None:
                raise CachePolicyError("snapshot transport is not configured")
            context = self.remote_snapshots_fetcher(clean, requested_feed, timeout)
            if isinstance(context, dict) and context.get("status") == "ok":
                actual_feed = str(context.get("feed") or requested_feed).lower()
                try:
                    self.store.upsert_snapshot_context(
                        context,
                        include_live=(state.phase == "open"),
                        live_session_date=state.today_session,
                    )
                except (MarketDataCacheError, OSError, ValueError) as exc:
                    result = dict(context)
                    result["cache_note"] = f"SQLite snapshot 写入不可用，已返回远端数据：{exc}"
                    return result
                result = dict(context)
                result["feed"] = actual_feed
                result["cache_note"] = (
                    f"SQLite snapshot：{state.phase}，远端刷新 "
                    f"{len(context.get('symbols') or {})} 只"
                )
                return result
            remote_failure = str(
                (context or {}).get("reason")
                if isinstance(context, dict) else "snapshot 返回无效数据"
            )

        fresh, stale, timestamps = self._cached_or_final_snapshots(
            clean, feed=requested_feed, adjustment=adjustment,
            state=state, sessions=sessions,
        )
        missing = [symbol for symbol in clean if symbol not in fresh]
        remote_symbols: dict[str, Any] = {}
        actual_feed = requested_feed
        write_note = ""
        # Outside regular hours a complete, final DB is network-free.  A cold,
        # partial, or intraday-stale cache queries only the affected symbols
        # once, then persists the refreshed snapshot without a live row.
        if not should_refresh and missing:
            if self.remote_snapshots_fetcher is None:
                raise CachePolicyError("snapshot transport is not configured")
            context = self.remote_snapshots_fetcher(missing, requested_feed, timeout)
            if isinstance(context, dict) and context.get("status") == "ok":
                remote_symbols = dict(context.get("symbols") or {})
                actual_feed = str(context.get("feed") or requested_feed).lower()
                timestamps.append(str(context.get("as_of") or ""))
                try:
                    self.store.upsert_snapshot_context(context, include_live=False)
                except (MarketDataCacheError, OSError, ValueError) as exc:
                    write_note = f"；数据库写入不可用：{exc}"
            else:
                remote_failure = str(
                    (context or {}).get("reason")
                    if isinstance(context, dict) else "snapshot 返回无效数据"
                )

        payloads: dict[str, Any] = {}
        stale_used = 0
        for symbol in clean:
            if symbol in fresh:
                payloads[symbol] = fresh[symbol]
            elif symbol in remote_symbols:
                payloads[symbol] = remote_symbols[symbol]
            elif symbol in stale:
                payloads[symbol] = stale[symbol]
                stale_used += 1
        if not payloads:
            if remote_failure:
                return {"status": "unavailable", "reason": remote_failure}
            return {
                "status": "unavailable",
                "reason": "SQLite 中没有可用的 latest snapshot 或已完成日线",
            }

        stamps = [stamp for stamp in timestamps if stamp]
        as_of = (
            max(stamps) if stamps
            else self.store.now_utc().isoformat().replace("+00:00", "Z")
        )
        note = f"SQLite snapshot：{state.phase}，读取 {len(fresh)} 只"
        if remote_symbols:
            note += f"，远端补充 {len(remote_symbols)} 只"
        if stale_used:
            note += f"；{stale_used} 只仅有盘中快照，可能不是收盘价"
        if remote_failure:
            note += f"；远端不可用，使用已缓存数据：{remote_failure}"
        result: dict[str, Any] = {
            "status": "ok",
            "as_of": as_of,
            "feed": actual_feed,
            "symbols": payloads,
            "cache_note": note + write_note,
        }
        if remote_failure or stale_used:
            result["stale"] = True
        return result


__all__ = [
    "CACHE_MODES",
    "CachePolicyError",
    "MarketDataCache",
    "SessionState",
    "classify_session",
    "overlap_changed",
]
