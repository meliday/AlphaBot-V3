"""Live fundamentals fetchers.

US fundamentals come from yfinance (quarterly income statement +
earnings history). KR fundamentals come from KIS' domestic finance APIs
(``inquire-income-statement`` and ``inquire-financial-ratio``).

Both functions are best-effort: any failure (missing dep, API error,
unexpected schema) returns ``[]`` so the caller can fall back to local
cache without blowing up the iteration.

ETF handling: when a US ticker is identified as an ETF or mutual fund,
``fetch_us_fundamentals`` switches to an aggregation mode that fetches
the fundamentals of the ETF's top holdings and returns a weighted-average
``FundamentalsQuarter`` with ``etf_proxy=True``.
"""

from __future__ import annotations

import logging
from typing import Any

from alpha_bot.models import FundamentalsQuarter

logger = logging.getLogger(__name__)


# ── US via yfinance ──────────────────────────────────────────────────


_US_EPS_KEYS = ("Diluted EPS", "Basic EPS")
_US_REVENUE_KEYS = ("Total Revenue", "Operating Revenue", "Revenue")

# Single-stock leveraged/inverse ETF registry.
# Maps ETF ticker → (underlying_ticker, leverage_multiplier).
# leverage_multiplier is positive for bull, negative for bear/inverse.
_SINGLE_STOCK_LEVERAGED_ETF: dict[str, tuple[str, float]] = {
    # TSLA-based
    "TSLL": ("TSLA", 2.0),
    "TSLS": ("TSLA", -1.0),
    "TSLQ": ("TSLA", -1.0),
    "TSL2": ("TSLA", 2.0),
    # NVDA-based
    "NVDL": ("NVDA", 2.0),
    "NVDU": ("NVDA", 2.0),
    "NVDQ": ("NVDA", -1.0),
    "NVDS": ("NVDA", -1.5),
    "NVD":  ("NVDA", -2.0),
    # MSTR-based
    "MSTU": ("MSTR", 2.0),
    "MSTZ": ("MSTR", -2.0),
    "MSTX": ("MSTR", 2.0),
    # AMZN-based
    "AMZU": ("AMZN", 2.0),
    "AMZD": ("AMZN", -1.0),
    # AAPL-based
    "AAPU": ("AAPL", 2.0),
    "AAPD": ("AAPL", -1.0),
    # MSFT-based
    "MSFU": ("MSFT", 2.0),
    "MSFO": ("MSFT", -1.0),
    # GOOGL-based
    "GGLL": ("GOOGL", 2.0),
    "GGLS": ("GOOGL", -1.0),
    # META-based
    "METU": ("META", 2.0),
    "METD": ("META", -1.0),
    # AMD-based
    "AMDU": ("AMD", 2.0),
    "AMDS": ("AMD", -1.0),
    # COIN-based
    "CONL": ("COIN", 2.0),
    "CONS": ("COIN", -1.0),
    # PLTR-based
    "PLTU": ("PLTR", 2.0),
    # SMCI-based
    "SMCU": ("SMCI", 2.0),
    # ARM-based
    "ARMU": ("ARM", 2.0),
    # SNOW-based
    "SNWL": ("SNOW", 2.0),
    # UBER-based
    "UBRL": ("UBER", 2.0),
}

# Static fallback basket holdings for diversified/index ETFs
# when yfinance funds_data is unavailable. Weights are approximate % of NAV.
_STATIC_ETF_HOLDINGS: dict[str, list[tuple[str, float]]] = {
    "SOXL": [("NVDA", 20), ("AMD", 12), ("AVGO", 10), ("QCOM", 8), ("TSM", 7)],
    "SOXS": [("NVDA", 20), ("AMD", 12), ("AVGO", 10), ("QCOM", 8), ("TSM", 7)],
    "SOXX": [("NVDA", 9), ("AVGO", 8), ("AMD", 8), ("QCOM", 7), ("TSM", 6)],
    "TQQQ": [("AAPL", 12), ("MSFT", 11), ("NVDA", 9), ("AMZN", 7), ("META", 6)],
    "SQQQ": [("AAPL", 12), ("MSFT", 11), ("NVDA", 9), ("AMZN", 7), ("META", 6)],
    "QQQ":  [("AAPL", 12), ("MSFT", 11), ("NVDA", 9), ("AMZN", 7), ("META", 6)],
    "QQQS": [("AAPL", 12), ("MSFT", 11), ("NVDA", 9), ("AMZN", 7), ("META", 6)],
    "SPY":  [("AAPL", 7), ("MSFT", 6), ("NVDA", 5), ("AMZN", 3), ("GOOGL", 3)],
    "SPXL": [("AAPL", 7), ("MSFT", 6), ("NVDA", 5), ("AMZN", 3), ("GOOGL", 3)],
    "SPXS": [("AAPL", 7), ("MSFT", 6), ("NVDA", 5), ("AMZN", 3), ("GOOGL", 3)],
    "ARKK": [("TSLA", 10), ("COIN", 9), ("RBLX", 7), ("SQ", 6), ("TDOC", 5)],
    "XLK":  [("MSFT", 22), ("AAPL", 20), ("NVDA", 17), ("AVGO", 4), ("AMD", 3)],
    "SMH":  [("NVDA", 20), ("TSM", 12), ("AVGO", 8), ("ASML", 7), ("AMD", 6)],
    "VGT":  [("AAPL", 17), ("MSFT", 15), ("NVDA", 13), ("AVGO", 4), ("AMD", 3)],
    "FNGU": [("META", 10), ("AMZN", 10), ("AAPL", 10), ("NVDA", 10), ("NFLX", 10),
             ("MSFT", 10), ("GOOGL", 10), ("AMD", 10), ("TSLA", 10), ("SNOW", 10)],
    "LABU": [("VRTX", 8), ("REGN", 7), ("AMGN", 6), ("GILD", 6), ("MRNA", 5)],
    "LABD": [("VRTX", 8), ("REGN", 7), ("AMGN", 6), ("GILD", 6), ("MRNA", 5)],
    "TECL": [("MSFT", 22), ("AAPL", 20), ("NVDA", 17), ("AVGO", 4), ("AMD", 3)],
    "TECS": [("MSFT", 22), ("AAPL", 20), ("NVDA", 17), ("AVGO", 4), ("AMD", 3)],
    "UPRO": [("AAPL", 7), ("MSFT", 6), ("NVDA", 5), ("AMZN", 3), ("GOOGL", 3)],
    "CURE": [("UNH", 10), ("JNJ", 8), ("LLY", 8), ("ABT", 6), ("TMO", 5)],
    "IWM":  [("SMCI", 1), ("SAIA", 1), ("LUNR", 1), ("CELH", 1), ("BOOT", 1)],
    "TNA":  [("SMCI", 1), ("SAIA", 1), ("LUNR", 1), ("CELH", 1), ("BOOT", 1)],
}


def fetch_us_fundamentals(ticker: str, limit: int = 4, _depth: int = 0) -> list[FundamentalsQuarter]:
    try:
        import yfinance as yf
    except ImportError:
        logger.info("yfinance not installed; skipping US fundamentals fetch")
        return []

    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
    except Exception as exc:
        logger.warning("yfinance info fetch failed for %s: %s", ticker, exc)
        info = {}

    # Route ETFs/mutual funds to holdings-based proxy (guard against infinite recursion)
    if _depth == 0 and info.get("quoteType") in ("ETF", "MUTUALFUND"):
        return _fetch_etf_proxy_fundamentals(tk, ticker)

    try:
        inc = getattr(tk, "quarterly_income_stmt", None)
        if inc is None or getattr(inc, "empty", True):
            inc = getattr(tk, "quarterly_financials", None)
    except Exception as exc:
        logger.warning("yfinance fundamentals fetch failed for %s: %s", ticker, exc)
        return []
    if inc is None or getattr(inc, "empty", True):
        return []

    eps_row = _first_row(inc, _US_EPS_KEYS)
    rev_row = _first_row(inc, _US_REVENUE_KEYS)
    if eps_row is None and rev_row is None:
        return []

    columns = list(inc.columns)  # Newest first.
    out: list[FundamentalsQuarter] = []
    for i, col in enumerate(columns[:limit]):
        period = _format_quarter_label(col)
        eps_now = _safe(eps_row, col) if eps_row is not None else None
        rev_now = _safe(rev_row, col) if rev_row is not None else None
        eps_yoy = _yoy_pct(eps_now, _safe(eps_row, columns[i + 4]) if (eps_row is not None and i + 4 < len(columns)) else None)
        rev_yoy = _yoy_pct(rev_now, _safe(rev_row, columns[i + 4]) if (rev_row is not None and i + 4 < len(columns)) else None)
        eps_surprise = _us_eps_surprise(tk, i) if i == 0 else None
        out.append(
            FundamentalsQuarter(
                period=period,
                eps_yoy=eps_yoy,
                revenue_yoy=rev_yoy,
                eps_surprise=eps_surprise,
            )
        )
    return [q for q in out if q.eps_yoy is not None or q.revenue_yoy is not None]


def _first_row(df: Any, keys: tuple[str, ...]):
    try:
        idx = df.index
    except AttributeError:
        return None
    for key in keys:
        if key in idx:
            return df.loc[key]
    return None


def _safe(row: Any, col: Any) -> float | None:
    if row is None:
        return None
    try:
        val = row[col]
    except (KeyError, IndexError):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check
        return None
    return f


def _yoy_pct(now: float | None, prev: float | None) -> float | None:
    if now is None or prev is None or prev == 0:
        return None
    return (now / prev - 1.0) * 100.0


def _us_eps_surprise(ticker_obj: Any, i: int) -> float | None:
    try:
        hist = getattr(ticker_obj, "earnings_history", None)
        if hist is None or getattr(hist, "empty", True):
            return None
        if "surprisePercent" in hist.columns:
            val = hist.iloc[i]["surprisePercent"]
        elif "epsActual" in hist.columns and "epsEstimate" in hist.columns:
            actual = float(hist.iloc[i]["epsActual"])
            est = float(hist.iloc[i]["epsEstimate"])
            if est == 0:
                return None
            val = (actual / est - 1) * 100
        else:
            return None
        f = float(val)
        return f if f == f else None
    except Exception:
        return None


def _format_quarter_label(col: Any) -> str:
    if hasattr(col, "year") and hasattr(col, "month"):
        q = (col.month - 1) // 3 + 1
        return f"{col.year}Q{q}"
    return str(col)


# ── ETF holdings proxy ───────────────────────────────────────────────


def _get_etf_holdings(tk: Any, etf_ticker: str) -> list[tuple[str, float]]:
    """Return [(symbol, weight_pct), ...] for the ETF's top holdings.

    Tries yfinance funds_data first; falls back to the static map.
    Weight is a percentage of NAV (0–100 range).
    """
    try:
        funds_data = getattr(tk, "funds_data", None)
        if funds_data is not None:
            holdings_df = getattr(funds_data, "top_holdings", None)
            if holdings_df is not None and not getattr(holdings_df, "empty", True):
                sym_col = next(
                    (c for c in holdings_df.columns if c.lower() in ("symbol", "ticker")),
                    None,
                )
                wt_col = next(
                    (c for c in holdings_df.columns
                     if "percent" in c.lower() or "weight" in c.lower()),
                    None,
                )
                if sym_col:
                    rows: list[tuple[str, float]] = []
                    for i in range(min(10, len(holdings_df))):
                        sym = str(holdings_df.iloc[i][sym_col]).strip()
                        if not sym or sym.lower() == "nan":
                            continue
                        weight = float(holdings_df.iloc[i][wt_col]) * 100 if wt_col else 1.0
                        rows.append((sym, weight))
                    if rows:
                        logger.debug("ETF %s: live holdings %s", etf_ticker, [s for s, _ in rows[:5]])
                        return rows
    except Exception as exc:
        logger.debug("ETF funds_data unavailable for %s: %s", etf_ticker, exc)

    static = _STATIC_ETF_HOLDINGS.get(etf_ticker.upper(), [])
    if static:
        logger.debug("ETF %s: using static holdings map (%d tickers)", etf_ticker, len(static))
    return static


def _fetch_single_stock_etf_fundamentals(
    underlying: str, leverage: float, etf_ticker: str
) -> list[FundamentalsQuarter]:
    """Fetch underlying stock's fundamentals for a single-stock leveraged/inverse ETF.

    Returns up to 4 quarters tagged with etf_proxy=True. The first quarter's
    period encodes ETF metadata (e.g. 'ETF:TSLL→TSLA×2.0') so downstream
    scoring notes can surface the leverage context; subsequent quarters keep
    their original period labels so Q-o-Q acceleration logic works correctly.
    """
    quarters = fetch_us_fundamentals(underlying, limit=4, _depth=1)
    if not quarters:
        logger.info("ETF %s: underlying %s returned no fundamentals", etf_ticker, underlying)
        return []

    direction = "Bull" if leverage > 0 else "Bear"
    tag = f"ETF:{etf_ticker}→{underlying}×{abs(leverage):.1f}({direction})"
    result = []
    for i, q in enumerate(quarters):
        result.append(
            FundamentalsQuarter(
                period=tag if i == 0 else q.period,
                eps_yoy=q.eps_yoy,
                revenue_yoy=q.revenue_yoy,
                eps_surprise=q.eps_surprise if i == 0 else None,
                etf_proxy=True,
            )
        )
    logger.info(
        "ETF %s proxy (single-stock %s×%.1f): eps_yoy=%s rev_yoy=%s",
        etf_ticker, underlying, abs(leverage),
        f"{quarters[0].eps_yoy:.1f}%" if quarters[0].eps_yoy is not None else "n/a",
        f"{quarters[0].revenue_yoy:.1f}%" if quarters[0].revenue_yoy is not None else "n/a",
    )
    return result


def _fetch_etf_proxy_fundamentals(tk: Any, etf_ticker: str) -> list[FundamentalsQuarter]:
    """Return proxy fundamentals for an ETF.

    Single-stock leveraged/inverse ETFs use the underlying stock's earnings
    directly. Diversified/index ETFs aggregate the top holdings.
    """
    sym = etf_ticker.upper()
    if sym in _SINGLE_STOCK_LEVERAGED_ETF:
        underlying, leverage = _SINGLE_STOCK_LEVERAGED_ETF[sym]
        return _fetch_single_stock_etf_fundamentals(underlying, leverage, sym)

    holdings = _get_etf_holdings(tk, etf_ticker)
    if not holdings:
        logger.info("No holdings data for ETF %s; fundamentals unavailable", etf_ticker)
        return []

    eps_parts: list[tuple[float, float]] = []   # (weight, eps_yoy)
    rev_parts: list[tuple[float, float]] = []   # (weight, rev_yoy)
    fetched_symbols: list[str] = []

    for symbol, weight in holdings[:7]:
        try:
            quarters = fetch_us_fundamentals(symbol, limit=1, _depth=1)
        except Exception as exc:
            logger.debug("Holdings fundamentals failed for %s: %s", symbol, exc)
            continue
        if not quarters:
            continue
        q = quarters[0]
        fetched_symbols.append(symbol)
        if q.eps_yoy is not None:
            eps_parts.append((weight, q.eps_yoy))
        if q.revenue_yoy is not None:
            rev_parts.append((weight, q.revenue_yoy))

    if not fetched_symbols:
        logger.info("ETF %s: no holding fundamentals retrievable", etf_ticker)
        return []

    def _wavg(parts: list[tuple[float, float]]) -> float | None:
        if not parts:
            return None
        total_w = sum(w for w, _ in parts)
        if total_w == 0:
            return None
        return sum(w * v for w, v in parts) / total_w

    top_labels = ", ".join(fetched_symbols[:5])
    period = f"ETF:{etf_ticker}→[{top_labels}]"
    result = FundamentalsQuarter(
        period=period,
        eps_yoy=_wavg(eps_parts),
        revenue_yoy=_wavg(rev_parts),
        etf_proxy=True,
    )
    logger.info(
        "ETF %s proxy fundamentals: eps_yoy=%.1f%% rev_yoy=%.1f%% (from %s)",
        etf_ticker,
        result.eps_yoy or 0,
        result.revenue_yoy or 0,
        top_labels,
    )
    return [result]


# ── KR via KIS domestic finance API ──────────────────────────────────


def fetch_kr_fundamentals(ticker: str, kis_client: Any, limit: int = 4) -> list[FundamentalsQuarter]:
    """Pull recent quarterly EPS/revenue YoY from KIS.

    Uses the "재무비율(분기)" endpoint which already includes growth-rate
    fields, so we don't need to compute them ourselves.
    """

    try:
        raw = kis_client.get(
            "/uapi/domestic-stock/v1/finance/financial-ratio",
            {
                "FID_DIV_CLS_CODE": "1",  # 0=annual, 1=quarterly
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": ticker,
            },
            tr_id="FHKST66430300",
        )
    except Exception as exc:
        logger.warning("KIS KR fundamentals fetch failed for %s: %s", ticker, exc)
        return []

    rows = raw.get("output") or raw.get("output1") or []
    if not isinstance(rows, list) or not rows:
        return []

    out: list[FundamentalsQuarter] = []
    for row in rows[:limit]:
        period = str(row.get("stac_yymm") or row.get("stac_yr_yymm") or "").strip()
        eps_yoy = _safe_pct(row.get("eps_grtr_rt") or row.get("eps_grwth_rate"))
        rev_yoy = _safe_pct(
            row.get("sale_account_grtr_rt")
            or row.get("revenue_grtr_rt")
            or row.get("sale_account_grwth_rate")
        )
        if eps_yoy is None and rev_yoy is None:
            continue
        out.append(FundamentalsQuarter(period=period or "?", eps_yoy=eps_yoy, revenue_yoy=rev_yoy))
    return out


def _safe_pct(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
