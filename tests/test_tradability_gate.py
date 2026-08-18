"""Pre-trade tradability gate tests.

A price/volume screen cannot see that a symbol is being delisted, is
suspended, or carries an exchange designation. Those states are also the
ones the exit ladder cannot trade its way out of, so this gate fails
closed where the regime and LLM gates fail open.
"""

from __future__ import annotations

import unittest

from alpha_bot.auto.orchestrator import _tradability_block
from alpha_bot.broker.base import supports_tradability_checks
from alpha_bot.broker.toss import TossBroker, TossSettings
from alpha_bot.errors import BrokerError


class StubClient:
    """Serves /api/v1/stocks and /warnings; everything else is unexpected."""

    def __init__(self, *, meta: dict | None = None, warnings: list | None = None):
        self.meta = meta if meta is not None else {
            "symbol": "005930",
            "securityType": "STOCK",
            "status": "ACTIVE",
            "koreanMarketDetail": {
                "liquidationTrading": False,
                "krxTradingSuspended": False,
            },
        }
        self.warnings = warnings or []
        self.warning_calls = 0

    def request(self, method, path, **kwargs):
        if path == "/api/v1/stocks":
            return {"result": [self.meta]}
        if path.endswith("/warnings"):
            self.warning_calls += 1
            return {"result": self.warnings}
        raise AssertionError(f"unexpected call: {method} {path}")


def make_broker(client) -> TossBroker:
    settings = TossSettings(client_id="c", client_secret="s")
    return TossBroker(settings, client=client)  # type: ignore[arg-type]


class TossTradabilityTests(unittest.TestCase):
    def test_a_clean_active_symbol_is_tradable(self):
        broker = make_broker(StubClient())
        self.assertIsNone(broker.tradability_block("005930", "KR"))

    def test_liquidation_trading_blocks(self):
        client = StubClient(meta={
            "symbol": "005930", "securityType": "STOCK", "status": "ACTIVE",
            "koreanMarketDetail": {"liquidationTrading": True},
        })
        reason = make_broker(client).tradability_block("005930", "KR")
        self.assertIn("정리매매", reason or "")
        # Short-circuits before spending a warnings call.
        self.assertEqual(client.warning_calls, 0)

    def test_krx_suspension_blocks(self):
        client = StubClient(meta={
            "symbol": "005930", "securityType": "STOCK", "status": "ACTIVE",
            "koreanMarketDetail": {"krxTradingSuspended": True},
        })
        self.assertIn("거래정지", make_broker(client).tradability_block("005930", "KR") or "")

    def test_non_active_listing_status_blocks(self):
        client = StubClient(meta={
            "symbol": "005930", "securityType": "STOCK", "status": "DELISTED",
        })
        self.assertIn("DELISTED", make_broker(client).tradability_block("005930", "KR") or "")

    def test_investment_risk_designation_blocks(self):
        client = StubClient(warnings=[
            {"warningType": "INVESTMENT_RISK", "startDate": "2026-08-01"},
        ])
        self.assertIn("INVESTMENT_RISK", make_broker(client).tradability_block("005930", "KR") or "")

    def test_volatility_interruption_blocks(self):
        client = StubClient(warnings=[{"warningType": "VI_DYNAMIC"}])
        self.assertIn("VI_DYNAMIC", make_broker(client).tradability_block("005930", "KR") or "")

    def test_an_unlisted_warning_type_does_not_block(self):
        client = StubClient(warnings=[{"warningType": "STOCK_WARRANTS"}])
        self.assertIsNone(make_broker(client).tradability_block("005930", "KR"))

    def test_missing_metadata_raises_so_callers_can_fail_closed(self):
        class Empty:
            def request(self, method, path, **kwargs):
                if path == "/api/v1/stocks":
                    return {"result": []}
                raise AssertionError("should not reach warnings")

        with self.assertRaises(BrokerError):
            make_broker(Empty()).tradability_block("005930", "KR")


class OrchestratorGateTests(unittest.TestCase):
    def test_brokers_without_the_capability_are_not_gated(self):
        class Plain:
            pass

        self.assertFalse(supports_tradability_checks(Plain()))
        self.assertEqual(_tradability_block(Plain(), "NVDA", "US"), (False, ""))

    def test_a_clear_symbol_passes(self):
        broker = make_broker(StubClient())
        blocked, reason = _tradability_block(broker, "005930", "KR")
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_a_flagged_symbol_is_blocked_with_its_reason(self):
        broker = make_broker(StubClient(warnings=[{"warningType": "OVERHEATED"}]))
        blocked, reason = _tradability_block(broker, "005930", "KR")
        self.assertTrue(blocked)
        self.assertIn("OVERHEATED", reason)

    def test_a_lookup_failure_fails_closed(self):
        class Broken:
            def tradability_block(self, ticker, market):
                raise BrokerError("venue unreachable")

        blocked, reason = _tradability_block(Broken(), "005930", "KR")
        self.assertTrue(blocked)
        self.assertIn("확인 실패", reason)


class HighValueOrderTests(unittest.TestCase):
    """Toss refuses orders >= 100M KRW without explicit acknowledgement."""

    def _broker(self, *, allow: bool):
        class Client:
            def __init__(self):
                self.payloads = []

            def request(self, method, path, **kwargs):
                if path == "/api/v1/stocks":
                    return {"result": [{"symbol": "005930", "securityType": "STOCK"}]}
                self.payloads.append(kwargs.get("payload"))
                return {"result": {"orderId": "TOSS-1"}}

        client = Client()
        settings = TossSettings(
            client_id="c", client_secret="s", account_seq=1,
            enable_live_orders=True, allow_high_value_orders=allow,
        )
        return TossBroker(settings, client=client), client  # type: ignore[arg-type]

    def test_an_oversized_order_is_refused_locally_by_default(self):
        from alpha_bot.errors import BrokerOrderRejected
        from alpha_bot.models import OrderRequest

        broker, client = self._broker(allow=False)
        order = OrderRequest("005930", "KR", "buy", 2000, "limit", 60_000.0)
        with self.assertRaises(BrokerOrderRejected) as ctx:
            broker.place_order(order)
        self.assertEqual(ctx.exception.code, "confirm-high-value-required")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(client.payloads, [])  # never reached the venue

    def test_an_order_below_the_threshold_passes_untouched(self):
        from alpha_bot.models import OrderRequest

        broker, client = self._broker(allow=False)
        broker.place_order(OrderRequest("005930", "KR", "buy", 10, "limit", 60_000.0))
        self.assertNotIn("confirmHighValueOrder", client.payloads[0])

    def test_explicit_opt_in_forwards_the_acknowledgement(self):
        from alpha_bot.models import OrderRequest

        broker, client = self._broker(allow=True)
        broker.place_order(OrderRequest("005930", "KR", "buy", 2000, "limit", 60_000.0))
        self.assertTrue(client.payloads[0]["confirmHighValueOrder"])


if __name__ == "__main__":
    unittest.main()
