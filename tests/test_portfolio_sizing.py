"""FX-unified portfolio valuation and sizing tests.

The defect being fixed: ``risk_per_trade_pct`` used to mean "% of whichever
currency sleeve the ticker trades in", so the same setup got a different
budget depending only on where the account's cash happened to sit. These
tests pin the new meaning — % of the whole portfolio — and the two edges
that keep it safe: cash affordability stays per-sleeve (KRW cannot settle a
USD buy), and any valuation failure falls back to the smaller sleeve base
rather than inflating positions.
"""

from __future__ import annotations

import unittest

from alpha_bot.auto.sizing import compute_position_size, sizing_base_value
from alpha_bot.broker.base import supports_portfolio_valuation
from alpha_bot.broker.toss import TossBroker, TossSettings
from alpha_bot.models import AccountBalance


# ── TossBroker.portfolio_value ───────────────────────────────────────


class ValuationClient:
    """Holdings 6.5M KRW + 1,000 USD; cash 500K KRW + 200 USD; mid 1,400."""

    def __init__(self):
        self.calls: list[str] = []
        self.mid_rate = "1400"
        self.buy_rate = "1412.5"

    def request(self, method, path, *, params=None, **kwargs):
        self.calls.append(path)
        if path == "/api/v1/holdings":
            return {
                "result": {
                    "marketValue": {"amount": {"krw": "6500000", "usd": "1000"}},
                    "items": [],
                }
            }
        if path == "/api/v1/buying-power":
            if params["currency"] == "KRW":
                return {"result": {"currency": "KRW", "cashBuyingPower": "500000"}}
            return {"result": {"currency": "USD", "cashBuyingPower": "200"}}
        if path == "/api/v1/exchange-rate":
            return {"result": {"midRate": self.mid_rate, "rate": self.buy_rate}}
        raise AssertionError(f"unexpected call: {path}")


def make_broker(client) -> TossBroker:
    settings = TossSettings(client_id="c", client_secret="s", account_seq=1)
    return TossBroker(settings, client=client)  # type: ignore[arg-type]


class PortfolioValueTests(unittest.TestCase):
    # KRW leg 6.5M + 0.5M = 7,000,000; USD leg (1,000 + 200) × 1,400 = 1,680,000.
    TOTAL_KRW = 8_680_000.0

    def test_both_sleeves_are_summed_in_krw(self):
        broker = make_broker(ValuationClient())
        self.assertAlmostEqual(broker.portfolio_value("KRW"), self.TOTAL_KRW)

    def test_usd_valuation_is_the_same_portfolio_converted(self):
        broker = make_broker(ValuationClient())
        self.assertAlmostEqual(broker.portfolio_value("USD"), self.TOTAL_KRW / 1400)

    def test_mid_rate_is_preferred_over_the_dealing_rate(self):
        # `rate` bakes in the buy spread; a valuation must not.
        client = ValuationClient()
        broker = make_broker(client)
        value = broker.portfolio_value("KRW")
        spread_value = 7_000_000 + 1200 * float(client.buy_rate)
        self.assertAlmostEqual(value, self.TOTAL_KRW)
        self.assertNotAlmostEqual(value, spread_value)

    def test_a_missing_usd_sleeve_counts_as_zero(self):
        class KrOnly(ValuationClient):
            def request(self, method, path, *, params=None, **kwargs):
                if path == "/api/v1/holdings":
                    return {
                        "result": {
                            "marketValue": {"amount": {"krw": "6500000", "usd": None}},
                            "items": [],
                        }
                    }
                return super().request(method, path, params=params, **kwargs)

        broker = make_broker(KrOnly())
        self.assertAlmostEqual(
            broker.portfolio_value("KRW"), 6_500_000 + 500_000 + 200 * 1400
        )

    def test_valuations_are_cached_inside_the_ttl(self):
        client = ValuationClient()
        broker = make_broker(client)
        broker.portfolio_value("KRW")
        first = len(client.calls)
        broker.portfolio_value("KRW")
        self.assertEqual(len(client.calls), first)  # served from cache

    def test_an_unusable_fx_rate_raises_instead_of_guessing(self):
        from alpha_bot.errors import BrokerError

        client = ValuationClient()
        client.mid_rate = "0"
        client.buy_rate = ""
        with self.assertRaises(BrokerError):
            make_broker(client).portfolio_value("KRW")


# ── sizing on top of the capability ──────────────────────────────────


class PortfolioBroker:
    """US sleeve of 10,000 USD inside a 50,000-USD portfolio."""

    name = "stub"

    def __init__(self, *, portfolio: float | None = 50_000.0, cash: float = 10_000.0):
        self._portfolio = portfolio
        self._cash = cash

    def get_cash_balance(self, market):
        return AccountBalance(
            broker=self.name, market=market, currency="USD",
            cash=self._cash, securities_value=0.0, total_value=self._cash,
        )

    def portfolio_value(self, currency: str) -> float:
        if self._portfolio is None:
            raise RuntimeError("FX unavailable")
        return self._portfolio


class SleeveBroker(PortfolioBroker):
    """Same sleeve, no valuation capability — the legacy path."""

    portfolio_value = None  # type: ignore[assignment]


class SizingBaseTests(unittest.TestCase):
    def test_capability_detection(self):
        self.assertTrue(supports_portfolio_valuation(PortfolioBroker()))
        self.assertFalse(supports_portfolio_valuation(SleeveBroker()))

    def test_portfolio_base_when_available(self):
        broker = PortfolioBroker()
        base, label = sizing_base_value(broker, broker.get_cash_balance("US"))
        self.assertEqual(base, 50_000.0)
        self.assertEqual(label, "통합자산")

    def test_sleeve_base_without_the_capability(self):
        broker = SleeveBroker()
        base, label = sizing_base_value(broker, broker.get_cash_balance("US"))
        self.assertEqual(base, 10_000.0)
        self.assertIn("시장평가", label)

    def test_a_valuation_failure_falls_back_to_the_smaller_sleeve(self):
        broker = PortfolioBroker(portfolio=None)
        base, label = sizing_base_value(broker, broker.get_cash_balance("US"))
        self.assertEqual(base, 10_000.0)
        self.assertIn("시장평가", label)


class ComputePositionSizeTests(unittest.TestCase):
    def test_risk_budget_comes_from_the_portfolio(self):
        # 1% of 50,000 = 500 risk / 2.5 per share = 200 shares — but the US
        # sleeve holds only 10,000 cash, so affordability caps it at 100.
        qty, note = compute_position_size(
            PortfolioBroker(), "US", entry=100.0, stop=97.5,
            risk_pct=1.0, max_position_pct=0.0,
        )
        self.assertEqual(qty, 100)
        self.assertIn("통합자산", note)
        self.assertIn("200주", note)  # the un-capped risk quantity is reported

    def test_cash_cap_stays_per_sleeve(self):
        # Bigger portfolio changes nothing when the sleeve cash is the binder.
        qty_small, _ = compute_position_size(
            PortfolioBroker(portfolio=50_000.0), "US", 100.0, 97.5, 1.0
        )
        qty_large, _ = compute_position_size(
            PortfolioBroker(portfolio=500_000.0), "US", 100.0, 97.5, 1.0
        )
        self.assertEqual(qty_small, 100)
        self.assertEqual(qty_large, 100)

    def test_position_cap_uses_the_portfolio_base(self):
        # 5% of 50,000 = 2,500 budget → 25 shares, tighter than sleeve cash.
        qty, note = compute_position_size(
            PortfolioBroker(), "US", entry=100.0, stop=97.5,
            risk_pct=1.0, max_position_pct=5.0,
        )
        self.assertEqual(qty, 25)
        self.assertIn("포지션 상한", note)

    def test_failure_path_reproduces_legacy_sleeve_sizing(self):
        # 1% of the 10,000 sleeve = 100 risk / 2.5 = 40 shares.
        qty, note = compute_position_size(
            PortfolioBroker(portfolio=None), "US", entry=100.0, stop=97.5,
            risk_pct=1.0, max_position_pct=0.0,
        )
        self.assertEqual(qty, 40)
        self.assertIn("시장평가", note)


if __name__ == "__main__":
    unittest.main()
