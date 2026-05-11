"""Shared utility functions used across multiple modules."""

from __future__ import annotations

from typing import Any

from alpha_bot.errors import DataError
from alpha_bot.models import Market


# ── Exchange Code Inference ──────────────────────────────────────────
# KIS requires an overseas exchange code for US orders/price queries.
# This is a conservative default mapping; extend before live use.

_NYSE_TICKERS = frozenset({
    "BRK.B", "KO", "JPM", "V", "DIS", "NKE", "WMT",
    "JNJ", "PG", "UNH", "HD", "BAC", "XOM", "CVX",
    "MRK", "PFE", "ABT", "CRM", "ORCL", "IBM",
})

_AMEX_TICKERS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "GDX",
})

# Runtime cache populated by data providers / brokers after a successful
# call confirms which KIS overseas exchange code actually serves a ticker.
# Maps "SYMBOL" → one of {"NASD", "NYSE", "AMEX"}.
_US_EXCD_CACHE: dict[str, str] = {}


def infer_us_exchange_code(ticker: str) -> str:
    """Return the KIS exchange code for a US ticker.

    Order: runtime cache (from prior successful fetch) → static whitelist
    (NYSE/AMEX) → NASD fallback. The cache is the source of truth once a
    ticker has been resolved against KIS's real feed (e.g. Arca-listed ETFs
    like SOXL, which the static list cannot enumerate exhaustively).
    """
    upper = ticker.upper()
    cached = _US_EXCD_CACHE.get(upper)
    if cached:
        return cached
    if upper in _NYSE_TICKERS:
        return "NYSE"
    if upper in _AMEX_TICKERS:
        return "AMEX"
    return "NASD"


# ── Safe Value Parsing ───────────────────────────────────────────────

def safe_float(row: dict[str, Any], *keys: str) -> float:
    """Return the first non-empty float value from *row* for the given *keys*.

    Raises ``DataError`` if none of the keys yield a usable value.
    """
    for key in keys:
        val = row.get(key)
        if val is not None and val != "":
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    raise DataError(f"No valid numeric value found for keys: {keys} in row: {row}")


# ── Market Validation ────────────────────────────────────────────────

_VALID_MARKETS: frozenset[str] = frozenset({"KR", "US"})


def validate_market(value: str) -> Market:
    """Validate and normalize a market string to a ``Market`` literal."""
    upper = value.upper()
    if upper not in _VALID_MARKETS:
        raise ValueError(f"Invalid market '{value}'. Must be one of: {sorted(_VALID_MARKETS)}")
    return upper  # type: ignore[return-value]
