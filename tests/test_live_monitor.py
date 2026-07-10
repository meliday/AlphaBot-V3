"""Live exit monitor — tick-driven passes over the standard exit engine."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.live_monitor import (
    LiveExitMonitor,
    StreamPricedProvider,
    TickPriceCache,
    held_kr_tickers,
)
from alpha_bot.broker import MockBroker
from alpha_bot.data.stream import Tick
from alpha_bot.market_hours import MarketStatus
from alpha_bot.models import OrderRequest
from tests.test_position_lifecycle import StubProvider, make_filled_buy

_OPEN = MarketStatus("KR", True, "장중 (테스트)")


def _tick(price: float, ticker: str = "NVDA") -> Tick:
    return Tick(ticker, datetime(2026, 7, 10, 10, 0, 0), price, 10, 100)


def _run(monitor: LiveExitMonitor) -> bool:
    with patch("alpha_bot.auto.position_manager.market_status", return_value=_OPEN), \
         patch("alpha_bot.auto.position_manager.notify", lambda *a, **k: False):
        return monitor.evaluate_if_fresh()


class CountingProvider(StubProvider):
    def __init__(self, price: float):
        super().__init__(price)
        self.candle_calls = 0

    def get_candles(self, ticker, market, lookback=260):
        self.candle_calls += 1
        return super().get_candles(ticker, market, lookback)


class StreamPricedProviderTests(unittest.TestCase):
    def test_tick_price_wins_over_base(self):
        prices = TickPriceCache()
        provider = StreamPricedProvider(StubProvider(100.0), prices)
        self.assertIsNone(provider.get_current_price("NVDA", "US"))  # no tick, base has no quote
        prices.update(_tick(92.5))
        self.assertEqual(provider.get_current_price("NVDA", "US"), 92.5)

    def test_candles_are_ttl_cached(self):
        base = CountingProvider(100.0)
        provider = StreamPricedProvider(base, TickPriceCache(), candle_ttl=300)
        for _ in range(5):
            provider.get_candles("NVDA", "US", lookback=220)
        self.assertEqual(base.candle_calls, 1)


class LiveExitMonitorTests(unittest.TestCase):
    def test_first_pass_runs_then_waits_for_fresh_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)  # stop 93 / t1 110
            monitor = LiveExitMonitor(queue, broker, StubProvider(100.0), say=lambda m: None)
            self.assertTrue(_run(monitor))    # startup pass (full sweep once)
            self.assertFalse(_run(monitor))   # nothing new → no pass
            monitor.on_tick(_tick(100.5))
            self.assertTrue(_run(monitor))    # fresh tick → pass

    def test_tick_below_stop_fires_market_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)  # candles close at 100 (no trigger)
            monitor = LiveExitMonitor(queue, broker, StubProvider(100.0), say=lambda m: None)
            monitor.on_tick(_tick(92.0))      # intraday crash through stop 93
            _run(monitor)
            orders = queue.list_orders()
            buy = next(o for o in orders if o.request.side == "buy")
            self.assertEqual(buy.exit_reason, "stop_loss")
            sell = next(o for o in orders if o.id == buy.exit_order_id)
            self.assertEqual(sell.request.order_type, "market")
            self.assertEqual(sell.request.quantity, 10)


class SubscriptionSyncTests(unittest.TestCase):
    class FakeStream:
        def __init__(self):
            self._desired: set[str] = set()
            self.calls: list[tuple[str, str]] = []

        def desired(self):
            return set(self._desired)

        def subscribe(self, ticker):
            self._desired.add(ticker)
            self.calls.append(("sub", ticker))

        def unsubscribe(self, ticker):
            self._desired.discard(ticker)
            self.calls.append(("unsub", ticker))

    def _kr_filled_buy(self, tmp: str):
        queue = ApprovalQueue(Path(tmp) / "pending.json")
        broker = MockBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
        cand = queue.enqueue(
            OrderRequest("005930", "KR", "buy", 5, "limit", 70000.0),
            stop_loss=65000.0, target1=77000.0, target2=88000.0, analysis_signal="Buy",
        )
        queue.approve(cand.id, broker)
        queue.sync_with_broker(broker)
        return queue, broker

    def test_held_kr_position_gets_subscribed_and_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._kr_filled_buy(tmp)
            self.assertEqual(held_kr_tickers(queue), {"005930"})

            monitor = LiveExitMonitor(queue, broker, StubProvider(70000.0), say=lambda m: None)
            stream = self.FakeStream()
            watch = monitor.sync_subscriptions(stream)
            self.assertEqual(watch, {"005930"})
            self.assertIn(("sub", "005930"), stream.calls)

            # Position closes externally → next sync unsubscribes.
            queue.mark_externally_closed(
                next(o for o in queue.list_orders() if o.request.side == "buy").id
            )
            watch = monitor.sync_subscriptions(stream)
            self.assertEqual(watch, set())
            self.assertIn(("unsub", "005930"), stream.calls)

    def test_us_positions_are_not_stream_subscribed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _broker = make_filled_buy(tmp, qty=10)  # US NVDA
            self.assertEqual(held_kr_tickers(queue), set())


if __name__ == "__main__":
    unittest.main()
