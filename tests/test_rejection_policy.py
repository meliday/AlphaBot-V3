from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.orchestrator import _on_cooldown, _rejection_retry_block
from alpha_bot.broker import MockBroker
from alpha_bot.models import OrderRequest


class RejectionRetryPolicyTests(unittest.TestCase):
    def _rejected(
        self,
        queue: ApprovalQueue,
        broker: MockBroker,
        when: datetime,
        *,
        retryable: bool,
        code: str,
    ):
        candidate = queue.enqueue(
            OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
            broker=broker,
        )
        updated = replace(
            candidate,
            status="rejected",
            submitted_at=when.isoformat(),
            rejection_code=code,
            rejection_retryable=retryable,
        )
        queue.update(updated)
        return updated

    def test_permanent_rejection_blocks_current_market_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            now = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)
            self._rejected(
                queue,
                broker,
                now - timedelta(hours=1),
                retryable=False,
                code="stock-restricted",
            )

            blocked, reason = _rejection_retry_block(
                queue, "NVDA", "US", broker, now=now
            )
            self.assertTrue(blocked)
            self.assertIn("stock-restricted", reason)

    def test_transient_rejections_use_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            now = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)
            self._rejected(
                queue,
                broker,
                now - timedelta(minutes=30),
                retryable=True,
                code="price-out-of-range",
            )
            self._rejected(
                queue,
                broker,
                now - timedelta(minutes=6),
                retryable=True,
                code="price-out-of-range",
            )

            blocked, reason = _rejection_retry_block(
                queue, "NVDA", "US", broker, now=now
            )
            self.assertTrue(blocked)  # second failure waits 10 minutes
            self.assertIn("2회 실패", reason)

            blocked, _ = _rejection_retry_block(
                queue, "NVDA", "US", broker, now=now + timedelta(minutes=5)
            )
            self.assertFalse(blocked)

    def test_rejected_and_cancelled_rows_do_not_consume_normal_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            now = datetime.now(timezone.utc)
            rejected = self._rejected(
                queue,
                broker,
                now,
                retryable=True,
                code="order-hours-closed",
            )
            self.assertFalse(
                _on_cooldown(queue, "NVDA", "US", timedelta(hours=24), broker)
            )
            queue.update(replace(rejected, status="cancelled"))
            self.assertFalse(
                _on_cooldown(queue, "NVDA", "US", timedelta(hours=24), broker)
            )


if __name__ == "__main__":
    unittest.main()
