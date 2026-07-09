import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker import MockBroker
from alpha_bot.errors import ApprovalError
from alpha_bot.models import OrderRequest, OrderResult
from alpha_bot.web.handlers_portfolio import handle_bot_stats


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

    def test_approve_claims_order_before_broker_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0))
            seen: dict[str, str] = {}

            class ReentrantBroker:
                name = "reentrant"

                def place_order(self, order):
                    try:
                        queue.approve(candidate.id, self)
                    except ApprovalError as exc:
                        seen["error"] = str(exc)
                    else:
                        seen["error"] = "nested approve succeeded"
                    return OrderResult(self.name, True, "BROKER-1", "accepted")

            updated, result = queue.approve(candidate.id, ReentrantBroker())

            self.assertEqual(updated.status, "submitted")
            self.assertTrue(result.accepted)
            self.assertIn("status=submitting", seen["error"])

    def test_mock_partial_sell_preserves_cost_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            broker.place_order(OrderRequest("NVDA", "US", "buy", 10, "limit", 100.0))
            broker.place_order(OrderRequest("NVDA", "US", "sell", 5, "limit", 110.0))

            positions = broker.get_positions("US")
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].quantity, 5)
            self.assertAlmostEqual(positions[0].avg_price, 100.0)
            self.assertAlmostEqual(positions[0].market_value, 500.0)

            balance = broker.get_cash_balance("US")
            self.assertAlmostEqual(balance.cash, 9550.0)
            self.assertAlmostEqual(balance.securities_value, 500.0)
            self.assertAlmostEqual(balance.total_value, 10050.0)

    def test_bot_stats_reads_activity_log_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            records = [
                {"event": "query", "signal": "Buy"},
                {"event": "query", "signal": "Wait"},
                {"event": "queue"},
                {"event": "trade", "status": "submitted"},
                {"event": "trade", "status": "rejected"},
            ]
            lines = [json.dumps(record) for record in records]
            (log_dir / f"activity_{today}.jsonl").write_text(
                "\n".join(lines),
                encoding="utf-8",
            )

            with patch("alpha_bot.audit_log.LOG_DIR", log_dir):
                stats = handle_bot_stats()

            self.assertEqual(stats["date"], today)
            self.assertEqual(stats["scans"], 2)
            self.assertEqual(stats["signals"], 1)
            self.assertEqual(stats["orders"], 1)
            self.assertEqual(stats["fills"], 1)


if __name__ == "__main__":
    unittest.main()
