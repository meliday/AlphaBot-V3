"""Tick → intraday bar aggregation (pure, in-memory).

Feeds the Phase-3b intraday strategies (ORB, VWAP) and any consumer that
wants minute bars out of the raw KIS tick stream. No I/O, no threads —
fully deterministic and unit-testable. Thread-safety is the caller's job
(the live loop feeds it from a single stream thread).

Semantics:
  * A bar covers ``[start, start + interval)``; ``start`` is the tick
    timestamp floored to the interval.
  * A bar is *completed* (and returned) when the first tick of a LATER
    interval arrives. Gaps produce no synthetic empty bars — the next bar
    simply starts at the next traded interval, matching how KRX minute
    charts render illiquid names.
  * Ticks older than the current bar's start are dropped (out-of-order
    wire noise must not rewrite history).
  * ``session_vwap`` accumulates Σ(price×volume)/Σvolume over everything
    fed since the last ``reset_session`` — the classic day-trading VWAP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alpha_bot.data.stream import Tick


@dataclass(frozen=True)
class IntradayBar:
    ticker: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float        # session VWAP as of this bar's close (not bar-local)
    tick_count: int


@dataclass
class _Building:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    tick_count: int = 0


@dataclass
class _SessionAccumulator:
    turnover: float = 0.0  # Σ price × volume
    volume: int = 0        # Σ volume

    def vwap(self, fallback: float) -> float:
        return self.turnover / self.volume if self.volume > 0 else fallback


class BarAggregator:
    """Aggregate ticks into fixed-interval bars per ticker."""

    def __init__(self, interval_seconds: int = 60, history: int = 500):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval = timedelta(seconds=interval_seconds)
        self.history = history
        self._building: dict[str, _Building] = {}
        self._bars: dict[str, list[IntradayBar]] = {}
        self._session: dict[str, _SessionAccumulator] = {}

    def _floor(self, ts: datetime) -> datetime:
        seconds = int(self.interval.total_seconds())
        epoch_like = ts.hour * 3600 + ts.minute * 60 + ts.second
        floored = (epoch_like // seconds) * seconds
        return ts.replace(hour=floored // 3600, minute=(floored % 3600) // 60,
                          second=floored % 60, microsecond=0)

    def on_tick(self, tick: Tick) -> IntradayBar | None:
        """Feed one tick; returns the bar that just COMPLETED, if any."""
        session = self._session.setdefault(tick.ticker, _SessionAccumulator())
        if tick.volume > 0:
            session.turnover += tick.price * tick.volume
            session.volume += tick.volume

        bar_start = self._floor(tick.time)
        building = self._building.get(tick.ticker)

        if building is None:
            self._building[tick.ticker] = _Building(
                start=bar_start, open=tick.price, high=tick.price,
                low=tick.price, close=tick.price,
                volume=max(tick.volume, 0), tick_count=1,
            )
            return None

        if bar_start < building.start:
            return None  # stale / out-of-order tick — never rewrite history

        if bar_start == building.start:
            building.high = max(building.high, tick.price)
            building.low = min(building.low, tick.price)
            building.close = tick.price
            building.volume += max(tick.volume, 0)
            building.tick_count += 1
            return None

        # Tick opened a later interval → the building bar is complete.
        completed = self._finalize(tick.ticker, building)
        self._building[tick.ticker] = _Building(
            start=bar_start, open=tick.price, high=tick.price,
            low=tick.price, close=tick.price,
            volume=max(tick.volume, 0), tick_count=1,
        )
        return completed

    def _finalize(self, ticker: str, building: _Building) -> IntradayBar:
        session = self._session.setdefault(ticker, _SessionAccumulator())
        bar = IntradayBar(
            ticker=ticker,
            start=building.start,
            open=building.open,
            high=building.high,
            low=building.low,
            close=building.close,
            volume=building.volume,
            vwap=round(session.vwap(fallback=building.close), 6),
            tick_count=building.tick_count,
        )
        store = self._bars.setdefault(ticker, [])
        store.append(bar)
        if len(store) > self.history:
            del store[: len(store) - self.history]
        return bar

    def force_close(self, ticker: str | None = None) -> list[IntradayBar]:
        """Flush in-progress bars (session end / shutdown)."""
        tickers = [ticker] if ticker else list(self._building)
        flushed: list[IntradayBar] = []
        for name in tickers:
            building = self._building.pop(name, None)
            if building is not None:
                flushed.append(self._finalize(name, building))
        return flushed

    def bars(self, ticker: str) -> list[IntradayBar]:
        return list(self._bars.get(ticker, []))

    def session_vwap(self, ticker: str) -> float | None:
        session = self._session.get(ticker)
        if session is None or session.volume <= 0:
            return None
        return session.vwap(fallback=0.0)

    def reset_session(self, ticker: str | None = None) -> None:
        """Clear VWAP accumulation and bar history (new trading day)."""
        if ticker is None:
            self._building.clear()
            self._bars.clear()
            self._session.clear()
        else:
            self._building.pop(ticker, None)
            self._bars.pop(ticker, None)
            self._session.pop(ticker, None)
