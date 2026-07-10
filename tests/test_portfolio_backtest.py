"""Portfolio backtest engine — shared cash, max_positions, risk sizing."""

from __future__ import annotations

import unittest

from alpha_bot.models import Candle, Market
from alpha_bot.portfolio_backtest import PortfolioBacktester, TickerSeries
from tests.test_backtest import FixedAnalyzer, _day, _series


def _engine(analyzer, **overrides):
    kwargs = dict(
        starting_cash=10_000.0,
        max_positions=5,
        risk_per_trade_pct=1.0,
        max_position_pct=20.0,
        commission_pct=0.0,
        slippage_pct=0.0,
    )
    kwargs.update(overrides)
    return PortfolioBacktester(analyzer, **kwargs)


def _ts(ticker: str, bars: dict[int, Candle] | None = None, market: Market = "US") -> TickerSeries:
    return TickerSeries(ticker=ticker, market=market, candles=_series(bars or {}))


class PortfolioConstraintTests(unittest.TestCase):
    def test_max_positions_caps_concurrency(self):
        # Three identical always-Buy tickers, two slots: the third only gets
        # in after a slot frees at max-hold expiry.
        universe = [_ts("T0"), _ts("T1"), _ts("T2")]
        result = _engine(FixedAnalyzer(93.0, 110.0, 140.0), max_positions=2).run(universe)
        self.assertEqual(result.max_concurrent_positions, 2)
        self.assertGreater(result.skipped_entries, 0)
        first_two = {t.ticker for t in result.trades[:2]}
        self.assertEqual(first_two, {"T0", "T1"})
        self.assertIn("T2", {t.ticker for t in result.trades})

    def test_risk_based_share_sizing(self):
        # equity 10_000 × 1% risk = 100 risk capital; per-share risk
        # 100 − 93 = 7 → 14 shares (cash cap 100, budget cap 20 don't bind).
        result = _engine(FixedAnalyzer(93.0, 110.0, 140.0)).run([_ts("T0")])
        self.assertGreater(len(result.trades), 0)
        self.assertEqual(result.trades[0].shares, 14)

    def test_position_budget_cap_binds(self):
        # 50% risk would ask for 714 shares (=71k), cash caps at 100,
        # but the 20% position budget caps at 20 shares first.
        result = _engine(
            FixedAnalyzer(93.0, 110.0, 140.0), risk_per_trade_pct=50.0
        ).run([_ts("T0")])
        self.assertEqual(result.trades[0].shares, 20)

    def test_too_little_cash_skips_entry(self):
        # 500 × 1% = 5 risk capital < 7 per-share risk → 0 shares → skip.
        result = _engine(
            FixedAnalyzer(93.0, 110.0, 140.0), starting_cash=500.0
        ).run([_ts("T0")])
        self.assertEqual(result.trades, [])
        self.assertGreater(result.skipped_entries, 0)
        self.assertAlmostEqual(result.ending_equity, 500.0, places=4)

    def test_single_market_enforced(self):
        with self.assertRaises(ValueError):
            _engine(FixedAnalyzer(93.0, 110.0, 140.0)).run(
                [_ts("T0", market="US"), _ts("K0", market="KR")]
            )


class PortfolioAccountingTests(unittest.TestCase):
    def _run(self, bars: dict[int, Candle], **overrides):
        return _engine(FixedAnalyzer(93.0, 110.0, 140.0), **overrides).run(
            [_ts("T0", bars)]
        )

    def test_equity_equals_cash_plus_realized_pnl(self):
        # Gap through the stop at bar 30 → realized loss; every later
        # re-entry stays flat. Ending equity must equal starting + Σ pnl.
        bars = {30: Candle(_day(30), 85.0, 86.0, 80.0, 82.0, 1_000_000)}
        result = self._run(bars)
        self.assertEqual(result.trades[0].outcome, "stop_gap")
        self.assertEqual(result.trades[0].shares, 14)
        self.assertAlmostEqual(result.trades[0].pnl, (85.0 - 100.0) * 14, places=4)
        total_pnl = sum(t.pnl for t in result.trades)
        self.assertAlmostEqual(result.ending_equity, 10_000.0 + total_pnl, places=4)
        self.assertAlmostEqual(result.equity_curve[-1][1], result.ending_equity, places=4)

    def test_scale_out_sells_larger_half_and_trails_runner(self):
        # Same shape as the single-ticker ladder test: t1 touch at bar 27,
        # ratchet at 28, trail breach at 29.
        bars = {
            27: Candle(_day(27), 104.0, 111.0, 103.0, 108.0, 1_000_000),
            28: Candle(_day(28), 109.0, 112.0, 105.0, 111.0, 1_000_000),
            29: Candle(_day(29), 110.0, 110.0, 101.0, 102.0, 1_000_000),
        }
        result = self._run(bars)
        trade = result.trades[0]
        self.assertEqual(trade.outcome, "target1+trail")
        self.assertEqual(trade.shares, 14)  # 7 sold at t1, 7 at the trail
        self.assertGreater(trade.pnl, 0.0)  # runner floored at breakeven
        total_pnl = sum(t.pnl for t in result.trades)
        self.assertAlmostEqual(result.ending_equity, 10_000.0 + total_pnl, places=4)

    def test_flat_universe_round_trips_to_par(self):
        # No triggers, zero costs: every time-exit closes at entry price,
        # so the account must end exactly where it started.
        result = self._run({})
        self.assertGreater(len(result.trades), 0)
        self.assertTrue(all(t.outcome == "time" for t in result.trades))
        self.assertAlmostEqual(result.ending_equity, 10_000.0, places=4)


if __name__ == "__main__":
    unittest.main()
