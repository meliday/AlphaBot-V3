import unittest

from alpha_bot.strategy.indicators import detect_vcp, latest_bollinger, latest_rsi, latest_sma, sma
from tests.factories import demo_candles


class IndicatorTests(unittest.TestCase):
    def test_sma_latest(self):
        values = [1, 2, 3, 4, 5]
        self.assertEqual(sma(values, 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(latest_sma(values, 5), 3.0)

    def test_rsi_and_bollinger(self):
        closes = [candle.close for candle in demo_candles()]
        self.assertGreaterEqual(latest_rsi(closes), 0)
        self.assertLessEqual(latest_rsi(closes), 100)
        mid, upper, lower, width = latest_bollinger(closes)
        self.assertGreater(upper, mid)
        self.assertGreater(mid, lower)
        self.assertGreater(width, 0)

    def test_detect_vcp_returns_named_pattern(self):
        pattern, score, details = detect_vcp(demo_candles())
        self.assertIsInstance(pattern, str)
        self.assertGreaterEqual(score, 0)
        self.assertIn("range contraction", details)


if __name__ == "__main__":
    unittest.main()
