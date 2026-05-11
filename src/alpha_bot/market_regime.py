"""Market-wide regime filter (CANSLIM's "M" — Market Direction).

Blocks new entries when the broad index is below its 200-day SMA. Cached for
24h so we don't hit yfinance on every iteration.
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


@dataclass(frozen=True)
class MarketRegime:
    market: Market
    index_symbol: str
    is_bullish: bool          # close > SMA200 → constructive
    close: float | None
    sma200: float | None
    reason: str


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
        closes = [float(v) for v in hist["Close"].dropna().tolist()]
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
        if bullish:
            reason = f"{symbol} 200일선 위 ({diff_pct:+.1f}%)"
        else:
            reason = f"{symbol} 200일선 아래 ({diff_pct:+.1f}%) — 신규 매수 차단"
        return MarketRegime(market=market, index_symbol=symbol, is_bullish=bullish,
                            close=close, sma200=sma200, reason=reason)
    except Exception as exc:
        logger.warning("Market regime fetch failed for %s: %s", market, exc)
        # Fail-open: if we can't determine regime, don't block trading.
        return MarketRegime(
            market=market, index_symbol=symbol, is_bullish=True,
            close=None, sma200=None,
            reason=f"{symbol} 조회 실패 ({exc}); fail-open",
        )
