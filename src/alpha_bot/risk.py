from __future__ import annotations

import math

from alpha_bot.models import Candle, TradePlan

# ── Stop-loss bounds (% of close) ──────────────────────────
# Minervini's "Trade Like a Stock Market Wizard" caps per-trade risk at 7%
# for momentum setups; we keep that as the upper bound. The lower bound at
# 2.5% prevents noise-driven whipsaws on intraday volatility.
_STOP_MAX_RISK = 0.07
_STOP_MIN_RISK = 0.025
_ATR_MULT = 2.0  # Wilder/Minervini standard


def build_trade_plan(candles: list[Candle], sma50: float, min_rr: float = 1.5) -> TradePlan:
    if len(candles) < 60:
        raise ValueError("Need at least 60 candles for support/resistance plan")
    close = candles[-1].close
    support_20d = min(candle.low for candle in candles[-20:])
    resistance_60d = max(candle.high for candle in candles[-60:])
    low_60d = min(candle.low for candle in candles[-60:])
    base_height = resistance_60d - low_60d

    # ── Volatility-adjusted stop ───────────────────────────
    # ATR stop gives the trade breathing room proportional to recent volatility.
    # Structural stop (just below 20-day low or 50-day SMA) acts as a sanity floor.
    from alpha_bot.strategy.indicators import latest_atr  # deferred to avoid cycle
    atr14 = latest_atr(candles, 14)
    atr_stop = close - _ATR_MULT * atr14
    structural_stop = min(sma50, support_20d) * 0.985  # below the lower of the two

    # Use the wider (further from close) of the two so we don't whipsaw on the
    # tighter level. With prices, "further from close" means lower → min().
    stop_loss = min(atr_stop, structural_stop)

    # Bound: max 7% risk, min 2.5% risk (proportional to price).
    stop_loss = max(stop_loss, close * (1 - _STOP_MAX_RISK))
    stop_loss = min(stop_loss, close * (1 - _STOP_MIN_RISK))

    risk_amount = close - stop_loss

    # Target1: real chart resistance if it's meaningfully above close;
    # otherwise a half-base measured move (post-breakout case).
    if resistance_60d > close * 1.03:
        target1 = resistance_60d
    else:
        target1 = close + base_height * 0.5

    # Target2: extension — full-base measured move or 10% above target1.
    target2 = max(target1 * 1.10, close + base_height * 1.0)

    entry_low = close * 0.99
    entry_high = close * 1.01
    rr_ratio = calculate_rr(close, stop_loss, target1)

    alternate_entry = None
    alternate_rr = None
    if rr_ratio < min_rr and sma50 < close:
        alternate_entry = max(sma50, stop_loss * 1.03)
        alternate_rr = calculate_rr(alternate_entry, stop_loss, target1)

    return TradePlan(
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        target1=target1,
        target2=target2,
        rr_ratio=rr_ratio,
        entry_reference=close,
        stop_pct=(stop_loss / close - 1) * 100,
        target1_pct=(target1 / close - 1) * 100,
        target2_pct=(target2 / close - 1) * 100,
        alternate_entry=alternate_entry,
        alternate_rr_ratio=alternate_rr,
    )


def calculate_rr(entry: float, stop: float, target: float) -> float:
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or math.isclose(risk, 0.0):
        return 0.0
    return reward / risk
