"""Venue-calendar session tests.

Payload shapes are taken from the Toss Open API spec so the parser is
pinned to the real contract, not to whatever the implementation happens to
accept. The behavioural tests then check the two things that matter:
sessions are read correctly across the KST midnight boundary, and every
failure path degrades to the local holiday table instead of blocking.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alpha_bot import market_calendar
from alpha_bot.market_calendar import SessionWindow, _regular_windows
from alpha_bot.market_hours import market_status

_KST = ZoneInfo("Asia/Seoul")


def kr_payload(*, today_open: bool = True, pre_market: bool = True) -> dict:
    def day(d: str, is_open: bool) -> dict:
        if not is_open:
            return {"date": d, "integrated": None}
        integrated: dict = {
            "regularMarket": {
                "startTime": f"{d}T09:00:00+09:00",
                "singlePriceAuctionStartTime": f"{d}T15:20:00+09:00",
                "endTime": f"{d}T15:30:00+09:00",
            },
            "afterMarket": {
                "startTime": f"{d}T15:30:00+09:00",
                "endTime": f"{d}T20:00:00+09:00",
            },
        }
        integrated["preMarket"] = (
            {
                "startTime": f"{d}T08:00:00+09:00",
                "endTime": f"{d}T09:00:00+09:00",
            }
            if pre_market
            else None
        )
        return {"date": d, "integrated": integrated}

    return {
        "today": day("2026-03-25", today_open),
        "previousBusinessDay": day("2026-03-24", True),
        "nextBusinessDay": day("2026-03-26", True),
    }


def us_payload() -> dict:
    """US regular hours land at 22:30–05:00 KST — the session crosses midnight."""

    def day(label: str, start: str, end: str) -> dict:
        return {
            "date": label,
            "dayMarket": None,
            "preMarket": None,
            "regularMarket": {"startTime": start, "endTime": end},
            "afterMarket": None,
        }

    return {
        "previousBusinessDay": day(
            "2026-03-24", "2026-03-24T22:30:00+09:00", "2026-03-25T05:00:00+09:00"
        ),
        "today": day(
            "2026-03-25", "2026-03-25T22:30:00+09:00", "2026-03-26T05:00:00+09:00"
        ),
        "nextBusinessDay": day(
            "2026-03-26", "2026-03-26T22:30:00+09:00", "2026-03-27T05:00:00+09:00"
        ),
    }


class ParsingTests(unittest.TestCase):
    def test_kr_business_day_yields_three_regular_sessions(self):
        windows = _regular_windows(kr_payload(), "KR")
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0].start.astimezone(_KST).hour, 9)
        self.assertEqual(windows[0].end.astimezone(_KST).hour, 15)

    def test_only_regular_hours_are_used(self):
        # Pre/after-market spans exist in the payload but must not widen the
        # tradable window: KR conditional stops only fire in the regular session.
        windows = _regular_windows(kr_payload(), "KR")
        for window in windows:
            self.assertEqual(window.start.astimezone(_KST).time().hour, 9)

    def test_a_closed_day_contributes_no_session(self):
        windows = _regular_windows(kr_payload(today_open=False), "KR")
        self.assertEqual(len(windows), 2)
        self.assertNotIn(
            date(2026, 3, 25), [w.start.astimezone(_KST).date() for w in windows]
        )

    def test_a_null_pre_market_does_not_close_the_regular_session(self):
        windows = _regular_windows(kr_payload(pre_market=False), "KR")
        self.assertEqual(len(windows), 3)

    def test_us_sessions_are_read_from_the_top_level(self):
        windows = _regular_windows(us_payload(), "US")
        self.assertEqual(len(windows), 3)

    def test_malformed_sessions_are_dropped_not_guessed(self):
        payload = {"today": {"regularMarket": {"startTime": "nonsense", "endTime": None}}}
        self.assertEqual(_regular_windows(payload, "US"), [])


class MarketStatusTests(unittest.TestCase):
    def setUp(self):
        market_calendar.reset_cache()
        self.addCleanup(market_calendar.reset_cache)

    def _with_calendar(self, windows):
        return patch.object(market_calendar, "_fetch", lambda market: windows)

    def test_open_inside_a_venue_session(self):
        windows = _regular_windows(kr_payload(), "KR")
        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with self._with_calendar(windows):
            status = market_status("KR", now=moment)
        self.assertTrue(status.is_open)
        self.assertIn("거래소 캘린더", status.reason)

    def test_closed_before_the_open_reports_the_next_start(self):
        windows = _regular_windows(kr_payload(), "KR")
        moment = datetime(2026, 3, 25, 8, 30, tzinfo=_KST)
        with self._with_calendar(windows):
            status = market_status("KR", now=moment)
        self.assertFalse(status.is_open)
        self.assertIn("개장 전", status.reason)
        self.assertEqual(status.next_open.astimezone(_KST).hour, 9)

    def test_a_venue_holiday_closes_the_day(self):
        windows = _regular_windows(kr_payload(today_open=False), "KR")
        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with self._with_calendar(windows):
            status = market_status("KR", now=moment)
        self.assertFalse(status.is_open)
        self.assertEqual(status.next_open.astimezone(_KST).date(), date(2026, 3, 26))

    def test_us_session_spanning_kst_midnight_is_open(self):
        windows = _regular_windows(us_payload(), "US")
        # 02:00 KST belongs to the session that began the previous KST evening.
        moment = datetime(2026, 3, 25, 2, 0, tzinfo=_KST)
        with self._with_calendar(windows):
            self.assertTrue(market_status("US", now=moment).is_open)

    def test_manual_holidays_still_override_the_venue(self):
        windows = _regular_windows(kr_payload(), "KR")
        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with self._with_calendar(windows):
            status = market_status("KR", now=moment, extra_holidays=[date(2026, 3, 25)])
        self.assertFalse(status.is_open)
        self.assertIn("수동 지정", status.reason)

    def test_an_unverified_year_becomes_tradable_once_the_venue_answers(self):
        # The whole point of the integration: the local table fails closed on
        # 2029, the venue calendar does not.
        moment = datetime(2029, 3, 26, 11, 0, tzinfo=_KST)
        blocked = market_status("KR", now=moment, use_venue_calendar=False)
        self.assertFalse(blocked.is_open)
        self.assertIn("미검증", blocked.reason)

        windows = [
            SessionWindow(
                datetime(2029, 3, 26, 9, 0, tzinfo=_KST),
                datetime(2029, 3, 26, 15, 30, tzinfo=_KST),
            )
        ]
        with self._with_calendar(windows):
            self.assertTrue(market_status("KR", now=moment).is_open)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        market_calendar.reset_cache()
        self.addCleanup(market_calendar.reset_cache)

    def test_no_credentials_falls_back_to_the_local_table(self):
        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with patch.object(market_calendar, "_fetch", lambda market: None):
            status = market_status("KR", now=moment)
        self.assertTrue(status.is_open)
        self.assertNotIn("거래소 캘린더", status.reason)

    def test_a_transport_failure_is_swallowed_into_unknown(self):
        def boom(self, *args, **kwargs):
            raise RuntimeError("venue unreachable")

        with patch("alpha_bot.broker.toss.TossRestClient.request", boom), patch(
            "alpha_bot.broker.toss.TossSettings.from_env",
            classmethod(lambda cls: cls(client_id="c", client_secret="s")),
        ):
            self.assertIsNone(market_calendar._fetch("KR"))

    def test_a_raising_calendar_never_reaches_the_market_gate(self):
        def boom(market, *, now=None):
            raise RuntimeError("cache exploded")

        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with patch.object(market_calendar, "get_sessions", boom):
            status = market_status("KR", now=moment)  # must not raise
        self.assertTrue(status.is_open)  # fell through to the local table

    def test_an_empty_calendar_is_treated_as_unknown(self):
        # Every listed day closed is indistinguishable from a schema change,
        # so decline to answer instead of freezing the bot on a guess.
        with patch(
            "alpha_bot.broker.toss.TossRestClient.request",
            lambda self, *a, **k: {"result": {"today": {"integrated": None}}},
        ), patch(
            "alpha_bot.broker.toss.TossSettings.from_env",
            classmethod(lambda cls: cls(client_id="c", client_secret="s")),
        ):
            self.assertIsNone(market_calendar._fetch("KR"))

    def test_sessions_are_cached_across_calls(self):
        calls = []

        def counting(market):
            calls.append(market)
            return []

        moment = datetime(2026, 3, 25, 11, 0, tzinfo=_KST)
        with patch.object(market_calendar, "_fetch", counting):
            market_calendar.get_sessions("KR", now=moment)
            market_calendar.get_sessions("KR", now=moment)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
