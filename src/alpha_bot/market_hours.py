"""Market hours and holiday gate.

Skips auto-trading work when the relevant exchange is closed (weekends,
public holidays, before-open / after-close). Holiday lists are baked in
for the current and following year; extend ``_HOLIDAYS`` in-place as
each new calendar drops, or override via ``config.yaml``.

Time windows (local exchange time, holiday-adjusted):
  KR (KOSPI/KOSDAQ):  09:00 – 15:30 KST, Mon–Fri
  US (NYSE/NASDAQ):   09:30 – 16:00 ET, Mon–Fri
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from alpha_bot.models import Market

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class MarketStatus:
    market: Market
    is_open: bool
    reason: str
    next_open: datetime | None = None


# Holidays are exchange-local calendar dates. Half-days (Black Friday,
# day after Thanksgiving on NYSE, KRX year-end) are treated as full
# closed days here for simplicity — the auto-pilot doesn't need those
# minutes.
_HOLIDAYS: dict[Market, set[date]] = {
    "KR": {
        # 2026
        date(2026, 1, 1),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 설날 연휴
        date(2026, 3, 1),  # 3·1절
        date(2026, 5, 5),  # 어린이날
        date(2026, 5, 25), # 부처님오신날
        date(2026, 6, 6),  # 현충일
        date(2026, 8, 15), # 광복절
        date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),  # 추석
        date(2026, 10, 3), # 개천절
        date(2026, 10, 9), # 한글날
        date(2026, 12, 25),
        date(2026, 12, 31), # 연말 휴장
        # 2027 — fill in as official calendar publishes; safe-but-bare for now
        date(2027, 1, 1),
    },
    "US": {
        # NYSE 2026
        date(2026, 1, 1),  # New Year's
        date(2026, 1, 19), # MLK Day
        date(2026, 2, 16), # Presidents' Day
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25), # Memorial Day
        date(2026, 6, 19), # Juneteenth
        date(2026, 7, 3),  # Independence Day (observed)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),# Thanksgiving
        date(2026, 12, 25),# Christmas
        # 2027
        date(2027, 1, 1),
    },
}


def _market_zone(market: Market) -> ZoneInfo:
    return _KST if market == "KR" else _ET


def _open_window(market: Market) -> tuple[time, time]:
    if market == "KR":
        return time(9, 0), time(15, 30)
    return time(9, 30), time(16, 0)


def market_status(
    market: Market,
    *,
    now: datetime | None = None,
    extra_holidays: Iterable[date] = (),
) -> MarketStatus:
    """Return whether ``market`` is currently open for trading."""

    tz = _market_zone(market)
    moment = (now or datetime.now(tz)).astimezone(tz)
    today_local = moment.date()
    holidays = _HOLIDAYS.get(market, set()) | set(extra_holidays)
    open_t, close_t = _open_window(market)

    if moment.weekday() >= 5:
        return MarketStatus(
            market, False, "주말 휴장",
            next_open=_next_session_open(market, today_local, holidays),
        )
    if today_local in holidays:
        return MarketStatus(
            market, False, "공휴일 휴장",
            next_open=_next_session_open(market, today_local, holidays),
        )
    if moment.time() < open_t:
        next_open = moment.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        return MarketStatus(market, False, f"개장 전 (현지 {moment.strftime('%H:%M')})", next_open=next_open)
    if moment.time() >= close_t:
        return MarketStatus(
            market, False, f"장 마감 (현지 {moment.strftime('%H:%M')})",
            next_open=_next_session_open(market, today_local, holidays),
        )
    return MarketStatus(market, True, f"장중 (현지 {moment.strftime('%H:%M')})")


def _next_session_open(
    market: Market, after: date, holidays: set[date]
) -> datetime:
    tz = _market_zone(market)
    open_t, _ = _open_window(market)
    candidate = after + timedelta(days=1)
    for _ in range(14):  # search up to 2 weeks ahead (handles long holiday clusters)
        if candidate.weekday() < 5 and candidate not in holidays:
            return datetime.combine(candidate, open_t, tzinfo=tz)
        candidate += timedelta(days=1)
    return datetime.combine(candidate, open_t, tzinfo=tz)


def any_market_open(markets: Iterable[Market], *, now: datetime | None = None) -> bool:
    return any(market_status(m, now=now).is_open for m in markets)
