"""Regime look-ahead isolation and live relative-strength derivation tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from alpha_bot.backtest import Backtester
from alpha_bot.data import SyntheticDataProvider
from alpha_bot.data.providers import compound_return
from alpha_bot.market_regime import MarketRegime
from alpha_bot.models import MarketContext
from alpha_bot.strategy import StrategyAnalyzer
from tests.factories import demo_candles

_BEAR = MarketRegime(
    market="US", index_symbol="^GSPC", is_bullish=False,
    close=4000.0, sma200=4500.0, reason="테스트 약세", return_3m=-8.0,
)
_BULL = MarketRegime(
    market="US", index_symbol="^GSPC", is_bullish=True,
    close=5000.0, sma200=4500.0, reason="테스트 강세", return_3m=5.0,
)


def _analyze(use_live: bool):
    provider = SyntheticDataProvider()
    return StrategyAnalyzer().analyze(
        "NVDA", "US", demo_candles(),
        provider.get_fundamentals("NVDA", "US"),
        provider.get_catalysts("NVDA", "US"),
        provider.get_market_context("NVDA", "US"),
        use_live_market_data=use_live,
    )


class RegimeLookAheadTests(unittest.TestCase):
    def test_bearish_regime_vetoes_live_signal(self):
        with patch("alpha_bot.market_regime.get_regime", return_value=_BEAR):
            report = _analyze(use_live=True)
        self.assertEqual(report.signal, "Hold Off")
        self.assertIn("레짐", report.reason)

    def test_backtest_mode_ignores_todays_regime(self):
        with patch("alpha_bot.market_regime.get_regime", return_value=_BEAR):
            report = _analyze(use_live=False)
        self.assertNotEqual(report.signal, "Hold Off")

    def test_backtest_results_independent_of_todays_regime(self):
        provider = SyntheticDataProvider()
        candles = provider.get_candles("NVDA", "US", 320)
        args = (
            "NVDA", "US", candles,
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        with patch("alpha_bot.market_regime.get_regime", return_value=_BEAR):
            bear_result = Backtester().run(*args)
        with patch("alpha_bot.market_regime.get_regime", return_value=_BULL):
            bull_result = Backtester().run(*args)
        self.assertEqual(bear_result.trades, bull_result.trades)


class DistributionDayTests(unittest.TestCase):
    """count_distribution_days — the early-warning leg of the regime filter."""

    def _series(self, events: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
        """Build (closes, volumes) from (pct_change, volume) event pairs,
        prefixed with a stable ramp so the lookback window is full."""
        closes, volumes = [100.0], [1_000_000.0]
        for pct, vol in events:
            closes.append(closes[-1] * (1 + pct / 100.0))
            volumes.append(vol)
        return closes, volumes

    def test_counts_down_days_on_rising_volume(self):
        from alpha_bot.market_regime import count_distribution_days
        events = []
        # 12 quiet up-days, then 6 distribution days (−0.5% on rising volume)
        # separated by quiet days.
        for _ in range(12):
            events.append((0.1, 1_000_000.0))
        for k in range(6):
            events.append((-0.5, 1_500_000.0 + k * 100_000))
            events.append((0.05, 900_000.0))
        closes, volumes = self._series(events)
        self.assertEqual(count_distribution_days(closes, volumes), 6)

    def test_down_day_on_falling_volume_is_not_distribution(self):
        from alpha_bot.market_regime import count_distribution_days
        events = [(0.1, 1_000_000.0)] * 10 + [(-1.0, 500_000.0)] * 10
        closes, volumes = self._series(events)
        # Volume halves on the first drop then stays flat — only the first
        # down bar even has a volume comparison, and it fails it.
        self.assertEqual(count_distribution_days(closes, volumes), 0)

    def test_shallow_dips_below_threshold_do_not_count(self):
        from alpha_bot.market_regime import count_distribution_days
        events = [(0.1, 1_000_000.0)] * 10 + [(-0.1, 2_000_000.0), (-0.15, 2_500_000.0)] * 5
        closes, volumes = self._series(events)
        self.assertEqual(count_distribution_days(closes, volumes), 0)

    def test_unusable_volume_fails_open_to_none(self):
        from alpha_bot.market_regime import count_distribution_days
        closes = [100.0 - i for i in range(30)]
        self.assertIsNone(count_distribution_days(closes, [0.0] * 30))
        self.assertIsNone(count_distribution_days(closes, [1.0] * 10))  # length mismatch


class RelativeStrengthTests(unittest.TestCase):
    def test_live_rs_derived_from_regime_benchmark(self):
        analyzer = StrategyAnalyzer()
        candles = demo_candles()
        empty_ctx = MarketContext()  # what live KIS mode actually provides
        with patch("alpha_bot.market_regime.get_regime", return_value=_BULL):
            rel = analyzer._relative_strength(empty_ctx, candles, "US", allow_live=True)
        stock = compound_return(candles, 63)
        self.assertIsNotNone(rel)
        self.assertAlmostEqual(rel, stock - 5.0, places=6)

    def test_backtest_mode_never_uses_live_benchmark(self):
        analyzer = StrategyAnalyzer()
        with patch("alpha_bot.market_regime.get_regime", return_value=_BULL):
            rel = analyzer._relative_strength(
                MarketContext(), demo_candles(), "US", allow_live=False
            )
        self.assertIsNone(rel)

    def test_explicit_context_still_takes_precedence(self):
        analyzer = StrategyAnalyzer()
        ctx = MarketContext(benchmark_return_3m=3.0, stock_return_3m=10.0)
        rel = analyzer._relative_strength(ctx, demo_candles(), "US", allow_live=True)
        self.assertAlmostEqual(rel, 7.0)


if __name__ == "__main__":
    unittest.main()
