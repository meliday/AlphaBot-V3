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


class AuditLoggingScopeTests(unittest.TestCase):
    """Only live decisions belong in the audit log.

    A backtest replays analyze() once per bar; one A/B study session wrote
    78k query rows (20.9MB) and made the dashboard's "today" counters
    meaningless. use_live_market_data already distinguishes the two.
    """

    def _analyze(self, *, live: bool):
        from unittest.mock import patch

        from alpha_bot.models import MarketContext
        from tests.factories import demo_candles

        candles = demo_candles("NVDA")
        calls = []
        with patch("alpha_bot.audit_log.log_query", lambda **kw: calls.append(kw)), \
             patch("alpha_bot.market_regime.get_regime",
                   side_effect=RuntimeError("no network in tests")):
            StrategyAnalyzer().analyze(
                "NVDA", "US", candles, [], [],
                MarketContext(), use_live_market_data=live,
            )
        return calls

    def test_a_live_analysis_is_audited(self):
        self.assertEqual(len(self._analyze(live=True)), 1)

    def test_a_replayed_analysis_is_not(self):
        self.assertEqual(self._analyze(live=False), [])
