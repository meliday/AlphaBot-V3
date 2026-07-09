"""Market-wide regime filter (CANSLIM's "M" — Market Direction).

Blocks new entries when the broad index is below its 200-day SMA, or when
institutional selling pressure piles up while still above it (IBD-style
distribution-day count). Cached so we don't hit yfinance on every iteration.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from alpha_bot.models import Market

logger = logging.getLogger(__name__)

# Index proxies per market
_INDEX_TICKERS: dict[str, str] = {
    "US": "^GSPC",   # S&P 500
    "KR": "^KS11",   # KOSPI Composite
}

# Cache: market → (timestamp_seconds, regime)
_CACHE: dict[str, tuple[float, "MarketRegime"]] = {}
# 6h TTL: an index can break its 200-SMA intraday during a sharp sell-off,
# and a 24h cache would keep new buys flowing through the regime flip until
# the next day. 6h gives four refreshes per trading day — timely enough to
# catch a regime change within one session while keeping yfinance volume low.
_CACHE_TTL_SEC = 6 * 3600
_LOCK = threading.RLock()

# ── Distribution days (IBD) ──
# A distribution day = index falls ≥ 0.2% on volume higher than the prior
# session — institutions unloading into strength. 6+ within ~5 weeks has
# historically preceded market tops, often weeks before the 200-day SMA
# breaks, so it acts as an early-warning veto on top of the SMA filter.
_DISTRIBUTION_LOOKBACK = 25   # trading days (~5 weeks)
_DISTRIBUTION_LIMIT = 6       # count at/above this → regime turns cautious
_DISTRIBUTION_DROP_PCT = 0.2  # minimum decline to qualify


def count_distribution_days(
    closes: list[float],
    volumes: list[float],
    lookback: int = _DISTRIBUTION_LOOKBACK,
    drop_pct: float = _DISTRIBUTION_DROP_PCT,
) -> int | None:
    """Count IBD-style distribution days over the last ``lookback`` sessions.

    Returns None (fail-open) when the volume series is missing or too sparse
    to be trusted — e.g. yfinance reports zero volume for some indices.
    """
    if len(closes) < 2 or len(volumes) != len(closes):
        return None
    start = max(1, len(closes) - lookback)
    usable = 0
    count = 0
    for i in range(start, len(closes)):
        volume, prev_volume = volumes[i], volumes[i - 1]
        if volume <= 0 or prev_volume <= 0:
            continue
        usable += 1
        dropped = closes[i] <= closes[i - 1] * (1 - drop_pct / 100.0)
        if dropped and volume > prev_volume:
            count += 1
    if usable < lookback // 2:
        return None  # not enough trustworthy volume data to judge
    return count


@dataclass(frozen=True)
class MarketRegime:
    market: Market
    index_symbol: str
    is_bullish: bool          # close > SMA200 → constructive
    close: float | None
    sma200: float | None
    reason: str
    # Index compound return over ~63 trading days (3 months). Used as the
    # benchmark leg of the relative-strength score when MarketContext does
    # not provide an explicit benchmark_return_3m.
    return_3m: float | None = None
    # IBD distribution-day count over the last ~25 sessions; None when the
    # index volume series is unusable (fail-open).
    distribution_days: int | None = None


def get_regime(market: Market, force_refresh: bool = False) -> MarketRegime:
    """Return the cached regime for `market`, refreshing if stale."""
    key = market.upper()
    with _LOCK:
        cached = _CACHE.get(key)
        if not force_refresh and cached and (time.time() - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]
    regime = _fetch_regime(key)
    with _LOCK:
        _CACHE[key] = (time.time(), regime)
    return regime


def _fetch_regime(market: str) -> MarketRegime:
    symbol = _INDEX_TICKERS.get(market)
    if not symbol:
        return MarketRegime(
            market=market, index_symbol="", is_bullish=True,
            close=None, sma200=None,
            reason=f"Unknown market '{market}'; defaulting to bullish.",
        )
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="13mo", interval="1d", auto_adjust=False)
        frame = hist.dropna(subset=["Close"])
        closes = [float(v) for v in frame["Close"].tolist()]
        volumes = [
            float(v) if v == v else 0.0  # NaN → 0 (skipped as unusable)
            for v in frame.get("Volume", []).tolist()
        ] if "Volume" in frame else []
        if len(closes) < 200:
            return MarketRegime(
                market=market, index_symbol=symbol, is_bullish=True,
                close=closes[-1] if closes else None, sma200=None,
                reason=f"{symbol} 200일 데이터 부족 ({len(closes)}일); fail-open",
            )
        close = closes[-1]
        sma200 = sum(closes[-200:]) / 200
        bullish = close > sma200
        diff_pct = (close / sma200 - 1) * 100
        return_3m = None
        if len(closes) > 63 and closes[-64] > 0:
            return_3m = (close / closes[-64] - 1) * 100
        dist_days = count_distribution_days(closes, volumes) if volumes else None
        if bullish:
            reason = f"{symbol} 200일선 위 ({diff_pct:+.1f}%)"
            # Distribution-day veto: heavy institutional selling while still
            # above the 200-day line — an early top warning the SMA filter
            # alone would catch weeks later.
            if dist_days is not None and dist_days >= _DISTRIBUTION_LIMIT:
                bullish = False
                reason += (
                    f" 이지만 분배일 {dist_days}회/{_DISTRIBUTION_LOOKBACK}일 "
                    f"(기준 {_DISTRIBUTION_LIMIT}회) — 기관 매도 압력, 신규 매수 차단"
                )
        else:
            reason = f"{symbol} 200일선 아래 ({diff_pct:+.1f}%) — 신규 매수 차단"
        return MarketRegime(market=market, index_symbol=symbol, is_bullish=bullish,
                            close=close, sma200=sma200, reason=reason,
                            return_3m=return_3m, distribution_days=dist_days)
    except Exception as exc:
        logger.warning("Market regime fetch failed for %s: %s", market, exc)
        # Fail-open: if we can't determine regime, don't block trading.
        return MarketRegime(
            market=market, index_symbol=symbol, is_bullish=True,
            close=None, sma200=None,
            reason=f"{symbol} 조회 실패 ({exc}); fail-open",
        )
