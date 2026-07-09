from __future__ import annotations

import csv
import json
import logging
import math
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from alpha_bot.broker.kis import KisRestClient, KisSettings
from alpha_bot.errors import DataError
from alpha_bot.models import Candle, Catalyst, FundamentalsQuarter, Market, MarketContext
from alpha_bot.utils import infer_us_exchange_code, safe_float

logger = logging.getLogger(__name__)


class DataProvider(Protocol):
    def get_candles(self, ticker: str, market: Market, lookback: int = 260) -> list[Candle]:
        ...

    def get_fundamentals(self, ticker: str, market: Market) -> list[FundamentalsQuarter]:
        ...

    def get_catalysts(self, ticker: str, market: Market) -> list[Catalyst]:
        ...

    def get_market_context(self, ticker: str, market: Market) -> MarketContext:
        ...

    def get_current_price(self, ticker: str, market: Market) -> float | None:
        """Real-time (or freshest available) price; None when unavailable.

        Exit monitoring prefers this over the last daily candle — a daily
        close lags an intraday crash by hours, which turns a planned −7%
        stop into a much larger realized loss.
        """
        ...


class FixtureDataProvider:
    """Reads user-maintained CSV/JSON data from a local directory."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def get_candles(self, ticker: str, market: Market, lookback: int = 260) -> list[Candle]:
        path = self.data_dir / "prices" / f"{market}_{ticker.upper()}.csv"
        if not path.exists():
            raise DataError(f"Missing price file: {path}. Use --demo or add local data.")
        logger.info("Loading candles from %s", path)
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [Candle.from_mapping(row) for row in csv.DictReader(handle)]
        rows.sort(key=lambda candle: candle.date)
        if len(rows) < min(lookback, 220):
            raise DataError(f"Need at least 220 candles for 200-day trend filter; got {len(rows)}.")
        return rows[-lookback:]

    def get_fundamentals(self, ticker: str, market: Market) -> list[FundamentalsQuarter]:
        path = self.data_dir / "fundamentals" / f"{market}_{ticker.upper()}.json"
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [FundamentalsQuarter.from_mapping(row) for row in rows]

    def get_catalysts(self, ticker: str, market: Market) -> list[Catalyst]:
        path = self.data_dir / "news" / f"{market}_{ticker.upper()}.json"
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [Catalyst.from_mapping(row) for row in rows]

    def get_market_context(self, ticker: str, market: Market) -> MarketContext:
        path = self.data_dir / "contexts" / f"{market}_{ticker.upper()}.json"
        if not path.exists():
            return MarketContext()
        return MarketContext.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    def get_current_price(self, ticker: str, market: Market) -> float | None:
        try:
            candles = self.get_candles(ticker, market, lookback=5)
        except DataError:
            return None
        return candles[-1].close if candles else None


class SyntheticDataProvider:
    """Deterministic demo data for local dry-runs and tests."""

    def get_candles(self, ticker: str, market: Market, lookback: int = 260) -> list[Candle]:
        seed = sum(ord(char) for char in f"{market}:{ticker.upper()}")
        rng = random.Random(seed)
        base = 50 + (seed % 90)
        if market == "KR":
            base *= 650
        start = date.today() - timedelta(days=int(lookback * 1.6) + 20)
        candles: list[Candle] = []
        price = float(base)
        trading_days = 0
        day = start
        while len(candles) < lookback:
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            trading_days += 1
            drift = 0.0018
            noise = rng.uniform(-0.018, 0.018)
            if trading_days > lookback - 60:
                # Last leg is constructive but compressed to make demo VCP behavior visible.
                noise *= max(0.25, (lookback - trading_days + 10) / 60)
            price = max(1.0, price * (1 + drift + noise))
            intraday = price * rng.uniform(0.006, 0.022)
            close = round(price, 2)
            open_price = round(close * (1 + rng.uniform(-0.008, 0.008)), 2)
            high = round(max(open_price, close) + intraday, 2)
            low = round(max(0.5, min(open_price, close) - intraday), 2)
            volume_base = 1_000_000 + (seed % 350_000)
            volume_decay = 1.0 if trading_days < lookback - 60 else 0.86
            volume = int(volume_base * volume_decay * (1 + rng.uniform(-0.25, 0.35)))
            candles.append(Candle(day, open_price, high, low, close, volume))
        return candles

    def get_fundamentals(self, ticker: str, market: Market) -> list[FundamentalsQuarter]:
        seed = sum(ord(char) for char in f"{market}:{ticker.upper()}")
        eps = 18 + (seed % 25)
        revenue = 14 + (seed % 22)
        return [
            FundamentalsQuarter("latest", eps_yoy=float(eps), revenue_yoy=float(revenue), eps_surprise=3.5),
            FundamentalsQuarter("previous", eps_yoy=max(1.0, eps - 6.0), revenue_yoy=max(1.0, revenue - 5.0)),
        ]

    def get_catalysts(self, ticker: str, market: Market) -> list[Catalyst]:
        theme = "AI infrastructure demand" if market == "US" else "sector leadership and institutional interest"
        return [
            Catalyst(
                headline=f"{ticker.upper()} demo catalyst",
                summary=f"Deterministic demo catalyst: {theme}. Replace with verified news before trading.",
                source="synthetic-demo",
                published_at=date.today(),
            )
        ]

    def get_market_context(self, ticker: str, market: Market) -> MarketContext:
        seed = sum(ord(char) for char in f"{market}:{ticker.upper()}")
        stock = 8.0 + (seed % 12)
        benchmark = 3.0 + (seed % 5)
        sector = "AI / semiconductors" if market == "US" else "KOSPI/KOSDAQ leadership candidate"
        return MarketContext(
            benchmark_return_3m=benchmark,
            stock_return_3m=stock,
            sector_theme=sector,
            foreign_flow="positive" if market == "KR" else "not tracked",
            institutional_flow="neutral-to-positive",
            broker_coverage="manual verification required",
        )

    def get_current_price(self, ticker: str, market: Market) -> float | None:
        candles = self.get_candles(ticker, market, lookback=5)
        return candles[-1].close if candles else None


class KisPriceDataProvider:
    """KIS REST daily-price provider with local fundamental/news fallback."""

    def __init__(self, fallback: DataProvider | None = None):
        self.client = KisRestClient(KisSettings.from_env())
        self.fallback = fallback

    def get_candles(self, ticker: str, market: Market, lookback: int = 260) -> list[Candle]:
        if market == "KR":
            candles = self._get_domestic_candles(ticker, lookback)
        elif market == "US":
            candles = self._get_us_candles(ticker, lookback)
        else:
            raise DataError(f"Unsupported market: {market}")
        if len(candles) < 20:
            raise DataError(
                f"KIS returned only {len(candles)} candles for {market}:{ticker}; "
                "need at least 20 for basic analysis (try a more established ticker "
                "or check that the exchange code is correct)."
            )
        return candles[-lookback:]

    def get_fundamentals(self, ticker: str, market: Market) -> list[FundamentalsQuarter]:
        live: list[FundamentalsQuarter] = []
        try:
            from alpha_bot.data.fundamentals import (
                fetch_kr_fundamentals,
                fetch_us_fundamentals,
            )
            if market == "US":
                live = fetch_us_fundamentals(ticker)
            elif market == "KR":
                live = fetch_kr_fundamentals(ticker, self.client)
        except Exception as exc:
            logger.warning("Live fundamentals fetch failed for %s:%s: %s", market, ticker, exc)
        if live:
            return live
        return self.fallback.get_fundamentals(ticker, market) if self.fallback else []

    def get_catalysts(self, ticker: str, market: Market) -> list[Catalyst]:
        return self.fallback.get_catalysts(ticker, market) if self.fallback else []

    def get_market_context(self, ticker: str, market: Market) -> MarketContext:
        return self.fallback.get_market_context(ticker, market) if self.fallback else MarketContext()

    def get_current_price(self, ticker: str, market: Market) -> float | None:
        """Live quote via KIS. Returns None on any failure so callers can
        fall back to the last daily close."""
        try:
            if market == "KR":
                raw = self.client.get(
                    "/uapi/domestic-stock/v1/quotations/inquire-price",
                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
                    tr_id="FHKST01010100",
                )
                output = raw.get("output", {}) or {}
                price = float(output.get("stck_prpr") or 0)
                return price if price > 0 else None
            if market == "US":
                from alpha_bot.utils import _US_EXCD_CACHE
                excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
                cached = _US_EXCD_CACHE.get(ticker.upper())
                api_excd = excd_map.get(cached or infer_us_exchange_code(ticker), "NAS")
                raw = self.client.get(
                    "/uapi/overseas-price/v1/quotations/price",
                    {"AUTH": "", "EXCD": api_excd, "SYMB": ticker.upper()},
                    tr_id="HHDFS00000300",
                )
                output = raw.get("output", {}) or {}
                price = float(output.get("last") or 0)
                return price if price > 0 else None
        except Exception as exc:
            logger.warning("Live quote failed for %s:%s: %s", market, ticker, exc)
        return None

    def _get_domestic_candles(self, ticker: str, lookback: int) -> list[Candle]:
        end_date = date.today()
        start_date = end_date - timedelta(days=int(lookback * 1.7) + 30)

        all_candles: list[Candle] = []
        current_end = end_date

        while len(all_candles) < lookback and current_end > start_date:
            raw = self.client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
                tr_id="FHKST03010100",
            )
            rows = raw.get("output2", [])
            if not rows:
                break

            batch = []
            for row in rows:
                if not row.get("stck_bsop_date"):
                    continue
                batch.append(
                    Candle(
                        date=_parse_yyyymmdd(row["stck_bsop_date"]),
                        open=float(row["stck_oprc"]),
                        high=float(row["stck_hgpr"]),
                        low=float(row["stck_lwpr"]),
                        close=float(row["stck_clpr"]),
                        volume=int(float(row["acml_vol"])),
                    )
                )

            if not batch:
                break

            all_candles.extend(batch)
            oldest_date_in_batch = min(c.date for c in batch)
            current_end = oldest_date_in_batch - timedelta(days=1)

        all_candles.sort(key=lambda candle: candle.date)
        unique_candles = []
        seen = set()
        for c in all_candles:
            if c.date not in seen:
                seen.add(c.date)
                unique_candles.append(c)
        return unique_candles

    def _get_us_candles(self, ticker: str, lookback: int) -> list[Candle]:
        """Fetch US candles, probing all major exchanges until one returns data.

        KIS routes US queries via EXCD={NAS, NYS, AMS}. Many tickers (notably
        Arca-listed ETFs like SOXL, VOO, ARKK) are not on the NASDAQ feed, so
        the previous static `infer_us_exchange_code` fallback returned 0 rows.
        We now probe candidates in order and remember the working one.
        """
        # Try cached exchange first, then probe the rest
        from alpha_bot.utils import _US_EXCD_CACHE
        sym = ticker.upper()
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        order = ["NASD", "NYSE", "AMEX"]
        cached = _US_EXCD_CACHE.get(sym)
        if cached and cached in order:
            order.remove(cached)
            order.insert(0, cached)
        else:
            # Static hint from the small whitelist, then fall through to others.
            hint = infer_us_exchange_code(ticker)
            if hint in order and order[0] != hint:
                order.remove(hint)
                order.insert(0, hint)

        for full_excd in order:
            api_excd = excd_map[full_excd]
            candles = self._fetch_us_exchange(ticker, api_excd, lookback)
            if candles:
                _US_EXCD_CACHE[sym] = full_excd
                logger.info("KIS US %s resolved to %s (%d candles)", sym, full_excd, len(candles))
                return candles
            logger.debug("KIS US %s on %s returned 0 rows; trying next", sym, full_excd)

        logger.warning("KIS US %s: no candles on any exchange (NAS/NYS/AMS)", sym)
        return []

    def _fetch_us_exchange(self, ticker: str, api_excd: str, lookback: int) -> list[Candle]:
        all_candles: list[Candle] = []
        current_bymd = ""  # 빈 값이면 오늘 기준 최근 100일

        while len(all_candles) < lookback:
            raw = self.client.get(
                "/uapi/overseas-price/v1/quotations/dailyprice",
                {
                    "AUTH": "",
                    "EXCD": api_excd,
                    "SYMB": ticker,
                    "GUBN": "0",
                    "BYMD": current_bymd,
                    "MODP": "1",  # 1: 수정주가
                },
                tr_id="HHDFS76240000",
            )
            rows = raw.get("output2", [])
            if not rows:
                break

            batch = []
            for row in rows:
                raw_date = row.get("xymd") or row.get("stck_bsop_date")
                if not raw_date:
                    continue
                try:
                    batch.append(
                        Candle(
                            date=_parse_yyyymmdd(raw_date),
                            open=safe_float(row, "open", "ovrs_nmix_oprc", "stck_oprc"),
                            high=safe_float(row, "high", "ovrs_nmix_hgpr", "stck_hgpr"),
                            low=safe_float(row, "low", "ovrs_nmix_lwpr", "stck_lwpr"),
                            close=safe_float(row, "clos", "last", "stck_clpr"),
                            volume=int(safe_float(row, "tvol", "acml_vol")),
                        )
                    )
                except DataError:
                    logger.warning("Skipping unparseable US candle row: %s", row)

            if not batch:
                break

            all_candles.extend(batch)
            oldest_date_in_batch = min(c.date for c in batch)
            current_bymd = (oldest_date_in_batch - timedelta(days=1)).strftime("%Y%m%d")

        all_candles.sort(key=lambda candle: candle.date)
        unique_candles = []
        seen = set()
        for c in all_candles:
            if c.date not in seen:
                seen.add(c.date)
                unique_candles.append(c)
        return unique_candles


def compound_return(candles: list[Candle], periods: int) -> float | None:
    if len(candles) <= periods:
        return None
    start = candles[-periods - 1].close
    if math.isclose(start, 0.0):
        return None
    return (candles[-1].close / start - 1) * 100


def _parse_yyyymmdd(value: str) -> date:
    text = str(value)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
