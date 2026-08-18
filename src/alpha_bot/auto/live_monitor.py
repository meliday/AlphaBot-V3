"""Phase 3a — real-time exit monitoring for held positions.

The 5-minute auto-pilot loop evaluates stops/targets against a price that
can be minutes old; an intraday flush can blow far through a planned −7%
stop before the next sweep. This module tightens that loop to seconds
WITHOUT introducing a second exit engine:

  * ``TickPriceCache`` holds the latest websocket tick per ticker.
  * ``StreamPricedProvider`` wraps the normal ``DataProvider`` so
    ``get_current_price`` serves the cached tick first (REST quote / last
    close as fallback) and ``get_candles`` is TTL-cached — the exit engine
    needs daily candles only for ATR/trail math, not fresh every pass.
  * ``LiveExitMonitor`` keeps websocket subscriptions in sync with the
    positions actually held and, whenever fresh ticks arrive, runs the
    battle-tested ``manage_open_positions`` — the SAME code path the
    auto-pilot uses (broker-qty verification, scale-outs, trail ratchet,
    external-close reconciliation, Telegram alerts and all).

The kill switch never gates this module: protecting held positions is
exactly what must keep running during an emergency stop. New buys are
someone else's job.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from alpha_bot.approval import ApprovalQueue
from alpha_bot.approval.queue import order_belongs_to_broker
from alpha_bot.auto.position_manager import manage_open_positions, remaining_quantity
from alpha_bot.broker.base import Broker
from alpha_bot.data import DataProvider
from alpha_bot.data.stream import Tick
from alpha_bot.market_hours import market_status
from alpha_bot.models import Candle, Market

logger = logging.getLogger(__name__)


class TickPriceCache:
    """Latest tick price per ticker, with a freshness generation counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {}
        self._generation = 0

    def update(self, tick: Tick) -> None:
        with self._lock:
            self._prices[tick.ticker] = tick.price
            self._generation += 1

    def price(self, ticker: str) -> float | None:
        with self._lock:
            return self._prices.get(ticker)

    @property
    def generation(self) -> int:
        """Monotonic counter — lets pollers ask "anything new since N?"."""
        with self._lock:
            return self._generation


class StreamPricedProvider:
    """DataProvider wrapper: tick prices first, TTL-cached daily candles.

    The exit engine calls ``get_current_price`` for trigger checks and
    ``get_candles`` for ATR/trail math. At a seconds-level cadence the
    candles must not be re-fetched every pass (KIS paper throttles at
    ~2 req/s), so they're cached for ``candle_ttl`` seconds; ticks are
    always served fresh from the stream cache.
    """

    def __init__(
        self,
        base: DataProvider,
        prices: TickPriceCache,
        candle_ttl: float = 300.0,
    ):
        self._base = base
        self._prices = prices
        self._candle_ttl = candle_ttl
        self._candles: dict[tuple[str, str, int], tuple[float, list[Candle]]] = {}
        self._lock = threading.Lock()

    def get_candles(self, ticker: str, market: Market, lookback: int = 260) -> list[Candle]:
        key = (ticker, market, lookback)
        now = time.monotonic()
        with self._lock:
            hit = self._candles.get(key)
            if hit and (now - hit[0]) < self._candle_ttl:
                return hit[1]
        candles = self._base.get_candles(ticker, market, lookback)
        with self._lock:
            self._candles[key] = (now, candles)
        return candles

    def get_current_price(self, ticker: str, market: Market) -> float | None:
        live = self._prices.price(ticker)
        if live is not None and live > 0:
            return live
        if hasattr(self._base, "get_current_price"):
            try:
                return self._base.get_current_price(ticker, market)
            except Exception as exc:
                logger.debug("Fallback quote failed for %s:%s: %s", market, ticker, exc)
        return None

    # Pass-throughs the exit engine doesn't use but the protocol defines.
    def get_fundamentals(self, ticker: str, market: Market):
        return self._base.get_fundamentals(ticker, market)

    def get_catalysts(self, ticker: str, market: Market):
        return self._base.get_catalysts(ticker, market)

    def get_market_context(self, ticker: str, market: Market):
        return self._base.get_market_context(ticker, market)


def held_kr_tickers(queue: ApprovalQueue, broker: Broker | None = None) -> set[str]:
    """KR tickers with a live remaining position (stream-subscribable)."""
    return held_tickers_by_market(queue, broker).get("KR", set())


def held_tickers_by_market(
    queue: ApprovalQueue, broker: Broker | None = None
) -> dict[Market, set[str]]:
    """Live queue positions grouped by market, scoped to one broker account."""

    orders = queue.list_orders()
    by_id = {o.id: o for o in orders}
    held: dict[Market, set[str]] = {}
    for buy in orders:
        if buy.request.side != "buy":
            continue
        if broker is not None and not order_belongs_to_broker(buy, broker):
            continue
        if buy.status not in {"filled", "partially_filled", "partially_filled_cancelled"}:
            continue
        if (buy.filled_quantity or 0) <= 0 or buy.avg_fill_price is None:
            continue
        exit_order = by_id.get(buy.exit_order_id or "")
        if exit_order and exit_order.status in {
            "pending", "submitting", "unknown", "submitted", "partially_filled", "filled",
        }:
            continue
        qty, _inflight = remaining_quantity(buy, by_id)
        if qty > 0:
            held.setdefault(buy.request.market, set()).add(buy.request.ticker)
    return held


class LiveExitMonitor:
    """Tick-driven wrapper around the standard exit engine.

    ``run_forever`` wiring (stream client, sleep loop, Ctrl-C) lives in the
    CLI; this class owns the decision cadence so it can be driven directly
    by tests with synthetic ticks and a mock broker.
    """

    def __init__(
        self,
        queue: ApprovalQueue,
        broker: Broker,
        provider: DataProvider,
        *,
        candle_ttl: float = 300.0,
        say: Callable[[str], None] = print,
        protective_stops: bool = False,
    ):
        self.queue = queue
        self.broker = broker
        self.protective_stops = protective_stops
        self.prices = TickPriceCache()
        self.provider = StreamPricedProvider(provider, self.prices, candle_ttl=candle_ttl)
        self.say = say
        self._last_generation = -1
        self._last_evaluation = 0.0

    # Stream callback — hand this to KisStreamClient(on_tick=...).
    def on_tick(self, tick: Tick) -> None:
        self.prices.update(tick)

    def sync_subscriptions(self, stream) -> set[str]:
        """Subscribe held KR tickers / unsubscribe closed ones. Returns the
        current watch set."""
        watch = held_kr_tickers(self.queue, self.broker)
        current = stream.desired()
        for ticker in watch - current:
            stream.subscribe(ticker)
        for ticker in current - watch:
            stream.unsubscribe(ticker)
        return watch

    def evaluate_if_fresh(self) -> bool:
        """Run one exit pass if new ticks arrived since the last pass.

        Returns True when a pass ran. The pass is the standard
        ``manage_open_positions`` — market-hours gating, broker
        verification, scale-out/trail logic and notifications included.
        """
        generation = self.prices.generation
        if generation == self._last_generation:
            return False
        self._last_generation = generation
        self._last_evaluation = time.monotonic()
        manage_open_positions(
            self.queue, self.broker, self.provider, self.say,
            protective_stops=self.protective_stops,
        )
        return True

    def evaluate_if_due(self, rest_poll_interval: float = 15.0) -> bool:
        """Evaluate on fresh KR ticks or periodic REST fallback for any market."""

        held = held_tickers_by_market(self.queue, self.broker)
        if not held or not any(self.market_open(market) for market in held):
            return False
        now = time.monotonic()
        fresh_tick = self.prices.generation != self._last_generation
        rest_due = (now - self._last_evaluation) >= max(1.0, rest_poll_interval)
        if not fresh_tick and not rest_due:
            return False
        self._last_generation = self.prices.generation
        self._last_evaluation = now
        manage_open_positions(
            self.queue, self.broker, self.provider, self.say,
            protective_stops=self.protective_stops,
        )
        return True

    def market_open(self, market: Market = "KR") -> bool:
        return market_status(market).is_open
