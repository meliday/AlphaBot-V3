"""Exit-ladder tests: target-1 scale-out, trailing stop, target-2, and the
price-confirmed earnings_caution force-exit."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.position_manager import (
    manage_open_positions,
    remaining_quantity,
    should_force_exit,
)
from alpha_bot.broker import MockBroker
from alpha_bot.market_hours import MarketStatus
from alpha_bot.models import Candle, FundamentalsQuarter, NewsAssessment, OrderRequest
from alpha_bot.strategy import StrategyAnalyzer
from tests.factories import demo_candles, downtrend_candles

_OPEN = MarketStatus("US", True, "장중 (테스트)")


def flat_candles(price: float, days: int = 240) -> list[Candle]:
    """Flat series ending at `price` — ATR ≈ 2% of price."""
    out: list[Candle] = []
    day = date(2026, 1, 1)
    while len(out) < days:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        out.append(
            Candle(day, price, round(price * 1.01, 4), round(price * 0.99, 4), price, 1_000_000)
        )
    return out


class StubProvider:
    def __init__(self, price: float):
        self.price = price

    def get_candles(self, ticker, market, lookback=260):
        return flat_candles(self.price)[-lookback:]

    def get_fundamentals(self, ticker, market):
        return []

    def get_catalysts(self, ticker, market):
        return []

    def get_market_context(self, ticker, market):
        from alpha_bot.models import MarketContext
        return MarketContext()


def make_filled_buy(tmp: str, qty: int = 10, price: float = 100.0,
                    stop: float = 93.0, t1: float = 110.0, t2: float = 125.0):
    queue = ApprovalQueue(Path(tmp) / "pending.json")
    broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
    cand = queue.enqueue(
        OrderRequest("NVDA", "US", "buy", qty, "limit", price),
        stop_loss=stop, target1=t1, target2=t2, analysis_signal="Buy",
    )
    queue.approve(cand.id, broker)
    queue.sync_with_broker(broker)  # mock fills immediately at limit_price
    return queue, broker


def run_manager(queue, broker, price: float, provider=None):
    with patch("alpha_bot.auto.position_manager.market_status", return_value=_OPEN), \
         patch("alpha_bot.auto.position_manager.notify", lambda *a, **k: False):
        manage_open_positions(
            queue, broker, provider or StubProvider(price), lambda m: None
        )


class ScaleOutTests(unittest.TestCase):
    def test_target1_scales_out_half_and_arms_trail(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=111.0)  # ≥ t1=110

            orders = queue.list_orders()
            by_id = {o.id: o for o in orders}
            buy = next(o for o in orders if o.request.side == "buy")
            sells = [o for o in orders if o.request.side == "sell"]

            self.assertEqual(len(sells), 1)
            self.assertEqual(sells[0].request.quantity, 5)  # (10+1)//2
            self.assertEqual(sells[0].status, "submitted")
            # Linked as partial, not as the closing exit.
            self.assertEqual(buy.partial_exit_ids, [sells[0].id])
            self.assertIsNone(buy.exit_order_id)
            # Trail armed at max(breakeven, close − 2×ATR) — never below cost.
            self.assertIsNotNone(buy.trail_stop)
            self.assertGreaterEqual(buy.trail_stop, buy.avg_fill_price)

            # While the partial sell is in flight, remaining is uncertain →
            # no further sells may fire.
            _, inflight = remaining_quantity(buy, by_id)
            self.assertTrue(inflight)
            run_manager(queue, broker, price=130.0)  # above t2, but must wait
            self.assertEqual(
                len([o for o in queue.list_orders() if o.request.side == "sell"]), 1
            )

    def test_target2_closes_runner_after_scale_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=111.0)   # scale out 5
            queue.sync_with_broker(broker)            # partial sell fills
            run_manager(queue, broker, price=126.0)   # ≥ t2=125 → close runner

            orders = queue.list_orders()
            buy = next(o for o in orders if o.request.side == "buy")
            sells = {o.id: o for o in orders if o.request.side == "sell"}
            self.assertEqual(len(sells), 2)
            final = sells[buy.exit_order_id]
            self.assertEqual(final.request.quantity, 5)
            self.assertEqual(buy.exit_reason, "target2")

    def test_trailing_stop_ratchets_and_exits_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=111.0)   # scale out, trail armed
            queue.sync_with_broker(broker)

            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            trail_at_t1 = buy.trail_stop

            run_manager(queue, broker, price=120.0)   # no exit → ratchet up
            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            self.assertGreater(buy.trail_stop, trail_at_t1)
            ratcheted = buy.trail_stop

            run_manager(queue, broker, price=118.0)   # below high, above trail → no down-move
            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            self.assertEqual(buy.trail_stop, ratcheted)

            run_manager(queue, broker, price=ratcheted - 1)  # breach → market sell
            orders = queue.list_orders()
            buy = next(o for o in orders if o.request.side == "buy")
            self.assertIsNotNone(buy.exit_order_id)
            self.assertEqual(buy.exit_reason, "trail_stop")
            final = next(o for o in orders if o.id == buy.exit_order_id)
            self.assertEqual(final.request.order_type, "market")
            self.assertEqual(final.request.quantity, 5)

    def test_single_share_exits_in_full_at_target1(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=1)
            run_manager(queue, broker, price=111.0)
            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            self.assertIsNotNone(buy.exit_order_id)
            self.assertEqual(buy.exit_reason, "target1")
            self.assertEqual(buy.partial_exit_ids, [])

    def test_hard_stop_before_target1_sells_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=92.0)  # ≤ stop=93
            orders = queue.list_orders()
            buy = next(o for o in orders if o.request.side == "buy")
            final = next(o for o in orders if o.id == buy.exit_order_id)
            self.assertEqual(buy.exit_reason, "stop_loss")
            self.assertEqual(final.request.quantity, 10)
            self.assertEqual(final.request.order_type, "market")

    def test_live_quote_overrides_stale_daily_close(self):
        """Daily candle says 100 (no trigger) but the live quote says 92 —
        the intraday crash must fire the stop."""

        class LiveProvider(StubProvider):
            def get_current_price(self, ticker, market):
                return 92.0

        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=100.0, provider=LiveProvider(100.0))
            buy = next(o for o in queue.list_orders() if o.request.side == "buy")
            self.assertEqual(buy.exit_reason, "stop_loss")


class ForceExitConfirmationTests(unittest.TestCase):
    def _report(self, candles, news=None):
        return StrategyAnalyzer().analyze(
            "NVDA", "US", candles,
            [FundamentalsQuarter("latest", eps_yoy=-5.0, revenue_yoy=-3.0)],
            [], StubProvider(0).get_market_context("NVDA", "US"),
            news_assessment=news,
            use_live_market_data=False,
        )

    def test_earnings_caution_alone_does_not_exit_above_sma50(self):
        report = self._report(demo_candles())  # uptrend → close > SMA50
        self.assertTrue(report.earnings_caution)
        force, _ = should_force_exit(report)
        self.assertFalse(force)

    def test_earnings_caution_exits_when_price_confirms(self):
        report = self._report(downtrend_candles())  # close < SMA50
        self.assertTrue(report.earnings_caution)
        force, reason = should_force_exit(report)
        self.assertTrue(force)
        self.assertIn("50일선", reason)

    def test_severe_news_exits_immediately_regardless_of_price(self):
        news = NewsAssessment(
            sentiment="negative", severity="high", earnings_caution=False,
            score_adjustment=-3, reasoning="회계 부정 조사",
        )
        report = self._report(demo_candles(), news=news)
        force, reason = should_force_exit(report)
        self.assertTrue(force)
        self.assertIn("심각한 악재", reason)


if __name__ == "__main__":
    unittest.main()
