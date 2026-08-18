"""Queue archiving tests.

The queue file is rewritten in full on every update, so it must stay
small — but its rows are the audit trail linking buys to exits, so
nothing may leave while any part of its story is still open. These pin
the boundary: fully-settled-and-old groups move out together; anything
open, recent, armed, or unprovable stays.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_bot.approval import ApprovalQueue
from alpha_bot.models import OrderCandidate, OrderRequest


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def seed(queue: ApprovalQueue, *orders: OrderCandidate) -> None:
    with queue._orders_transaction() as rows:  # test seam: direct seeding
        rows.extend(orders)


def buy_row(oid: str, ticker: str, *, status="filled", qty=10, filled=10,
            exit_id=None, partials=(), age_days=30, stop_id=None, stop_qty=0):
    return OrderCandidate(
        id=oid,
        request=OrderRequest(ticker, "US", "buy", qty, "limit", 100.0),
        status=status, created_at=iso_days_ago(age_days),
        filled_quantity=filled, avg_fill_price=100.0 if filled else None,
        exit_order_id=exit_id, partial_exit_ids=list(partials),
        protective_stop_id=stop_id, protective_stop_quantity=stop_qty,
        protective_stop_price=90.0 if (stop_id or stop_qty) else None,
    )


def sell_row(oid: str, ticker: str, *, status="filled", qty=10, filled=10,
             age_days=30):
    return OrderCandidate(
        id=oid,
        request=OrderRequest(ticker, "US", "sell", qty, "market", None),
        status=status, created_at=iso_days_ago(age_days),
        filled_quantity=filled, avg_fill_price=105.0 if filled else None,
    )


class ArchiveTests(unittest.TestCase):
    def _queue(self, tmp):
        return ApprovalQueue(Path(tmp) / "pending.json"), Path(tmp) / "arch"

    def test_a_settled_old_group_moves_out_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1", partials=["P1"]),
                sell_row("P1", "NVDA", qty=5, filled=5),
                sell_row("S1", "NVDA", qty=5, filled=5),
                buy_row("B2", "AAPL", status="submitted", filled=0),  # still open
            )
            moved = queue.archive_closed_orders(archive_dir=arch, min_age_days=7)
            self.assertEqual(moved, 3)

            remaining = {o.id for o in queue.list_orders()}
            self.assertEqual(remaining, {"B2"})

            archived = json.loads(next(arch.glob("*.json")).read_text())["orders"]
            self.assertEqual({r["id"] for r in archived}, {"B1", "P1", "S1"})

    def test_recent_groups_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1", age_days=1),
                sell_row("S1", "NVDA", age_days=1),
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 0)
            self.assertEqual(len(queue.list_orders()), 2)

    def test_open_shares_keep_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            # 10 bought, only 5 sold — remaining shares are a live position.
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1"),
                sell_row("S1", "NVDA", qty=5, filled=5),
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 0)

    def test_an_armed_or_pending_protective_stop_keeps_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1", stop_id="COND-1", stop_qty=10),
                sell_row("S1", "NVDA"),
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 0)

    def test_dead_intents_archive_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            seed(queue, buy_row("B1", "NVDA", status="rejected", filled=0))
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 1)
            self.assertEqual(queue.list_orders(), [])

    def test_second_run_is_a_no_op_and_never_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, arch = self._queue(tmp)
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1"),
                sell_row("S1", "NVDA"),
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 2)
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 0)
            archived = json.loads(next(arch.glob("*.json")).read_text())["orders"]
            self.assertEqual(len(archived), 2)


if __name__ == "__main__":
    unittest.main()


class UnreferencedSellTests(unittest.TestCase):
    def test_inert_rejected_sells_archive_but_filled_ones_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            arch = Path(tmp) / "arch"
            seed(
                queue,
                sell_row("S1", "SNDK", status="rejected", filled=0),   # inert spam
                sell_row("S2", "SNDK", status="filled", filled=10),    # unreferenced but real
                sell_row("S3", "SNDK", status="rejected", filled=0, age_days=1),  # too fresh
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 1)
            remaining = {o.id for o in queue.list_orders()}
            self.assertEqual(remaining, {"S2", "S3"})

    def test_a_referenced_rejected_sell_stays_with_its_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            arch = Path(tmp) / "arch"
            # Open position whose exit attempt was rejected — the story is
            # still live; neither row may leave.
            seed(
                queue,
                buy_row("B1", "NVDA", exit_id="S1"),
                sell_row("S1", "NVDA", status="rejected", filled=0),
            )
            self.assertEqual(queue.archive_closed_orders(archive_dir=arch), 0)
