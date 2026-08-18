from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from multiprocessing import Process
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.position_manager import manage_open_positions, reconcile_queue_with_broker
from alpha_bot.broker import MockBroker
from alpha_bot.errors import ApprovalError, BrokerError
from alpha_bot.market_hours import MarketStatus
from alpha_bot.models import OrderFill, OrderRequest, OrderResult
from tests.test_position_lifecycle import StubProvider


def _enqueue_many(path: str, prefix: str) -> None:
    queue = ApprovalQueue(Path(path))
    for index in range(20):
        queue.enqueue(
            OrderRequest(f"{prefix}{index}", "US", "buy", 1, "limit", 100.0)
        )


class _DelegatingBroker:
    """Keep one broker scope while overriding selected network behaviour."""

    def __init__(self, source: MockBroker):
        self.source = source
        self.name = source.name
        self.instance_id = source.instance_id
        self.account_id = source.account_id
        self.mode = source.mode

    def place_order(self, order):
        return self.source.place_order(order)

    def get_cash_balance(self, market):
        return self.source.get_cash_balance(market)

    def cancel_order(self, *args):
        return self.source.cancel_order(*args)


class BrokerScopeSafetyTests(unittest.TestCase):
    def test_queue_read_modify_write_is_safe_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pending.json")
            first = Process(target=_enqueue_many, args=(path, "A"))
            second = Process(target=_enqueue_many, args=(path, "B"))
            first.start()
            second.start()
            first.join(10)
            second.join(10)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            self.assertEqual(len(ApprovalQueue(Path(path)).list_orders()), 40)

    def test_bound_order_cannot_be_approved_by_another_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            first = MockBroker(Path(tmp) / "one.json", Path(tmp) / "one-state.json")
            second = MockBroker(Path(tmp) / "two.json", Path(tmp) / "two-state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                broker=first,
            )

            with self.assertRaises(ApprovalError):
                queue.approve(candidate.id, second)
            queue.approve(candidate.id, first)

    def test_sync_never_queries_another_account_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            owner = MockBroker(Path(tmp) / "owner.json", Path(tmp) / "owner-state.json")
            other = MockBroker(Path(tmp) / "other.json", Path(tmp) / "other-state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                broker=owner,
            )
            queue.approve(candidate.id, owner)

            class OtherAccount(_DelegatingBroker):
                def __init__(self, source):
                    super().__init__(source)
                    self.fill_calls = 0

                def get_order_fill(self, *args):
                    self.fill_calls += 1
                    raise AssertionError("foreign order must not be queried")

            foreign = OtherAccount(other)
            self.assertEqual(queue.sync_with_broker(foreign), [])
            self.assertEqual(foreign.fill_calls, 0)

    def test_legacy_active_order_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0)
            )
            queue.update(replace(candidate, status="submitted", broker="mock"))

            self.assertEqual([candidate.id], [o.id for o in queue.unscoped_broker_orders(broker)])
            self.assertEqual(queue.sync_with_broker(broker), [])


class QueryFailureSafetyTests(unittest.TestCase):
    def _filled_buy(self, tmp: str, ticker: str = "NVDA"):
        queue = ApprovalQueue(Path(tmp) / "pending.json")
        broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
        candidate = queue.enqueue(
            OrderRequest(ticker, "US", "buy", 5, "limit", 100.0),
            broker=broker,
            stop_loss=95.0,
            target1=110.0,
        )
        queue.approve(candidate.id, broker)
        queue.sync_with_broker(broker)
        return queue, broker, candidate.id

    def test_reconcile_does_not_treat_position_outage_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, buy_id = self._filled_buy(tmp)

            class Outage(_DelegatingBroker):
                def get_positions(self, market):
                    raise BrokerError("temporary position outage")

            self.assertEqual(reconcile_queue_with_broker(queue, Outage(broker)), [])
            buy = next(o for o in queue.list_orders() if o.id == buy_id)
            self.assertIsNone(buy.exit_order_id)

    def test_position_manager_never_marks_external_close_during_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, buy_id = self._filled_buy(tmp)

            class Outage(_DelegatingBroker):
                def get_positions(self, market):
                    raise BrokerError("temporary position outage")

            with patch(
                "alpha_bot.auto.position_manager.market_status",
                return_value=MarketStatus("US", True, "test"),
            ):
                manage_open_positions(
                    queue, Outage(broker), StubProvider(90.0), lambda _message: None
                )

            orders = queue.list_orders()
            buy = next(o for o in orders if o.id == buy_id)
            # A real stop may still be sent from the locally confirmed
            # position (exit protection must continue during a read outage),
            # but the buy must never be synthesised as an external close.
            self.assertNotEqual(buy.exit_reason, "external_close")
            self.assertFalse(any(o.id.startswith("EXT-") for o in orders))


class FillMonotonicityTests(unittest.TestCase):
    def test_confirmed_partial_fill_cannot_regress_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 5, "limit", 100.0),
                broker=broker,
            )
            queue.approve(candidate.id, broker)
            row = next(o for o in queue.list_orders() if o.id == candidate.id)
            queue.update(
                replace(
                    row,
                    status="partially_filled",
                    filled_quantity=2,
                    avg_fill_price=100.0,
                )
            )

            class LookupMiss(_DelegatingBroker):
                def get_order_fill(self, broker_order_id, market, ordered_quantity):
                    return OrderFill(
                        broker_order_id,
                        "submitted",
                        0,
                        ordered_quantity,
                        None,
                        "temporary lookup miss",
                    )

            queue.sync_with_broker(LookupMiss(broker))
            updated = next(o for o in queue.list_orders() if o.id == candidate.id)
            self.assertEqual(updated.status, "partially_filled")
            self.assertEqual(updated.filled_quantity, 2)
            self.assertEqual(updated.avg_fill_price, 100.0)

    def test_transport_exception_marks_outcome_unknown_not_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            base = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                broker=base,
            )

            class Timeout(_DelegatingBroker):
                def place_order(self, order):
                    raise BrokerError("response timed out")

            with self.assertRaises(BrokerError):
                queue.approve(candidate.id, Timeout(base))
            updated = next(o for o in queue.list_orders() if o.id == candidate.id)
            self.assertEqual(updated.status, "unknown")
            self.assertEqual([updated.id], [o.id for o in queue.unresolved_orders(base)])


if __name__ == "__main__":
    unittest.main()
