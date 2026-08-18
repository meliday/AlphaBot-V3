from __future__ import annotations

import unittest

from alpha_bot.errors import BrokerOrderRejected
from alpha_bot.pricing import floor_to_tick, round_order_price, tick_size


class PriceTickTests(unittest.TestCase):
    def test_kr_common_stock_boundaries(self):
        self.assertEqual(tick_size(1_999, "KR"), 1)
        self.assertEqual(tick_size(2_000, "KR"), 5)
        self.assertEqual(tick_size(50_000, "KR"), 100)
        self.assertEqual(tick_size(200_000, "KR"), 500)
        self.assertEqual(tick_size(500_000, "KR"), 1_000)
        self.assertEqual(round_order_price(50_123, "KR", "buy"), 50_100)
        self.assertEqual(round_order_price(50_123, "KR", "sell"), 50_200)

    def test_kr_etf_uses_its_distinct_tick_table(self):
        self.assertEqual(tick_size(1_999.7, "KR", security_type="ETF"), 1)
        self.assertEqual(tick_size(2_001, "KR", security_type="ETF"), 5)
        self.assertEqual(
            round_order_price(1_999.7, "KR", "buy", security_type="ETF"), 1_999
        )
        self.assertEqual(
            round_order_price(2_001, "KR", "sell", security_type="ETF"), 2_005
        )

    def test_us_decimal_precision_and_direction(self):
        self.assertEqual(round_order_price(185.507, "US", "buy"), 185.50)
        self.assertEqual(round_order_price(185.507, "US", "sell"), 185.51)
        self.assertEqual(round_order_price(0.123456, "US", "buy"), 0.1234)
        self.assertEqual(round_order_price(0.123456, "US", "sell"), 0.1235)
        self.assertEqual(floor_to_tick(0.123456, "US"), 0.1234)

    def test_non_positive_or_non_finite_price_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(BrokerOrderRejected):
                round_order_price(value, "US", "buy")


if __name__ == "__main__":
    unittest.main()
