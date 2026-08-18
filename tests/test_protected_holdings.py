"""Protected-holding tests.

The bot shares a brokerage account with the operator's own long-term/DCA
positions. Its entire position model assumes it opened what it manages, so
those holdings must be untouchable: no buys, no exits, no protective
stops. Enforcement is layered — enqueue() is the chokepoint every order
funnels through, with independent guards in the auto sweep and the
position manager so a single missed call site cannot expose them.

Also covers the fractional-quantity handling that made this urgent: DCA
shares bought by amount are fractional, and the previous strict guard
raised on them, silently disabling the broker-verification safety checks.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alpha_bot.approval import ApprovalQueue
from alpha_bot.config import _parse_tickers
from alpha_bot.errors import ApprovalError
from alpha_bot.models import OrderRequest

PROTECTED = frozenset({"VOO", "QQQM", "BRK.B", "QLD", "SCHD"})


class ParsingTests(unittest.TestCase):
    def test_comma_separated_string_from_the_simple_yaml_parser(self):
        self.assertEqual(_parse_tickers("VOO, QQQM, BRK.B"), {"VOO", "QQQM", "BRK.B"})

    def test_case_is_normalised_but_dots_and_hyphens_survive(self):
        self.assertEqual(_parse_tickers("brk.b, brk-b"), {"BRK.B", "BRK-B"})

    def test_absent_means_nothing_is_protected(self):
        self.assertEqual(_parse_tickers(None), frozenset())
        self.assertEqual(_parse_tickers(""), frozenset())


class EnqueueChokepointTests(unittest.TestCase):
    def _queue(self, tmp: str) -> ApprovalQueue:
        return ApprovalQueue(Path(tmp) / "pending.json", protected_tickers=PROTECTED)

    def test_a_protected_buy_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            with self.assertRaises(ApprovalError) as ctx:
                queue.enqueue(OrderRequest("VOO", "US", "buy", 1, "limit", 500.0))
            self.assertIn("protected holding", str(ctx.exception))
            self.assertEqual(queue.list_orders(), [])

    def test_a_protected_sell_is_refused_too(self):
        # Selling matters more than buying: it would liquidate the
        # operator's own long-term position.
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            with self.assertRaises(ApprovalError):
                queue.enqueue(OrderRequest("SCHD", "US", "sell", 10, "market", None))

    def test_matching_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            with self.assertRaises(ApprovalError):
                queue.enqueue(OrderRequest("voo", "US", "buy", 1, "limit", 500.0))

    def test_unprotected_tickers_are_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            order = queue.enqueue(OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0))
            self.assertEqual(order.request.ticker, "NVDA")

    def test_an_empty_protected_set_blocks_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            order = queue.enqueue(OrderRequest("VOO", "US", "buy", 1, "limit", 500.0))
            self.assertEqual(order.request.ticker, "VOO")


class PositionManagerGuardTests(unittest.TestCase):
    """Second layer: even a pre-existing queue row must not be traded."""

    def test_a_protected_position_is_never_managed(self):
        from unittest.mock import patch
        from alpha_bot.auto.position_manager import manage_open_positions
        from alpha_bot.broker import MockBroker
        from alpha_bot.market_hours import MarketStatus
        from tests.test_protective_stops import StubProvider

        with tempfile.TemporaryDirectory() as tmp:
            # Seed a filled VOO buy as if it predated the protection.
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "l.json", Path(tmp) / "s.json")
            cand = queue.enqueue(
                OrderRequest("VOO", "US", "buy", 10, "limit", 500.0),
                stop_loss=460.0, target1=600.0, target2=700.0,
            )
            queue.approve(cand.id, broker)
            queue.sync_with_broker(broker)

            with patch("alpha_bot.auto.position_manager.market_status",
                       return_value=MarketStatus("US", True, "장중")), \
                 patch("alpha_bot.auto.position_manager.notify", lambda *a, **k: False):
                # Price far below the stop — would normally trigger a sell.
                manage_open_positions(
                    queue, broker, StubProvider(400.0), lambda m: None,
                    protective_stops=True, protected_tickers=PROTECTED,
                )

            sells = [o for o in queue.list_orders() if o.request.side == "sell"]
            self.assertEqual(sells, [], "protected position was traded")
            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            self.assertIsNone(buy.protective_stop_id, "protected position was armed")


class FractionalHoldingTests(unittest.TestCase):
    """DCA shares are fractional; that must not disable position snapshots."""

    def test_fractions_floor_to_whole_shares(self):
        from alpha_bot.broker.toss import _floor_holding_quantity
        self.assertEqual(_floor_holding_quantity("5.07368", "QQQM"), 5)
        self.assertEqual(_floor_holding_quantity("10", "SCHD"), 10)

    def test_a_sub_share_holding_reads_as_zero_not_an_error(self):
        from alpha_bot.broker.toss import _floor_holding_quantity
        self.assertEqual(_floor_holding_quantity("0.2257", "BRK.B"), 0)

    def test_one_fractional_holding_no_longer_poisons_the_snapshot(self):
        from alpha_bot.broker.toss import TossBroker, TossSettings

        class Client:
            def request(self, method, path, **kwargs):
                return {"result": {"items": [
                    {"symbol": "QQQM", "marketCountry": "US", "quantity": "5.07368",
                     "averagePurchasePrice": "294.2", "lastPrice": "300",
                     "marketValue": {"amount": "1521"},
                     "profitLoss": {"amount": "29", "rate": "0.02"}},
                    {"symbol": "F", "marketCountry": "US", "quantity": "1",
                     "averagePurchasePrice": "13.94", "lastPrice": "14",
                     "marketValue": {"amount": "14"},
                     "profitLoss": {"amount": "0.06", "rate": "0.004"}},
                ]}}

        settings = TossSettings(client_id="c", client_secret="s", account_seq=1)
        broker = TossBroker(settings, client=Client())  # type: ignore[arg-type]
        held = {p.ticker: p.quantity for p in broker.get_positions("US")}
        # Before the fix this raised, and both callers swallowed it — which
        # silently disabled reconciliation and the pre-sell holding check.
        self.assertEqual(held, {"QQQM": 5, "F": 1})

    def test_placing_a_fractional_order_is_still_refused(self):
        from alpha_bot.broker.toss import _whole_quantity
        from alpha_bot.errors import BrokerError
        with self.assertRaises(BrokerError):
            _whole_quantity("1.5", "order quantity")


if __name__ == "__main__":
    unittest.main()
