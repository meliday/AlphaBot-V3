"""Breakout confirmation gate — pivot break + volume + extension guard."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from alpha_bot.models import Candle
from alpha_bot.strategy import StrategyAnalyzer, StrategyParams
from alpha_bot.strategy.indicators import breakout_status
from tests.test_market_scenarios import (
    _SEGS_A1,
    _make_candles,
    _strong_fundamentals,
)
from alpha_bot.models import MarketContext


def _base_series(
    last_close: float,
    last_volume: int = 1_000_000,
    bars: int = 70,
    base_high: float = 102.0,
) -> list[Candle]:
    """Flat base (close 100, high `base_high`) with a configurable final bar.

    Pivot = max high of bars [-65:-5] = base_high.
    """
    out: list[Candle] = []
    day = date(2025, 1, 1)
    for _ in range(bars - 1):
        day += timedelta(days=1)
        out.append(Candle(day, 100.0, base_high, 98.0, 100.0, 1_000_000))
    day += timedelta(days=1)
    out.append(
        Candle(day, 100.0, max(last_close, 100.0) + 0.5, 99.0, last_close, last_volume)
    )
    return out


class BreakoutStatusTests(unittest.TestCase):
    def test_confirmed_on_fresh_volume_backed_break(self):
        candles = _base_series(last_close=103.0, last_volume=2_000_000)
        status, detail = breakout_status(candles)
        self.assertEqual(status, "confirmed", detail)

    def test_still_basing_below_pivot(self):
        candles = _base_series(last_close=101.0)
        status, _ = breakout_status(candles)
        self.assertEqual(status, "no_breakout")

    def test_extended_past_pivot_is_rejected(self):
        candles = _base_series(last_close=112.0, last_volume=2_000_000)  # +9.8% > 5%
        status, _ = breakout_status(candles)
        self.assertEqual(status, "extended")

    def test_quiet_volume_break_is_rejected(self):
        candles = _base_series(last_close=103.0, last_volume=1_000_000)  # 1.0× avg
        status, _ = breakout_status(candles)
        self.assertEqual(status, "low_volume")

    def test_insufficient_history_passes_through(self):
        candles = _base_series(last_close=103.0)[-30:]
        status, _ = breakout_status(candles)
        self.assertEqual(status, "insufficient")


class BreakoutGateIntegrationTests(unittest.TestCase):
    """The gate is opt-in: default OFF keeps the pre-pivot squeeze style
    (scenario A1 = Buy); ON demands a confirmed break first."""

    @classmethod
    def setUpClass(cls):
        cls.candles = _make_candles(_SEGS_A1)
        cls.fundamentals = _strong_fundamentals()

    def _analyze(self, require: bool):
        params = StrategyParams(require_breakout_confirmation=require)
        return StrategyAnalyzer(params=params).analyze(
            "TEST", "US", self.candles, self.fundamentals, [], MarketContext(),
            use_live_market_data=False,
        )

    def test_gate_off_keeps_squeeze_buy(self):
        self.assertEqual(self._analyze(require=False).signal, "Buy")

    def test_gate_on_waits_for_the_pivot_break(self):
        report = self._analyze(require=True)
        self.assertEqual(report.signal, "Wait")
        self.assertIn("돌파 확인 게이트", report.reason)


if __name__ == "__main__":
    unittest.main()
