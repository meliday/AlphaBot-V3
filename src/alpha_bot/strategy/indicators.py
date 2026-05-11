from __future__ import annotations

import math
from statistics import mean, stdev

from alpha_bot.models import Candle


def sma(values: list[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        out.append(running / window if index >= window - 1 else None)
    return out


def latest_sma(values: list[float], window: int) -> float:
    """Return the latest SMA. Uses all available data if len(values) < window."""
    if not values:
        raise ValueError("No values provided")
    effective = min(window, len(values))
    return sum(values[-effective:]) / effective


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return [None for _ in values]

    out: list[float | None] = [None for _ in values]
    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = mean(gains)
    avg_loss = mean(losses)
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[index] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def latest_rsi(values: list[float], period: int = 14) -> float:
    """Return the latest RSI. Returns 50.0 (neutral) if data is insufficient."""
    series = rsi(values, period)
    latest = series[-1] if series else None
    return latest if latest is not None else 50.0


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder's Average True Range. Returns a list aligned to `candles`."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(candles) < period + 1:
        return [None for _ in candles]
    out: list[float | None] = [None for _ in candles]
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    # Initial ATR = simple average of first `period` true ranges
    avg = sum(trs[:period]) / period
    out[period] = avg
    # Wilder's smoothing
    for i in range(period + 1, len(candles)):
        tr_idx = i - 1  # trs is offset by 1 vs candles
        avg = (avg * (period - 1) + trs[tr_idx]) / period
        out[i] = avg
    return out


def latest_atr(candles: list[Candle], period: int = 14) -> float:
    """Return latest ATR. Falls back to mean true range if data is insufficient."""
    series = atr(candles, period)
    latest = series[-1] if series else None
    if latest is not None:
        return latest
    # fallback: simple mean of all true ranges
    if len(candles) < 2:
        return candles[-1].close * 0.02 if candles else 1.0
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
           for p, c in zip(candles, candles[1:])]
    return sum(trs) / len(trs)


def latest_bollinger(values: list[float], window: int = 20, stdevs: float = 2.0) -> tuple[float, float, float, float]:
    """Returns (mid, upper, lower, width). Uses available data if len < window.

    Uses the sample standard deviation (Bessel's correction, n-1) rather than
    the population sigma, which is the convention in trading-platform Bollinger
    Band implementations (TradingView, MetaTrader, KIS HTS). The difference is
    ~2.6% on a 20-bar window but matters when calibrating squeeze thresholds.
    """
    if not values:
        raise ValueError("No values provided")
    slice_ = values[-min(window, len(values)):]
    mid = mean(slice_)
    # stdev() requires at least 2 data points; fall back to 0 sigma otherwise.
    sigma = stdev(slice_) if len(slice_) >= 2 else 0.0
    upper = mid + sigma * stdevs
    lower = mid - sigma * stdevs
    width = (upper - lower) / mid if not math.isclose(mid, 0.0) else 0.0
    return mid, upper, lower, width


def detect_vcp(candles: list[Candle], lookback: int = 60, chunks: int = 3) -> tuple[str, int, str]:
    if len(candles) < lookback:
        return "no clear pattern yet", 0, f"Need {lookback} candles for VCP read."
    recent = candles[-lookback:]
    chunk_size = lookback // chunks
    ranges: list[float] = []
    volumes: list[float] = []
    for chunk_index in range(chunks):
        start = chunk_index * chunk_size
        # Last chunk absorbs any remainder so no candles are silently dropped
        # when lookback is not evenly divisible by chunks (e.g. 61 // 3 = 20).
        end = (chunk_index + 1) * chunk_size if chunk_index < chunks - 1 else lookback
        chunk = recent[start:end]
        high = max(candle.high for candle in chunk)
        low = min(candle.low for candle in chunk)
        ranges.append((high - low) / high if high else 0.0)
        volumes.append(mean([candle.volume for candle in chunk]))

    narrowing_ranges = all(ranges[index] < ranges[index - 1] for index in range(1, len(ranges)))
    fading_volume = volumes[-1] < volumes[0] * 0.95
    close = candles[-1].close
    high_52w = max(candle.high for candle in candles[-252:])
    near_high = close >= high_52w * 0.85

    score = 0
    if narrowing_ranges:
        score += 4
    if fading_volume:
        score += 3
    if near_high:
        score += 2
    if candles[-1].close > candles[-2].close:
        score += 1
    score = min(score, 10)

    range_text = " -> ".join(f"{value * 100:.1f}%" for value in ranges)
    volume_text = " -> ".join(f"{value:,.0f}" for value in volumes)
    details = f"range contraction {range_text}; avg volume {volume_text}"

    if score >= 8:
        return "VCP base with constructive contraction", score, details
    if score >= 5:
        return "early VCP / volatility contraction", score, details
    return "no clear VCP yet", score, details


def volume_accumulation_summary(candles: list[Candle], lookback: int = 20) -> tuple[int, str]:
    if len(candles) < lookback + 1:
        return 0, "insufficient volume history"
    recent = candles[-lookback:]
    up_volume = [
        candle.volume
        for prev, candle in zip(candles[-lookback - 1 : -1], recent)
        if candle.close > prev.close
    ]
    down_volume = [
        candle.volume
        for prev, candle in zip(candles[-lookback - 1 : -1], recent)
        if candle.close <= prev.close
    ]
    if not up_volume or not down_volume:
        return 1, "one-sided volume pattern; manual check required"
    up_avg = mean(up_volume)
    down_avg = mean(down_volume)
    ratio = up_avg / down_avg if down_avg else 0.0
    if ratio >= 1.2:
        return 3, f"accumulation: up-day volume {ratio:.2f}x down-day volume"
    if ratio >= 0.95:
        return 2, f"neutral: up-day volume {ratio:.2f}x down-day volume"
    return 0, f"distribution risk: up-day volume {ratio:.2f}x down-day volume"


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if math.isclose(avg_loss, 0.0):
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
