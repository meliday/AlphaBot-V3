"""Broker-side protective stop tests.

The property under test throughout is **at most one seller**: the venue
stop and the polling ladder must never both be live against the same
shares. Everything else (arming, re-arming, expiry recovery) exists to
serve that invariant while keeping the position covered when the bot is
not running.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto.position_manager import manage_open_positions
from alpha_bot.auto.protective_stops import (
    effective_stop_price,
    release_protective_stop,
    stop_engaged,
    sync_protective_stop,
)
from alpha_bot.broker import MockBroker
from alpha_bot.broker.base import supports_protective_stops
from alpha_bot.market_hours import MarketStatus
from alpha_bot.models import Candle, OrderRequest

_OPEN = MarketStatus("US", True, "장중 (테스트)")


def flat_candles(price: float, days: int = 240) -> list[Candle]:
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
    queue.sync_with_broker(broker)
    return queue, broker


def run_manager(queue, broker, price: float, *, protective: bool = True):
    with patch("alpha_bot.auto.position_manager.market_status", return_value=_OPEN), \
         patch("alpha_bot.auto.position_manager.notify", lambda *a, **k: False):
        manage_open_positions(
            queue, broker, StubProvider(price), lambda m: None,
            protective_stops=protective,
        )


def the_buy(queue):
    return next(o for o in queue.list_orders() if o.request.side == "buy")


def sells(queue):
    return [o for o in queue.list_orders() if o.request.side == "sell"]


class CountingBroker(MockBroker):
    """Mock that records protective-stop traffic for call-count assertions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.places = 0
        self.amends = 0
        self.cancels = 0

    def place_protective_stop(self, **kwargs):
        self.places += 1
        return super().place_protective_stop(**kwargs)

    def amend_protective_stop(self, stop_id, **kwargs):
        self.amends += 1
        return super().amend_protective_stop(stop_id, **kwargs)

    def cancel_protective_stop(self, stop_id):
        self.cancels += 1
        return super().cancel_protective_stop(stop_id)


class CapabilityTests(unittest.TestCase):
    def test_mock_broker_advertises_the_capability(self):
        self.assertTrue(supports_protective_stops(MockBroker))

    def test_plain_object_does_not(self):
        self.assertFalse(supports_protective_stops(object()))

    def test_replaying_a_client_order_id_does_not_double_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = MockBroker(Path(tmp) / "l.json", Path(tmp) / "s.json")
            kwargs = dict(
                ticker="NVDA", market="US", quantity=10,
                stop_price=93.0, client_order_id="ps-ORD-1-abc",
            )
            first = broker.place_protective_stop(**kwargs)
            second = broker.place_protective_stop(**kwargs)
            self.assertEqual(first, second)


class EffectiveStopTests(unittest.TestCase):
    def test_hard_stop_governs_before_the_scale_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = make_filled_buy(tmp, stop=93.0)
            buy = replace(the_buy(queue), trail_stop=97.0)
            self.assertEqual(effective_stop_price(buy, scaled_out=False), 93.0)

    def test_trail_governs_the_runner_after_the_scale_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = make_filled_buy(tmp, stop=93.0)
            buy = replace(the_buy(queue), trail_stop=97.0)
            self.assertEqual(effective_stop_price(buy, scaled_out=True), 97.0)

    def test_runner_falls_back_to_the_hard_stop_before_the_trail_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = make_filled_buy(tmp, stop=93.0)
            buy = replace(the_buy(queue), trail_stop=None)
            self.assertEqual(effective_stop_price(buy, scaled_out=True), 93.0)


class ArmingTests(unittest.TestCase):
    def test_holding_arms_the_venue_stop_at_the_planned_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, stop=93.0)
            run_manager(queue, broker, price=100.0)  # no exit trigger

            buy = the_buy(queue)
            self.assertIsNotNone(buy.protective_stop_id)
            self.assertEqual(buy.protective_stop_price, 93.0)
            self.assertEqual(buy.protective_stop_quantity, 10)
            self.assertEqual(
                broker.protective_stop_status(buy.protective_stop_id), "WATCHING"
            )

    def test_disabled_leaves_the_venue_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0, protective=False)
            self.assertIsNone(the_buy(queue).protective_stop_id)

    def test_steady_state_sweeps_cost_no_venue_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = CountingBroker(Path(tmp) / "l.json", Path(tmp) / "s.json")
            cand = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 10, "limit", 100.0),
                stop_loss=93.0, target1=110.0, target2=125.0, analysis_signal="Buy",
            )
            queue.approve(cand.id, broker)
            queue.sync_with_broker(broker)

            for _ in range(4):
                run_manager(queue, broker, price=100.0)

            self.assertEqual(broker.places, 1)
            self.assertEqual(broker.amends, 0)

    def test_a_dead_venue_stop_is_re_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            first_id = the_buy(queue).protective_stop_id

            # Simulate expiry / manual cancellation at the venue.
            broker.cancel_protective_stop(first_id)
            run_manager(queue, broker, price=100.0)

            buy = the_buy(queue)
            self.assertIsNotNone(buy.protective_stop_id)
            self.assertNotEqual(buy.protective_stop_id, first_id)
            self.assertEqual(
                broker.protective_stop_status(buy.protective_stop_id), "WATCHING"
            )

    def test_a_venue_failure_degrades_to_polling_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)

            def boom(**kwargs):
                raise RuntimeError("venue down")

            broker.place_protective_stop = boom  # type: ignore[method-assign]
            run_manager(queue, broker, price=100.0)  # must not raise

            self.assertIsNone(the_buy(queue).protective_stop_id)


class SingleSellerTests(unittest.TestCase):
    """The core invariant: never two live sellers for the same shares."""

    def test_an_engaged_venue_stop_suspends_the_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)
            run_manager(queue, broker, price=100.0)  # arm
            stop_id = the_buy(queue).protective_stop_id

            state = broker._read_state()
            state["protective_stops"][stop_id]["status"] = "ORDERED"
            broker._write_state(state)

            run_manager(queue, broker, price=111.0)  # would normally scale out
            self.assertEqual(sells(queue), [])

    def test_unknown_venue_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)

            def boom(stop_id):
                raise RuntimeError("status unavailable")

            broker.protective_stop_status = boom  # type: ignore[method-assign]
            self.assertTrue(stop_engaged(broker, the_buy(queue), lambda m: None))

    def test_bot_exit_releases_the_venue_stop_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)
            run_manager(queue, broker, price=100.0)
            stop_id = the_buy(queue).protective_stop_id

            run_manager(queue, broker, price=111.0)  # target-1 scale-out

            buy = the_buy(queue)
            self.assertEqual(len(sells(queue)), 1)
            self.assertEqual(sells(queue)[0].request.quantity, 5)
            self.assertIsNone(broker.protective_stop_status(stop_id))
            self.assertIsNone(buy.protective_stop_id)

    def test_a_failed_release_aborts_the_bot_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)
            run_manager(queue, broker, price=100.0)

            def boom(stop_id):
                raise RuntimeError("cancel refused")

            broker.cancel_protective_stop = boom  # type: ignore[method-assign]
            run_manager(queue, broker, price=111.0)

            # No sell may be queued while the venue stop is still live.
            self.assertEqual(sells(queue), [])
            self.assertIsNotNone(the_buy(queue).protective_stop_id)

    def test_external_close_retires_the_venue_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=100.0)
            stop_id = the_buy(queue).protective_stop_id

            # User liquidated through the broker UI.
            broker.get_positions = lambda market: []  # type: ignore[method-assign]
            run_manager(queue, broker, price=92.0)  # stop level → triggers the path

            self.assertIsNone(broker.protective_stop_status(stop_id))


class ReleaseTests(unittest.TestCase):
    def test_release_is_a_no_op_without_an_armed_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            buy, ok = release_protective_stop(queue, broker, the_buy(queue), lambda m: None)
            self.assertTrue(ok)
            self.assertIsNone(buy.protective_stop_id)

    def test_sync_clears_a_stale_stop_when_nothing_is_left_to_protect(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            armed = the_buy(queue)
            self.assertIsNotNone(armed.protective_stop_id)

            cleared = sync_protective_stop(
                queue, broker, armed, quantity=0, scaled_out=False,
                say=lambda m: None, enabled=True,
            )
            self.assertIsNone(cleared.protective_stop_id)


if __name__ == "__main__":
    unittest.main()
