"""P1 risk-hardening tests: kill switch, daily-loss breaker, position cap,
stale-order cancellation, and Telegram notify plumbing."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.guards import (
    daily_loss_exceeded,
    kill_switch_active,
    realized_pnl_today,
    unpriced_external_closes_today,
)
from alpha_bot.auto.sizing import compute_position_size
from alpha_bot.broker import MockBroker
from alpha_bot.models import OrderRequest


def _filled_round_trip(tmp: str, buy_price=100.0, sell_price=90.0, qty=10,
                       link="exit_order_id"):
    """Create a filled buy + filled sell pair linked via exit/partial ids."""
    queue = ApprovalQueue(Path(tmp) / "pending.json")
    broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
    buy = queue.enqueue(
        OrderRequest("NVDA", "US", "buy", qty, "limit", buy_price),
        stop_loss=buy_price * 0.93, target1=buy_price * 1.1, analysis_signal="Buy",
    )
    queue.approve(buy.id, broker)
    queue.sync_with_broker(broker)
    sell = queue.enqueue(
        OrderRequest("NVDA", "US", "sell", qty, "limit", sell_price),
        analysis_signal="Sell",
    )
    # Link before approval so the buy row records the relationship.
    buy_row = next(o for o in queue.list_orders() if o.id == buy.id)
    if link == "exit_order_id":
        queue.update(replace(buy_row, exit_order_id=sell.id, exit_reason="stop_loss"))
    else:
        queue.update(replace(buy_row, partial_exit_ids=[sell.id]))
    queue.approve(sell.id, broker)
    queue.sync_with_broker(broker)
    return queue, broker


class KillSwitchTests(unittest.TestCase):
    def test_inactive_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_KILL_SWITCH": str(Path(tmp) / "KS")}):
                self.assertIsNone(kill_switch_active())

    def test_active_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "KS"
            path.write_text("수동 점검 중\n", encoding="utf-8")
            with patch.dict(os.environ, {"BOT_KILL_SWITCH": str(path)}):
                self.assertEqual(kill_switch_active(), "수동 점검 중")

    def test_empty_file_still_engages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "KS"
            path.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {"BOT_KILL_SWITCH": str(path)}):
                self.assertIsNotNone(kill_switch_active())


class DailyLossTests(unittest.TestCase):
    def test_realized_loss_via_exit_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = _filled_round_trip(tmp, 100.0, 90.0, 10)
            self.assertAlmostEqual(realized_pnl_today(queue, "US"), -100.0)

    def test_realized_loss_via_partial_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = _filled_round_trip(tmp, 100.0, 95.0, 10, link="partial")
            self.assertAlmostEqual(realized_pnl_today(queue, "US"), -50.0)

    def test_breaker_trips_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = _filled_round_trip(tmp, 100.0, 90.0, 10)
            # Mock account: 10_000 start − 1_000 buy + 900 sell = 9_900 total.
            tripped, detail = daily_loss_exceeded(queue, broker, "US", limit_pct=1.0)
            self.assertTrue(tripped)
            self.assertIn("한도", detail)

    def test_breaker_holds_under_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = _filled_round_trip(tmp, 100.0, 90.0, 10)
            tripped, _ = daily_loss_exceeded(queue, broker, "US", limit_pct=3.0)
            self.assertFalse(tripped)

    def test_breaker_disabled_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = _filled_round_trip(tmp, 100.0, 50.0, 10)
            tripped, _ = daily_loss_exceeded(queue, broker, "US", limit_pct=0.0)
            self.assertFalse(tripped)

    def test_profit_never_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = _filled_round_trip(tmp, 100.0, 120.0, 10)
            self.assertAlmostEqual(realized_pnl_today(queue, "US"), 200.0)
            tripped, _ = daily_loss_exceeded(queue, broker, "US", limit_pct=0.5)
            self.assertFalse(tripped)

    def test_unpriced_external_close_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            buy = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 10, "limit", 100.0)
            )
            queue.approve(buy.id, broker)
            queue.sync_with_broker(broker)
            queue.mark_externally_closed(buy.id, broker=broker)

            self.assertEqual(len(unpriced_external_closes_today(queue, "US")), 1)
            tripped, detail = daily_loss_exceeded(
                queue, broker, "US", limit_pct=1.0
            )
            self.assertTrue(tripped)
            self.assertIn("체결가", detail)
            self.assertIn("안전 차단", detail)


class PositionCapTests(unittest.TestCase):
    def test_cap_shrinks_oversized_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            # total 10_000; risk 1% = 100; per-share risk 2.5 → 40 shares
            # = $4_000 (40% of account!). 20% cap → 20 shares.
            qty, note = compute_position_size(
                broker, "US", entry=100.0, stop=97.5, risk_pct=1.0,
                max_position_pct=20.0,
            )
            self.assertEqual(qty, 20)
            self.assertIn("포지션 상한", note)

    def test_cap_disabled_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            qty, _ = compute_position_size(
                broker, "US", entry=100.0, stop=97.5, risk_pct=1.0,
                max_position_pct=0.0,
            )
            self.assertEqual(qty, 40)


class StaleOrderTests(unittest.TestCase):
    def _submitted_order(self, tmp: str, submitted_at: str):
        queue = ApprovalQueue(Path(tmp) / "pending.json")
        broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
        cand = queue.enqueue(
            OrderRequest("NVDA", "US", "buy", 5, "limit", 100.0),
            analysis_signal="Buy",
        )
        queue.approve(cand.id, broker)  # → submitted (mock accepts)
        row = next(o for o in queue.list_orders() if o.id == cand.id)
        queue.update(replace(row, submitted_at=submitted_at))
        return queue, broker, cand.id

    def test_old_unfilled_order_is_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, oid = self._submitted_order(tmp, "2026-07-08T00:00:00+00:00")
            cancelled = queue.cancel_stale_orders(broker, max_age_minutes=60)
            self.assertEqual([o.id for o in cancelled], [oid])
            row = next(o for o in queue.list_orders() if o.id == oid)
            self.assertEqual(row.status, "cancelled")
            # Broker-side: fill lookups now report cancelled, and the mock
            # ledger no longer counts the row toward positions.
            fill = broker.get_order_fill(row.broker_order_id, "US", 5)
            self.assertEqual(fill.status, "cancelled")
            self.assertEqual(broker.get_positions("US"), [])

    def test_fresh_order_is_kept(self):
        from alpha_bot.models import utc_now_iso
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, oid = self._submitted_order(tmp, utc_now_iso())
            self.assertEqual(queue.cancel_stale_orders(broker, 60), [])
            row = next(o for o in queue.list_orders() if o.id == oid)
            self.assertEqual(row.status, "submitted")

    def test_partially_filled_buy_cancels_only_remainder_in_local_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, oid = self._submitted_order(tmp, "2026-07-08T00:00:00+00:00")
            row = next(o for o in queue.list_orders() if o.id == oid)
            queue.update(replace(row, status="partially_filled", filled_quantity=2))
            cancelled = queue.cancel_stale_orders(broker, 60)
            self.assertEqual([o.id for o in cancelled], [oid])
            updated = next(o for o in queue.list_orders() if o.id == oid)
            self.assertEqual(updated.status, "partially_filled_cancelled")
            self.assertEqual(updated.filled_quantity, 2)

    def test_stale_sell_is_never_cancelled_by_entry_freshness_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "sell", 1, "limit", 100.0),
                broker=broker,
            )
            queue.approve(candidate.id, broker)
            row = next(o for o in queue.list_orders() if o.id == candidate.id)
            queue.update(replace(row, submitted_at="2026-07-08T00:00:00+00:00"))
            self.assertEqual(queue.cancel_stale_orders(broker, 60), [])

    def test_disabled_at_zero_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker, _ = self._submitted_order(tmp, "2026-07-08T00:00:00+00:00")
            self.assertEqual(queue.cancel_stale_orders(broker, 0), [])


class NotifyTests(unittest.TestCase):
    def test_noop_without_credentials(self):
        from alpha_bot import notify as notify_mod
        env = {k: v for k, v in os.environ.items()
               if k not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(notify_mod, "load_dotenv", lambda *a, **k: None):
            self.assertFalse(notify_mod.notify("test"))

    def test_dedupe_suppresses_repeat_sends(self):
        from alpha_bot import notify as notify_mod

        calls: list[str] = []

        class FakeResponse:
            def read(self):
                return b'{"ok": true}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=10):
            calls.append(request.full_url)
            return FakeResponse()

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "C",
        }), patch.object(notify_mod, "load_dotenv", lambda *a, **k: None), \
             patch.object(notify_mod.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(notify_mod.notify("hello", dedupe_key="k1"))
            self.assertFalse(notify_mod.notify("hello", dedupe_key="k1"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class PortfolioBreakerTests(unittest.TestCase):
    """The breaker measures losses against the same base sizing uses.

    Positions are sized as a % of the FX-unified portfolio; measuring the
    loss against the (possibly much smaller) sleeve would trip the breaker
    on a single normally-sized stop-out whenever most of the account sits
    in the other currency.
    """

    class PortfolioMock(MockBroker):
        portfolio = 100_000.0

        def portfolio_value(self, currency: str) -> float:
            return self.portfolio

    def _round_trip(self, tmp, portfolio: float):
        queue, _ = _filled_round_trip(tmp, 100.0, 90.0, 10)  # -100 realized
        broker = self.PortfolioMock(Path(tmp) / "l2.json", Path(tmp) / "s2.json")
        broker.portfolio = portfolio
        return queue, broker

    def test_a_normal_stop_out_does_not_trip_against_the_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            # -100 on a 100k portfolio = 0.1%; the sleeve alone (10k mock
            # default) would have read it as 1% and tripped at limit 1.0.
            queue, broker = self._round_trip(tmp, portfolio=100_000.0)
            tripped, _ = daily_loss_exceeded(queue, broker, "US", limit_pct=1.0)
            self.assertFalse(tripped)

    def test_portfolio_scale_losses_still_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._round_trip(tmp, portfolio=2_000.0)  # -100 = 5%
            tripped, detail = daily_loss_exceeded(queue, broker, "US", limit_pct=3.0)
            self.assertTrue(tripped)
            self.assertIn("통합자산", detail)
