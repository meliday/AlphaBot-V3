"""Exchange session windows from the venue's own calendar.

``market_hours`` ships a hand-maintained holiday table that fails closed on
any year nobody has verified. That is the safe default, but it means the bot
stops trading entirely each January until someone edits the table. When Toss
credentials are configured we can just ask the venue instead: its calendar
covers holidays, partial closures, and early closes without annual upkeep.

Structured like :mod:`market_regime` — a process-wide TTL cache behind a
lock, built lazily from the environment, and **fail-open**: every failure
path returns ``None`` so ``market_hours`` falls back to the baked-in table
rather than blocking trading on a calendar lookup.

The endpoint is account-independent (token only), so it works before
``TOSS_ACCOUNT_SEQ`` is known.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from alpha_bot.models import Market

logger = logging.getLogger(__name__)

# The calendar only shifts on business-day boundaries, and one response
# carries previous/today/next. Six hours matches the market-regime cache and
# keeps a long-running auto-pilot to a handful of calls per day.
_TTL_SECONDS = 6 * 3600

_CACHE: dict[Market, tuple[float, list["SessionWindow"] | None]] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class SessionWindow:
    """One regular-trading-hours span, timezone-aware."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _regular_windows(payload: dict[str, Any], market: Market) -> list[SessionWindow]:
    """Pull every regular session out of a previous/today/next payload.

    All three days are collected on purpose: a US regular session runs
    22:30–05:00 KST, so "now" during the small hours belongs to the
    *previous* business day's window. Checking membership across the whole
    set handles the midnight crossing without any date arithmetic.

    Only regular hours count. Pre/after/day-market sessions are thinner and,
    for KR, Toss triggers conditional stop orders during the KRX regular
    session only — trading outside it would mean holding positions the
    broker-side stop cannot protect.
    """

    windows: list[SessionWindow] = []
    for key in ("previousBusinessDay", "today", "nextBusinessDay"):
        day = payload.get(key)
        if not isinstance(day, dict):
            continue
        if market == "KR":
            container = day.get("integrated")
            session = container.get("regularMarket") if isinstance(container, dict) else None
        else:
            session = day.get("regularMarket")
        if not isinstance(session, dict):
            continue  # null session = that day is closed
        start = _parse(session.get("startTime"))
        end = _parse(session.get("endTime"))
        if start and end and end > start:
            windows.append(SessionWindow(start, end))
    return sorted(windows, key=lambda w: w.start)


def _fetch(market: Market) -> list[SessionWindow] | None:
    """One calendar call, or None when Toss is unavailable/unconfigured."""

    try:
        from alpha_bot.broker.toss import TossRestClient, TossSettings

        settings = TossSettings.from_env()  # raises when credentials are absent
        client = TossRestClient(settings)
        raw = client.request(
            "GET",
            f"/api/v1/market-calendar/{market}",
            idempotent=True,
        )
    except Exception as exc:
        logger.info("Market calendar unavailable for %s (%s) — using local table", market, exc)
        return None

    result = raw.get("result")
    if not isinstance(result, dict):
        logger.warning("Market calendar payload for %s was not an object", market)
        return None
    windows = _regular_windows(result, market)
    if not windows:
        # A genuinely empty calendar (every listed day closed) is
        # indistinguishable here from a schema change. Decline to answer and
        # let the local table decide rather than freezing the bot on a guess.
        logger.warning("Market calendar for %s contained no regular sessions", market)
        return None
    return windows


def get_sessions(market: Market, *, now: datetime | None = None) -> list[SessionWindow] | None:
    """Cached regular-session windows around today, or None if unknown."""

    moment = now or datetime.now(timezone.utc)
    with _LOCK:
        cached = _CACHE.get(market)
        if cached and (moment.timestamp() - cached[0]) < _TTL_SECONDS:
            return cached[1]
    windows = _fetch(market)
    with _LOCK:
        _CACHE[market] = (moment.timestamp(), windows)
    return windows


def session_for(market: Market, moment: datetime) -> SessionWindow | None:
    """The regular session containing ``moment``, if the venue calendar knows."""

    windows = get_sessions(market, now=moment)
    if windows is None:
        return None
    return next((w for w in windows if w.contains(moment)), None)


def next_session_start(market: Market, moment: datetime) -> datetime | None:
    windows = get_sessions(market, now=moment)
    if not windows:
        return None
    return next((w.start for w in windows if w.start > moment), None)


def calendar_available(market: Market, *, now: datetime | None = None) -> bool:
    return get_sessions(market, now=now) is not None


def reset_cache() -> None:
    """Drop cached calendars (tests, and after credentials change)."""

    with _LOCK:
        _CACHE.clear()


def cache_ttl_seconds() -> int:
    return _TTL_SECONDS


def _cache_deadline(market: Market) -> datetime | None:
    with _LOCK:
        cached = _CACHE.get(market)
    if not cached:
        return None
    return datetime.fromtimestamp(cached[0], tz=timezone.utc) + timedelta(seconds=_TTL_SECONDS)
