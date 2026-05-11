import tempfile
import unittest
from pathlib import Path

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker import MockBroker
from alpha_bot.errors import ApprovalError
from alpha_bot.models import OrderRequest


class ApprovalBrokerTests(unittest.TestCase):
    def test_enqueue_and_approve_mock_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                stop_loss=90,
                target1=120,
                target2=135,
                analysis_signal="Buy",
            )
            updated, result = queue.approve(candidate.id, MockBroker(Path(tmp) / "ledger.json"))
            self.assertEqual(updated.status, "submitted")
            self.assertTrue(result.accepted)

    def test_duplicate_pending_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            request = OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0)
            queue.enqueue(request)
            with self.assertRaises(ApprovalError):
                queue.enqueue(request)


if __name__ == "__main__":
    unittest.main()
