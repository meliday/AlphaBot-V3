import unittest

from alpha_bot.data import SyntheticDataProvider
from alpha_bot.models import FundamentalsQuarter
from alpha_bot.strategy import StrategyAnalyzer
from tests.factories import demo_candles, downtrend_candles


class StrategyRuleTests(unittest.TestCase):
    def test_below_200_day_sma_forces_hold_off(self):
        provider = SyntheticDataProvider()
        report = StrategyAnalyzer().analyze(
            "NVDA",
            "US",
            downtrend_candles(),
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        self.assertEqual(report.signal, "Hold Off")

    def test_score_below_threshold_cannot_be_buy(self):
        provider = SyntheticDataProvider()
        report = StrategyAnalyzer(min_score=29).analyze(
            "NVDA",
            "US",
            demo_candles(),
            [FundamentalsQuarter("latest", eps_yoy=1, revenue_yoy=1)],
            [],
            provider.get_market_context("NVDA", "US"),
        )
        if report.scoreboard.total < 29:
            self.assertNotIn(report.signal, {"Buy", "Strong Buy"})

    def test_negative_growth_sets_earnings_caution(self):
        provider = SyntheticDataProvider()
        report = StrategyAnalyzer().analyze(
            "NVDA",
            "US",
            demo_candles(),
            [FundamentalsQuarter("latest", eps_yoy=-5, revenue_yoy=12)],
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        self.assertTrue(report.earnings_caution)


if __name__ == "__main__":
    unittest.main()
