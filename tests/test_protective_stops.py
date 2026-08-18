"""Broker-side protective stop tests.

Two properties under test throughout:

* **At most one seller** — the venue stop and the polling ladder must never
  both be live against the same shares.
* **Resumable venue writes** — modify is never used (no idempotency key at
  Toss); every re-arm is cancel + idempotent create behind a write-ahead
  intent, so a lost response at any step is recovered by replaying the same
  deterministic key instead of arming a duplicate.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto import protective_stops as ps
from alpha_bot.auto.position_manager import manage_open_positions
from alpha_bot.auto.protective_stops import (
    effective_stop_price,
    release_protective_stop,
    resolve_pending_stop,
    stop_engaged,
    sync_protective_stop,
    warn_unreferenced_stops,
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
    broker = CountingBroker(Path(tmp) / "ledger.json", Path(tmp) / "state.json")
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
        self.cancels = 0
        self.status_calls = 0

    def place_protective_stop(self, **kwargs):
        self.places += 1
        return super().place_protective_stop(**kwargs)

    def cancel_protective_stop(self, stop_id):
        self.cancels += 1
        return super().cancel_protective_stop(stop_id)

    def protective_stop_status(self, stop_id):
        self.status_calls += 1
        return super().protective_stop_status(stop_id)

    def venue_stops(self) -> dict:
        return self._read_state().get("protective_stops", {})


class Base(unittest.TestCase):
    def setUp(self):
        ps.reset_status_cache()
        self.addCleanup(ps.reset_status_cache)


class CapabilityTests(Base):
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


class EffectiveStopTests(Base):
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


class ArmingTests(Base):
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
            queue, broker = make_filled_buy(tmp)
            for _ in range(4):
                run_manager(queue, broker, price=100.0)
            self.assertEqual(broker.places, 1)
            self.assertEqual(broker.cancels, 0)

    def test_a_dead_venue_stop_is_re_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            first_id = the_buy(queue).protective_stop_id

            # Simulate expiry / manual cancellation at the venue.
            broker.cancel_protective_stop(first_id)
            ps.reset_status_cache()
            run_manager(queue, broker, price=100.0)

            buy = the_buy(queue)
            self.assertIsNotNone(buy.protective_stop_id)
            self.assertNotEqual(buy.protective_stop_id, first_id)

    def test_a_venue_failure_degrades_to_polling_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)

            def boom(**kwargs):
                raise RuntimeError("venue down")

            broker.place_protective_stop = boom  # type: ignore[method-assign]
            run_manager(queue, broker, price=100.0)  # must not raise
            self.assertIsNone(the_buy(queue).protective_stop_id)

    def test_a_venue_outage_never_freezes_position_management(self):
        # Intent stuck unresolved (venue down) — evaluation must continue and
        # a bot stop-loss exit must still be *attempted*; only the sell is
        # gated, and release aborts it. Holding logic keeps running.
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)

            def boom(**kwargs):
                raise RuntimeError("venue down")

            broker.place_protective_stop = boom  # type: ignore[method-assign]
            run_manager(queue, broker, price=100.0)   # arm attempt fails → intent
            self.assertIsNone(the_buy(queue).protective_stop_id)

            run_manager(queue, broker, price=111.0)   # target-1 would fire
            # Sell aborted (a stop may exist at the venue) — but no crash,
            # and the loop is still evaluating.
            self.assertEqual(sells(queue), [])


class ReArmTests(Base):
    """Re-arm = cancel + idempotent create; never the modify endpoint."""

    def _armed(self, tmp, stop=93.0):
        queue, broker = make_filled_buy(tmp, qty=10, stop=stop)
        run_manager(queue, broker, price=100.0)
        return queue, broker

    def test_a_stop_change_cancels_then_creates(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._armed(tmp)
            old_id = the_buy(queue).protective_stop_id

            moved = replace(the_buy(queue), stop_loss=95.0)
            queue.update(moved)
            updated = sync_protective_stop(
                queue, broker, moved, quantity=10, scaled_out=False,
                say=lambda m: None, enabled=True,
            )

            self.assertEqual(broker.cancels, 1)
            self.assertEqual(broker.places, 2)
            self.assertNotEqual(updated.protective_stop_id, old_id)
            stops = broker.venue_stops()
            self.assertEqual(len(stops), 1)  # exactly one live stop
            self.assertEqual(
                stops[updated.protective_stop_id]["stop_price"], 95.0
            )

    def test_drift_inside_the_tolerance_is_not_re_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._armed(tmp, stop=93.0)
            buy = the_buy(queue)

            moved = replace(buy, stop_loss=93.2)  # 0.2 < tolerance 0.5
            queue.update(moved)
            sync_protective_stop(
                queue, broker, moved, quantity=10, scaled_out=False,
                say=lambda m: None, enabled=True, amend_tolerance=0.5,
            )
            self.assertEqual(broker.cancels, 0)
            self.assertEqual(broker.places, 1)

    def test_a_quantity_change_re_arms_regardless_of_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._armed(tmp, stop=93.0)
            sync_protective_stop(
                queue, broker, the_buy(queue), quantity=7, scaled_out=False,
                say=lambda m: None, enabled=True, amend_tolerance=10.0,
            )
            self.assertEqual(broker.cancels, 1)
            self.assertEqual(broker.places, 2)
            self.assertEqual(the_buy(queue).protective_stop_quantity, 7)

    def test_a_failed_cancel_keeps_the_old_stop_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._armed(tmp, stop=93.0)
            old = the_buy(queue)

            def refuse(stop_id):
                raise RuntimeError("cancel refused")

            broker.cancel_protective_stop = refuse  # type: ignore[method-assign]
            moved = replace(old, stop_loss=95.0)
            queue.update(moved)
            sync_protective_stop(
                queue, broker, moved, quantity=10, scaled_out=False,
                say=lambda m: None, enabled=True,
            )
            after = the_buy(queue)
            # Old protection intact, no create attempted on top of it.
            self.assertEqual(after.protective_stop_id, old.protective_stop_id)
            self.assertEqual(after.protective_stop_price, 93.0)
            self.assertEqual(broker.places, 1)


class WriteAheadReplayTests(Base):
    """A create whose response is lost is recovered, never duplicated."""

    class LostResponseBroker(CountingBroker):
        """First create lands at the venue but the response is 'lost'."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.drop_next_response = False

        def place_protective_stop(self, **kwargs):
            stop_id = super().place_protective_stop(**kwargs)
            if self.drop_next_response:
                self.drop_next_response = False
                raise ConnectionError("response lost after the venue accepted")
            return stop_id

    def test_lost_create_response_is_replayed_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = self.LostResponseBroker(Path(tmp) / "l.json", Path(tmp) / "s.json")
            cand = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 10, "limit", 100.0),
                stop_loss=93.0, target1=110.0, target2=125.0, analysis_signal="Buy",
            )
            queue.approve(cand.id, broker)
            queue.sync_with_broker(broker)

            broker.drop_next_response = True
            run_manager(queue, broker, price=100.0)

            interim = the_buy(queue)
            self.assertIsNone(interim.protective_stop_id)     # id never arrived
            self.assertEqual(interim.protective_stop_price, 93.0)  # intent on disk
            self.assertEqual(len(broker.venue_stops()), 1)    # but the stop exists

            run_manager(queue, broker, price=100.0)           # replay same key

            resolved = the_buy(queue)
            self.assertIsNotNone(resolved.protective_stop_id)
            self.assertEqual(len(broker.venue_stops()), 1)    # still exactly one
            self.assertIn(resolved.protective_stop_id, broker.venue_stops())

    def test_release_resolves_the_pending_intent_before_cancelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            broker = self.LostResponseBroker(Path(tmp) / "l.json", Path(tmp) / "s.json")
            cand = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 10, "limit", 100.0),
                stop_loss=93.0, target1=110.0, target2=125.0, analysis_signal="Buy",
            )
            queue.approve(cand.id, broker)
            queue.sync_with_broker(broker)

            broker.drop_next_response = True
            run_manager(queue, broker, price=100.0)           # intent stuck
            self.assertEqual(len(broker.venue_stops()), 1)

            run_manager(queue, broker, price=92.0)            # stop-loss exit

            # Exit went through, and the venue stop was recovered *and* released.
            self.assertEqual(len(sells(queue)), 1)
            self.assertEqual(broker.venue_stops(), {})


class SingleSellerTests(Base):
    """The core invariant: never two live sellers for the same shares."""

    def test_an_engaged_venue_stop_suspends_the_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)
            run_manager(queue, broker, price=100.0)  # arm
            stop_id = the_buy(queue).protective_stop_id

            state = broker._read_state()
            state["protective_stops"][stop_id]["status"] = "ORDERED"
            broker._write_state(state)
            ps.reset_status_cache()

            run_manager(queue, broker, price=111.0)  # would normally scale out
            self.assertEqual(sells(queue), [])

    def test_unknown_venue_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)

            def boom(stop_id):
                raise RuntimeError("status unavailable")

            broker.protective_stop_status = boom  # type: ignore[method-assign]
            ps.reset_status_cache()
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

            self.assertEqual(sells(queue), [])
            self.assertIsNotNone(the_buy(queue).protective_stop_id)

    def test_external_close_retires_the_venue_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp, qty=10)
            run_manager(queue, broker, price=100.0)
            stop_id = the_buy(queue).protective_stop_id

            broker.get_positions = lambda market: []  # user sold via the app
            run_manager(queue, broker, price=92.0)

            self.assertIsNone(broker.protective_stop_status(stop_id))


class StatusCacheTests(Base):
    def test_repeat_lookups_inside_the_ttl_hit_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            buy = the_buy(queue)

            broker.status_calls = 0
            stop_engaged(broker, buy, lambda m: None)
            stop_engaged(broker, buy, lambda m: None)
            stop_engaged(broker, buy, lambda m: None)
            self.assertEqual(broker.status_calls, 1)

    def test_arming_invalidates_the_cached_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            buy = the_buy(queue)
            stop_engaged(broker, buy, lambda m: None)  # populate cache

            state = broker._read_state()
            state["protective_stops"][buy.protective_stop_id]["status"] = "ORDERED"
            broker._write_state(state)

            # Within the TTL the stale WATCHING answer is served — that is the
            # accepted trade-off — but a reset (as any place/cancel performs)
            # must surface the new state immediately.
            self.assertFalse(stop_engaged(broker, buy, lambda m: None))
            ps.reset_status_cache()
            self.assertTrue(stop_engaged(broker, buy, lambda m: None))


class RatchetStepTests(Base):
    def _scaled_out(self, tmp):
        queue, broker = make_filled_buy(tmp, qty=10, t1=110.0)
        run_manager(queue, broker, price=111.0)   # scale out, trail arms
        queue.sync_with_broker(broker)            # settle the partial fill
        return queue, broker

    def test_a_tiny_rise_does_not_move_the_trail(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._scaled_out(tmp)
            before = the_buy(queue).trail_stop
            # ATR ≈ 2.22 at 111 → min step ≈ 0.22; a 0.05 rise is noise.
            run_manager(queue, broker, price=111.05)
            self.assertEqual(the_buy(queue).trail_stop, before)

    def test_a_real_rise_still_ratchets(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = self._scaled_out(tmp)
            before = the_buy(queue).trail_stop
            run_manager(queue, broker, price=114.0)
            self.assertGreater(the_buy(queue).trail_stop, before)


class OrphanSweepTests(Base):
    def test_an_untracked_venue_stop_is_reported_but_never_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)  # legitimate armed stop

            orphan_id = broker.place_protective_stop(
                ticker="NVDA", market="US", quantity=3,
                stop_price=90.0, client_order_id="manual-or-orphan",
            )
            messages: list[str] = []
            with patch("alpha_bot.notify.notify", lambda *a, **k: False):
                unknown = warn_unreferenced_stops(queue, broker, messages.append)

            self.assertEqual(unknown, [orphan_id])
            self.assertTrue(any(orphan_id in m for m in messages))
            self.assertIn(orphan_id, broker.venue_stops())  # untouched

    def test_a_fully_tracked_venue_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, broker = make_filled_buy(tmp)
            run_manager(queue, broker, price=100.0)
            messages: list[str] = []
            self.assertEqual(warn_unreferenced_stops(queue, broker, messages.append), [])
            self.assertEqual(messages, [])


class ReleaseTests(Base):
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
