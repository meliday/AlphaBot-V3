import unittest
from datetime import date, timedelta

from alpha_bot.backtest import Backtester
from alpha_bot.data import SyntheticDataProvider
from alpha_bot.models import (
    AnalysisReport,
    Candle,
    IndicatorSnapshot,
    MarketContext,
    Scoreboard,
    TechnicalBreakdown,
    TradePlan,
)


class BacktestTests(unittest.TestCase):
    def test_backtest_is_reproducible(self):
        provider = SyntheticDataProvider()
        candles = provider.get_candles("NVDA", "US", 320)
        result1 = Backtester().run(
            "NVDA",
            "US",
            candles,
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        result2 = Backtester().run(
            "NVDA",
            "US",
            candles,
            provider.get_fundamentals("NVDA", "US"),
            provider.get_catalysts("NVDA", "US"),
            provider.get_market_context("NVDA", "US"),
        )
        self.assertEqual(result1.trades, result2.trades)


# ── Exit-ladder simulation (parity with the live position manager) ────


def _fixed_report(stop: float, t1: float, t2: float) -> AnalysisReport:
    plan = TradePlan(
        entry_low=99.0, entry_high=101.0, stop_loss=stop, target1=t1, target2=t2,
        rr_ratio=2.0, entry_reference=100.0, stop_pct=-7.0,
        target1_pct=10.0, target2_pct=25.0,
    )
    return AnalysisReport(
        ticker="TEST", company_name="TEST", market="US", signal="Buy",
        reason="fixed", scoreboard=Scoreboard(8, "", 8, "", 8, ""),
        indicators=IndicatorSnapshot(
            close=100.0, sma50=95.0, sma200=90.0, rsi14=60.0,
            bollinger_mid=100.0, bollinger_upper=105.0, bollinger_lower=95.0,
            bollinger_width=0.1, resistance_60d=110.0, support_20d=95.0,
            volume_summary="", vcp_pattern="", vcp_score=5, vcp_details="",
        ),
        trade_plan=plan,
        technical=TechnicalBreakdown("", "", "", "", ""),
        earnings_caution=False, catalyst_summary="", as_of=date(2026, 1, 1),
    )


class FixedAnalyzer:
    """Always says Buy with a fixed trade plan — the candle series alone
    determines the exit path, so each ladder branch is testable."""

    def __init__(self, stop: float, t1: float, t2: float):
        self.report = _fixed_report(stop, t1, t2)

    def analyze(self, *args, **kwargs):
        return self.report


def _flat(day: date, price: float = 100.0) -> Candle:
    return Candle(day, price, price + 2, price - 2, price, 1_000_000)


def _series(bars: dict[int, Candle], length: int = 100) -> list[Candle]:
    """Flat 100-price series with specific bars overridden. Entry lands on
    bar 26 (index start = max(20, len//4) = 25 → entry at 25+1)."""
    start = date(2025, 1, 1)
    out = []
    for i in range(length):
        day = start + timedelta(days=i)
        out.append(bars.get(i) or _flat(day))
    return out


def _run(candles, stop=93.0, t1=110.0, t2=140.0, **kwargs):
    bt = Backtester(FixedAnalyzer(stop, t1, t2), commission_pct=0.0,
                    slippage_pct=0.0, **kwargs)
    return bt.run("TEST", "US", candles, [], [], MarketContext())


def _day(i: int) -> date:
    return date(2025, 1, 1) + timedelta(days=i)


class ExitLadderTests(unittest.TestCase):
    def test_gap_below_stop_exits_everything_at_open(self):
        candles = _series({27: Candle(_day(27), 85.0, 86.0, 80.0, 82.0, 1_000_000)})
        trade = _run(candles).trades[0]
        self.assertEqual(trade.outcome, "stop_gap")
        self.assertAlmostEqual(trade.exit, 85.0, places=2)

    def test_target1_scales_out_then_trail_stops_the_runner(self):
        candles = _series({
            # t1(110) touched; low stays above the freshly-armed trail.
            27: Candle(_day(27), 104.0, 111.0, 103.0, 108.0, 1_000_000),
            # Runner survives and the trail ratchets on the close.
            28: Candle(_day(28), 109.0, 112.0, 105.0, 111.0, 1_000_000),
            # Low pierces the trail → runner exits at the trail.
            29: Candle(_day(29), 110.0, 110.0, 101.0, 102.0, 1_000_000),
        })
        trade = _run(candles).trades[0]
        self.assertEqual(trade.outcome, "target1+trail")
        self.assertEqual(trade.exit_date, _day(29).isoformat())
        # Blended exit: half at 110, half at the trail (breakeven..t1 range).
        self.assertGreater(trade.exit, 100.0)
        self.assertLess(trade.exit, 110.0)
        self.assertGreater(trade.return_pct, 0.0)  # runner floored ≥ breakeven

    def test_target2_closes_the_runner(self):
        candles = _series({
            27: Candle(_day(27), 104.0, 111.0, 104.0, 108.0, 1_000_000),
            28: Candle(_day(28), 112.0, 145.0, 110.0, 138.0, 1_000_000),
        })
        trade = _run(candles).trades[0]
        self.assertEqual(trade.outcome, "target1+target2")
        self.assertAlmostEqual(trade.exit, (110.0 + 140.0) / 2, places=2)

    def test_single_stage_mode_preserves_old_behavior(self):
        candles = _series({
            27: Candle(_day(27), 104.0, 111.0, 103.0, 108.0, 1_000_000),
        })
        trade = _run(candles, split_exits=False).trades[0]
        self.assertEqual(trade.outcome, "target1")
        self.assertAlmostEqual(trade.exit, 110.0, places=2)

    def test_runner_expires_at_max_hold(self):
        bars = {27: Candle(_day(27), 104.0, 111.0, 104.0, 108.0, 1_000_000)}
        # Drift sideways at 108 with lows that never reach the trail
        # (≈ breakeven) and highs that never reach target-2 → time expiry.
        for i in range(28, 100):
            bars[i] = Candle(_day(i), 108.0, 110.0, 106.0, 108.0, 1_000_000)
        trade = _run(_series(bars)).trades[0]
        self.assertEqual(trade.outcome, "target1+time")
        # Half at t1 (110), half at the final close (108).
        self.assertAlmostEqual(trade.exit, 109.0, places=2)


if __name__ == "__main__":
    unittest.main()
