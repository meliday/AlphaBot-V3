from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)

_KR_SUFFIX = ".KS"
_KR_KOSDAQ_SUFFIX = ".KQ"


class Quote(TypedDict):
    ticker: str
    market: str
    company: str
    price: float | None
    prev_close: float | None
    change: float | None      # absolute
    change_pct: float | None  # percent
    currency: str
    error: str | None


def fetch_quotes(tickers: list[dict]) -> list[Quote]:
    """Fetch current price + day change for a list of {ticker, market, company?} dicts."""
    results: list[Quote] = []
    for row in tickers:
        ticker = row.get("ticker", "")
        market = row.get("market", "US").upper()
        company = row.get("company", "") or ""
        results.append(_fetch_one(ticker, market, company))
    return results


def _fetch_one(ticker: str, market: str, company: str) -> Quote:
    base: Quote = {
        "ticker": ticker,
        "market": market,
        "company": company,
        "price": None,
        "prev_close": None,
        "change": None,
        "change_pct": None,
        "currency": "KRW" if market == "KR" else "USD",
        "error": None,
    }
    try:
        import yfinance as yf
        yf_ticker = ticker if market == "US" else f"{ticker}{_KR_SUFFIX}"
        fi = yf.Ticker(yf_ticker).fast_info
        price = float(fi.last_price) if fi.last_price else None
        prev = float(fi.previous_close) if fi.previous_close else None
        if price is None and market == "KR":
            # KOSDAQ fallback
            fi = yf.Ticker(f"{ticker}{_KR_KOSDAQ_SUFFIX}").fast_info
            price = float(fi.last_price) if fi.last_price else None
            prev = float(fi.previous_close) if fi.previous_close else None
        base["price"] = price
        base["prev_close"] = prev
        if price is not None and prev and prev != 0:
            base["change"] = price - prev
            base["change_pct"] = (price - prev) / prev * 100
    except Exception as exc:
        logger.warning("Quote fetch failed for %s:%s — %s", market, ticker, exc)
        base["error"] = str(exc)
    return base
