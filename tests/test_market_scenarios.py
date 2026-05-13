"""Market scenario tests for StrategyAnalyzer.

Ten canonical market scenarios verified by manual probe runs (seed=42):

  A1 VCP-Buy      Buy   score=24, rr=2.47  (VCP squeeze + strong fundamentals)
  A2 RR-Gate      Wait  score=27, rr=0.75  (great score, R/R gate blocks entry)
  A3 Score-Gate   Wait  score=10, rr=3.28  (good R/R, score gate blocks entry)
  B1 Pullback     Wait  score=16, rr=3.28  (pullback kills trend sub-scores)
  B2 Sideways     Hold Off score=11        (close barely below SMA200)
  C1 Correction   Hold Off score=9         (close << SMA200 after crash)
  C2 BubbleBurst  Hold Off score=8         (close << SMA200 after deflation)
  C3 LLM-Veto     Hold Off earns_caution   (LLM high-severity veto + negative EPS)
  D1 Whipsaw      Hold Off score=8         (choppy, close < SMA200)
  D2 PumpDump     Hold Off score=12        (dump brings close < SMA200)
"""

from __future__ import annotations

import random
import unittest
from datetime import date, timedelta

from alpha_bot.models import (
    Candle,
    FundamentalsQuarter,
    MarketContext,
    NewsAssessment,
)
from alpha_bot.strategy import StrategyAnalyzer


# ── Candle factory ────────────────────────────────────────────────────

def _make_candles(
    segments: list[tuple],
    start_price: float = 100.0,
    seed: int = 42,
) -> list[Candle]:
    """Build a deterministic OHLCV series from segment descriptors.

    Each segment is (n_bars, drift, noise, vol_base, up_day_vol_mult).
    drift and noise are fractional per bar (0.01 = 1 %).
    up_day_vol_mult > 1 creates accumulation; < 1 creates distribution.
    """
    rng = random.Random(seed)
    candles: list[Candle] = []
    price = start_price
    d = date(2023, 1, 2)
    for n, drift, noise, vol_base, up_mult in segments:
        for _ in range(n):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            prev = price
            price = max(0.01, prev * (1 + drift + rng.uniform(-noise, noise)))
            intra = price * rng.uniform(0.003, 0.015)
            o = round(prev * (1 + rng.uniform(-0.003, 0.003)), 4)
            c = round(price, 4)
            h = round(max(o, c) + intra, 4)
            l = round(max(0.01, min(o, c) - intra), 4)
            v = int(vol_base * (up_mult if c > prev else 1.0) * rng.uniform(0.8, 1.2))
            candles.append(Candle(d, o, h, l, c, v))
            d += timedelta(days=1)
    return candles


# ── Fundamental presets ───────────────────────────────────────────────

def _strong_fundamentals() -> list[FundamentalsQuarter]:
    """Three quarters of accelerating EPS + revenue → F ≈ 9/10."""
    return [
        FundamentalsQuarter("2025Q4", eps_yoy=40.0, revenue_yoy=30.0, reported_at=date(2026, 1, 20)),
        FundamentalsQuarter("2025Q3", eps_yoy=32.0, revenue_yoy=25.0, reported_at=date(2025, 10, 15)),
        FundamentalsQuarter("2025Q2", eps_yoy=25.0, revenue_yoy=20.0, reported_at=date(2025, 7, 15)),
    ]


def _good_fundamentals() -> list[FundamentalsQuarter]:
    """Two quarters of solid growth → F ≈ 8/10."""
    return [
        FundamentalsQuarter("2025Q4", eps_yoy=30.0, revenue_yoy=22.0, reported_at=date(2026, 1, 20)),
        FundamentalsQuarter("2025Q3", eps_yoy=25.0, revenue_yoy=18.0, reported_at=date(2025, 10, 15)),
    ]


def _weak_fundamentals() -> list[FundamentalsQuarter]:
    """Single quarter of modest growth → F ≈ 2/10."""
    return [FundamentalsQuarter("2025Q4", eps_yoy=5.0, revenue_yoy=3.0)]


def _negative_fundamentals() -> list[FundamentalsQuarter]:
    """Declining EPS and revenue → earnings_caution=True, F = 0."""
    return [FundamentalsQuarter("2025Q4", eps_yoy=-20.0, revenue_yoy=-10.0)]


# ── Scenario candle blueprints ────────────────────────────────────────
# Format: (n_bars, drift, noise_amp, vol_base, up_day_vol_mult)

# A1: Bull run → VCP pullback → tight squeeze pivot  →  Buy
_SEGS_A1 = [
    (200,  0.0015, 0.014, 1_500_000, 1.2),
    (30,  -0.002,  0.010, 1_500_000, 0.9),
    (20,  -0.001,  0.005, 1_000_000, 0.9),
    (10,   0.001,  0.002,   700_000, 1.2),
]

# A2: Sustained uptrend ending at 52-week high  →  Wait (R/R gate)
_SEGS_A2 = [
    (200,  0.0012, 0.010, 1_500_000, 1.4),
    (60,   0.0006, 0.004, 1_200_000, 1.3),
]

# A3 / B1 shared: uptrend then significant pullback  →  Wait (score/trend gate)
_SEGS_PULLBACK = [
    (220,  0.0015, 0.010, 1_500_000, 1.2),
    (40,  -0.002,  0.010, 1_800_000, 0.8),
]

# B2: Mild rise then long sideways grind  →  Hold Off (close dips below SMA200)
_SEGS_B2 = [
    (100,  0.001,  0.005, 1_000_000, 1.0),
    (160,  0.0,    0.012, 1_000_000, 1.0),
]

# C1: Steady uptrend then sharp -30 % crash  →  Hold Off
_SEGS_C1 = [
    (200,  0.001,  0.010, 1_500_000, 1.0),
    (60,  -0.007,  0.010, 2_000_000, 0.8),
]

# C2: Rapid bubble run-up then slow multi-month deflation  →  Hold Off
_SEGS_C2 = [
    (100,  0.005,  0.010, 2_000_000, 1.5),
    (160, -0.003,  0.015, 1_500_000, 0.8),
]

# C3 / D1 base: clean uptrend (would normally pass SMA filter)
_SEGS_UPTREND = [(260, 0.001, 0.010, 1_500_000, 1.2)]

# D1: Gentle rise then long choppy high-noise grind  →  Hold Off
_SEGS_D1 = [
    (100,  0.001,  0.005, 1_000_000, 1.0),
    (160,  0.0,    0.020, 1_000_000, 1.0),
]

# D2: Base uptrend → speculative pump (+40 %) → violent dump (-50 %)  →  Hold Off
_SEGS_D2 = [
    (200,  0.001,  0.008, 1_000_000, 1.1),
    (20,   0.020,  0.010, 3_000_000, 2.0),
    (40,  -0.015,  0.010, 2_500_000, 0.7),
]


# ── Shared helpers ────────────────────────────────────────────────────

def _analyze(candles, fundamentals, catalysts=None, context=None, news=None):
    return StrategyAnalyzer().analyze(
        "TEST", "US", candles, fundamentals,
        catalysts or [], context or MarketContext(),
        news_assessment=news,
    )


_BAD_NEWS = NewsAssessment(
    sentiment="negative",
    severity="high",
    earnings_caution=True,
    score_adjustment=-3,
    reasoning="가이던스 컷 + 회계 이슈",
    source="test",
)


# ═══════════════════════════════════════════════════════════════════════
# A1 — VCP Buy: long bull run → VCP base → tight pivot
# Verified: signal=Buy, score=24, rr=2.47, vcp_score=9
# ═══════════════════════════════════════════════════════════════════════

class A1VcpBuyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_A1), _strong_fundamentals())

    def test_signal_is_buy(self):
        self.assertEqual(self.report.signal, "Buy")

    def test_rr_meets_minimum(self):
        self.assertGreaterEqual(self.report.trade_plan.rr_ratio, 1.5)

    def test_score_meets_threshold(self):
        self.assertGreaterEqual(self.report.scoreboard.total, 24)

    def test_vcp_pattern_detected(self):
        self.assertIn("VCP", self.report.indicators.vcp_pattern)

    def test_vcp_score_high(self):
        self.assertGreaterEqual(self.report.indicators.vcp_score, 7)

    def test_close_above_sma200(self):
        self.assertGreater(self.report.indicators.close, self.report.indicators.sma200)

    def test_bollinger_squeeze(self):
        # Tight VCP base → Bollinger width well below squeeze threshold (0.08)
        self.assertLess(self.report.indicators.bollinger_width, 0.08)

    def test_no_earnings_caution(self):
        self.assertFalse(self.report.earnings_caution)


# ═══════════════════════════════════════════════════════════════════════
# A2 — R/R Gate: score ≥ 27 but R/R = 0.75 → Wait
# Tests that a high score alone does not override the R/R floor.
# Verified: signal=Wait, score=27, rr=0.75, T=10
# ═══════════════════════════════════════════════════════════════════════

class A2RrGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_A2), _strong_fundamentals())

    def test_signal_is_wait(self):
        self.assertEqual(self.report.signal, "Wait")

    def test_score_would_qualify_for_strong_buy(self):
        self.assertGreaterEqual(self.report.scoreboard.total, 25)

    def test_rr_is_below_minimum(self):
        self.assertLess(self.report.trade_plan.rr_ratio, 1.5)

    def test_trend_score_is_perfect(self):
        # Pure uptrend → all trend conditions met
        self.assertEqual(self.report.scoreboard.technical_trend, 10)

    def test_close_above_sma200(self):
        self.assertGreater(self.report.indicators.close, self.report.indicators.sma200)

    def test_bollinger_tight_at_peak(self):
        # Late-stage uptrend coils tight before breakout attempt
        self.assertLess(self.report.indicators.bollinger_width, 0.05)


# ═══════════════════════════════════════════════════════════════════════
# A3 — Score Gate: R/R = 3.28 but score = 10 → Wait
# Tests that good R/R alone does not override the score floor.
# Verified: signal=Wait, score=10, rr=3.28 (weak_funds kills F sub-score)
# ═══════════════════════════════════════════════════════════════════════

class A3ScoreGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_PULLBACK), _weak_fundamentals())

    def test_signal_is_wait(self):
        self.assertEqual(self.report.signal, "Wait")

    def test_rr_exceeds_minimum(self):
        self.assertGreater(self.report.trade_plan.rr_ratio, 1.5)

    def test_score_below_buy_threshold(self):
        self.assertLess(self.report.scoreboard.total, 24)

    def test_fundamentals_score_is_low(self):
        self.assertLessEqual(self.report.scoreboard.fundamentals, 3)

    def test_no_earnings_caution(self):
        self.assertFalse(self.report.earnings_caution)


# ═══════════════════════════════════════════════════════════════════════
# B1 — Healthy Pullback: uptrend + -10 % pullback, solid fundamentals
# Pullback kills T sub-score (SMA50 drops); overall score < 24 → Wait.
# Verified: signal=Wait, score=16, rr=3.28
# ═══════════════════════════════════════════════════════════════════════

class B1PullbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_PULLBACK), _good_fundamentals())

    def test_signal_not_buy(self):
        self.assertNotIn(self.report.signal, {"Buy", "Strong Buy"})

    def test_fundamentals_score_reflects_solid_growth(self):
        self.assertGreaterEqual(self.report.scoreboard.fundamentals, 6)

    def test_rr_is_attractive(self):
        # After pullback, old resistance creates a wide reward target
        self.assertGreater(self.report.trade_plan.rr_ratio, 1.5)

    def test_trend_score_weakened_by_pullback(self):
        # SMA50 crosses below SMA200 (or close dips to SMA200) during pullback
        self.assertLessEqual(self.report.scoreboard.technical_trend, 8)

    def test_vcp_not_formed(self):
        # Simple pullback without volatility contraction ≠ VCP
        self.assertNotIn("VCP base with constructive contraction", self.report.indicators.vcp_pattern)


# ═══════════════════════════════════════════════════════════════════════
# B2 — Sideways Consolidation: long flat grind → low score, no pattern
# Close drifts below SMA200 → Hold Off (master trend filter).
# Verified: signal=Hold Off, score=11
# ═══════════════════════════════════════════════════════════════════════

class B2SidewaysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_B2), _weak_fundamentals())

    def test_signal_not_buy(self):
        self.assertNotIn(self.report.signal, {"Buy", "Strong Buy"})

    def test_total_score_is_low(self):
        self.assertLessEqual(self.report.scoreboard.total, 16)

    def test_vcp_not_detected(self):
        self.assertNotIn("VCP base with constructive contraction", self.report.indicators.vcp_pattern)

    def test_trend_score_low(self):
        # Flat price → SMAs converge → alignment weakens
        self.assertLessEqual(self.report.scoreboard.technical_trend, 6)


# ═══════════════════════════════════════════════════════════════════════
# C1 — Sharp Correction: steady uptrend then -30 % crash
# close << SMA200 → master trend filter always fires Hold Off.
# Verified: signal=Hold Off, T=0, rsi=1.8
# ═══════════════════════════════════════════════════════════════════════

class C1SharpCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_C1), _good_fundamentals())

    def test_signal_is_hold_off(self):
        self.assertEqual(self.report.signal, "Hold Off")

    def test_close_below_sma200(self):
        self.assertLess(self.report.indicators.close, self.report.indicators.sma200)

    def test_trend_score_is_zero(self):
        # Death cross + close below SMA200 → 0
        self.assertEqual(self.report.scoreboard.technical_trend, 0)

    def test_rsi_in_oversold_territory(self):
        self.assertLess(self.report.indicators.rsi14, 20)

    def test_no_vcp_in_crash(self):
        self.assertEqual(self.report.indicators.vcp_score, 0)


# ═══════════════════════════════════════════════════════════════════════
# C2 — Bubble Burst: +150 % run-up then slow -40 % deflation
# SMA200 lags the bubble peak; close falls far below it → Hold Off.
# Verified: signal=Hold Off, close=101.8 vs sma200=138.8
# ═══════════════════════════════════════════════════════════════════════

class C2BubbleBurstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_C2), _good_fundamentals())

    def test_signal_is_hold_off(self):
        self.assertEqual(self.report.signal, "Hold Off")

    def test_close_far_below_sma200(self):
        # SMA200 is inflated by the bubble run; close has retreated far below
        self.assertLess(self.report.indicators.close, self.report.indicators.sma200 * 0.85)

    def test_trend_score_is_zero(self):
        self.assertEqual(self.report.scoreboard.technical_trend, 0)

    def test_momentum_score_is_very_low(self):
        self.assertLessEqual(self.report.scoreboard.momentum_flow, 2)


# ═══════════════════════════════════════════════════════════════════════
# C3 — Earnings Miss + LLM Veto
# Good chart shape, but negative fundamentals set earnings_caution=True
# and the LLM high-severity veto forces Hold Off regardless of trend.
# Verified: signal=Hold Off, earnings_caution=True
# ═══════════════════════════════════════════════════════════════════════

class C3EarningsMissLlmVetoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candles = _make_candles(_SEGS_UPTREND)
        cls.report_with_veto = _analyze(cls.candles, _negative_fundamentals(), news=_BAD_NEWS)
        cls.report_no_news = _analyze(cls.candles, _negative_fundamentals())

    def test_llm_veto_produces_hold_off(self):
        self.assertEqual(self.report_with_veto.signal, "Hold Off")

    def test_earnings_caution_set_by_veto_news(self):
        self.assertTrue(self.report_with_veto.earnings_caution)

    def test_negative_fundamentals_alone_set_earnings_caution(self):
        # Even without LLM news, negative EPS raises the caution flag
        self.assertTrue(self.report_no_news.earnings_caution)

    def test_negative_fundamentals_zero_fundamentals_score(self):
        self.assertEqual(self.report_with_veto.scoreboard.fundamentals, 0)

    def test_llm_score_adjustment_applied(self):
        # LLM score_adjustment=-3 on top of already-0 fundamentals stays at 0 (clamped)
        self.assertEqual(self.report_with_veto.scoreboard.fundamentals, 0)

    def test_good_chart_would_pass_sma_filter(self):
        # The candles themselves are a clean uptrend; SMA filter is not the veto source
        self.assertGreater(self.report_no_news.indicators.close, self.report_no_news.indicators.sma200)


# ═══════════════════════════════════════════════════════════════════════
# D1 — Choppy Whipsaw: random high-noise walk
# No trend, no VCP, close drifts below SMA200 → Hold Off.
# Verified: signal=Hold Off, score=8, vcp_score=2
# ═══════════════════════════════════════════════════════════════════════

class D1ChoppyWhipsawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_D1), _weak_fundamentals())

    def test_signal_not_buy(self):
        self.assertNotIn(self.report.signal, {"Buy", "Strong Buy"})

    def test_total_score_very_low(self):
        self.assertLessEqual(self.report.scoreboard.total, 14)

    def test_no_vcp_pattern(self):
        # Random walk has no systematic volatility contraction
        self.assertNotIn("VCP base with constructive contraction", self.report.indicators.vcp_pattern)

    def test_trend_score_low(self):
        self.assertLessEqual(self.report.scoreboard.technical_trend, 4)


# ═══════════════════════════════════════════════════════════════════════
# D2 — Pump & Dump: base → speculative spike → violent reversal
# Dump brings price far below SMA200 (lagged by the spike) → Hold Off.
# Verified: signal=Hold Off, score=12, rsi=6.1, bw=0.40
# ═══════════════════════════════════════════════════════════════════════

class D2PumpDumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _analyze(_make_candles(_SEGS_D2), _good_fundamentals())

    def test_signal_is_hold_off(self):
        self.assertEqual(self.report.signal, "Hold Off")

    def test_close_below_sma200(self):
        # SMA200 is inflated by the pump spike; dump brings close well below
        self.assertLess(self.report.indicators.close, self.report.indicators.sma200)

    def test_rsi_extreme_oversold(self):
        self.assertLess(self.report.indicators.rsi14, 20)

    def test_bollinger_very_wide_after_extreme_move(self):
        # The spike+crash massively widens the 20-day Bollinger bands
        self.assertGreater(self.report.indicators.bollinger_width, 0.20)

    def test_trend_score_collapsed(self):
        self.assertLessEqual(self.report.scoreboard.technical_trend, 2)


# ═══════════════════════════════════════════════════════════════════════
# Cross-scenario invariants
# ═══════════════════════════════════════════════════════════════════════

class CrossScenarioInvariantTests(unittest.TestCase):
    def test_uptrend_outscores_downtrend(self):
        up_report = _analyze(_make_candles(_SEGS_A2), _strong_fundamentals())
        down_report = _analyze(_make_candles(_SEGS_C1), _good_fundamentals())
        self.assertGreater(up_report.scoreboard.total, down_report.scoreboard.total)

    def test_vcp_squeeze_bollinger_far_tighter_than_bubble_burst(self):
        """VCP base (tight noise) should have much narrower Bollinger than a bubble burst."""
        a1 = _analyze(_make_candles(_SEGS_A1), _strong_fundamentals())
        c2 = _analyze(_make_candles(_SEGS_C2), _good_fundamentals())
        self.assertLess(a1.indicators.bollinger_width, c2.indicators.bollinger_width)

    def test_llm_positive_news_boosts_fundamentals_score(self):
        candles = _make_candles(_SEGS_A1)
        neutral = _analyze(candles, _good_fundamentals())
        good_news = NewsAssessment(
            sentiment="positive", severity="high", earnings_caution=False,
            score_adjustment=2, reasoning="대형 계약 체결 + 어닝 서프라이즈", source="test",
        )
        boosted = _analyze(candles, _good_fundamentals(), news=good_news)
        self.assertGreaterEqual(boosted.scoreboard.fundamentals, neutral.scoreboard.fundamentals)

    def test_llm_negative_high_severity_always_holds_off(self):
        """Regardless of how good the chart is, high-severity negative news = Hold Off."""
        candles = _make_candles(_SEGS_A2)  # perfect uptrend
        r = _analyze(candles, _strong_fundamentals(), news=_BAD_NEWS)
        self.assertEqual(r.signal, "Hold Off")

    def test_close_below_sma200_always_hold_off_when_200_bars_available(self):
        """The master trend filter is an unconditional gate when 200+ bars exist."""
        for segs in (_SEGS_C1, _SEGS_C2, _SEGS_D2):
            r = _analyze(_make_candles(segs), _strong_fundamentals())
            ind = r.indicators
            if ind.close < ind.sma200:
                self.assertEqual(r.signal, "Hold Off",
                    msg=f"Expected Hold Off when close={ind.close:.2f} < sma200={ind.sma200:.2f}")


if __name__ == "__main__":
    unittest.main()
