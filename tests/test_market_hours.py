from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from alpha_bot.market_hours import market_status


class MarketHoursSafetyTests(unittest.TestCase):
    def test_kr_substitute_holiday_is_closed(self):
        status = market_status(
            "KR", now=datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        )
        self.assertFalse(status.is_open)
        self.assertIn("휴장", status.reason)

    def test_us_early_close_is_not_treated_as_regular_session(self):
        status = market_status(
            "US",
            now=datetime(2026, 11, 27, 14, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        self.assertFalse(status.is_open)

    def test_unverified_calendar_year_fails_closed(self):
        status = market_status(
            "KR", now=datetime(2027, 1, 4, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        )
        self.assertFalse(status.is_open)
        self.assertIn("미검증", status.reason)


if __name__ == "__main__":
    unittest.main()
