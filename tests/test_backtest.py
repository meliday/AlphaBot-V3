import unittest

from alpha_bot.backtest import Backtester
from alpha_bot.data import SyntheticDataProvider


class BacktestTests(unittest.TestCase):
    def test_backtest_is_reproducible(self):
        provider = SyntheticDataProvider()
        candles = provider.get_candles("NVDA", "US", 320)
        result1 = Backtester().run(
            "NVDA",
            "US",
            candles,
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        result2 = Backtester().run(
            "NVDA",
            "US",
            candles,
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        self.assertEqual(result1.trades, result2.trades)


if __name__ == "__main__":
    unittest.main()
