import unittest

from alpha_bot.risk import build_trade_plan, calculate_rr
from alpha_bot.strategy.indicators import latest_sma
from tests.factories import demo_candles


class RiskTests(unittest.TestCase):
    def test_rr_calculation(self):
        self.assertEqual(calculate_rr(100, 90, 120), 2)
        self.assertEqual(calculate_rr(100, 100, 120), 0)

    def test_build_trade_plan_has_concrete_levels(self):
        candles = demo_candles()
        sma50 = latest_sma([candle.close for candle in candles], 50)
        plan = build_trade_plan(candles, sma50)
        self.assertGreater(plan.entry_high, plan.entry_low)
        self.assertLess(plan.stop_loss, plan.entry_reference)
        self.assertGreater(plan.target1, plan.entry_reference)
        self.assertGreater(plan.rr_ratio, 0)


if __name__ == "__main__":
    unittest.main()
