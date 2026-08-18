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


# Holidays are exchange-local calendar dates. Years not explicitly marked
# complete below fail closed instead of guessing that an omitted holiday is
# a trading day.
_HOLIDAYS: dict[Market, set[date]] = {
    "KR": {
        # 2026
        date(2026, 1, 1),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 설날 연휴
        date(2026, 3, 2),  # 3·1절 대체공휴일
        date(2026, 5, 1),  # 근로자의 날 (KRX 휴장)
        date(2026, 5, 5),  # 어린이날
        date(2026, 5, 25), # 부처님오신날
        date(2026, 6, 3),  # 전국동시지방선거
        date(2026, 8, 17), # 광복절 대체공휴일
        date(2026, 9, 24), date(2026, 9, 25),  # 추석
        date(2026, 10, 5), # 개천절 대체공휴일
        date(2026, 10, 9), # 한글날
        date(2026, 12, 25),
        date(2026, 12, 31), # 연말 휴장
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
        # NYSE 2027 (official published calendar)
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    },
}

_COMPLETE_HOLIDAY_YEARS: dict[Market, set[int]] = {
    "KR": {2026},
    "US": {2026, 2027},
}

_EARLY_CLOSES: dict[Market, dict[date, time]] = {
    "KR": {},
    "US": {
        date(2026, 11, 27): time(13, 0),
        date(2026, 12, 24): time(13, 0),
        date(2027, 11, 26): time(13, 0),
    },
}


def _market_zone(market: Market) -> ZoneInfo:
    return _KST if market == "KR" else _ET


def _open_window(market: Market, session_date: date | None = None) -> tuple[time, time]:
    if market == "KR":
        return time(9, 0), time(15, 30)
    close = _EARLY_CLOSES.get(market, {}).get(session_date, time(16, 0))
    return time(9, 30), close


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
    if today_local.year not in _COMPLETE_HOLIDAY_YEARS.get(market, set()):
        return MarketStatus(
            market,
            False,
            f"{today_local.year}년 휴장일 달력 미검증 — 안전 차단",
        )
    holidays = _HOLIDAYS.get(market, set()) | set(extra_holidays)
    open_t, close_t = _open_window(market, today_local)

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
    early = " · 조기폐장일" if close_t < time(16, 0) and market == "US" else ""
    return MarketStatus(
        market, True, f"장중 (현지 {moment.strftime('%H:%M')}){early}"
    )


def _next_session_open(
    market: Market, after: date, holidays: set[date]
) -> datetime | None:
    tz = _market_zone(market)
    candidate = after + timedelta(days=1)
    for _ in range(14):  # search up to 2 weeks ahead (handles long holiday clusters)
        if candidate.year not in _COMPLETE_HOLIDAY_YEARS.get(market, set()):
            return None
        if candidate.weekday() < 5 and candidate not in holidays:
            open_t, _ = _open_window(market, candidate)
            return datetime.combine(candidate, open_t, tzinfo=tz)
        candidate += timedelta(days=1)
    return None


def any_market_open(markets: Iterable[Market], *, now: datetime | None = None) -> bool:
    return any(market_status(m, now=now).is_open for m in markets)
