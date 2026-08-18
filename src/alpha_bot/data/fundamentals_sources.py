"""Multi-source fundamentals resolution with an explicit fallback chain.

Fundamentals used to come from exactly one place per market, and when that
place said nothing the analyzer scored the name 0/10 — "펀더멘털 데이터
없음 — 평가 불가" — which silently caps a 30-point score at 20 and makes
``min_score`` unreachable. That is indistinguishable, from the outside,
from a genuinely weak company. Three separate ways it bit:

* the Toss provider never called yfinance at all, so every US ticker
  without a hand-made fixture file scored zero;
* yfinance rate-limits and intermittently returns empty frames;
* KR fundamentals were reachable only through a KIS client, which no
  longer exists once the account moves to Toss.

So resolution is a chain: try each source in order, take the first that
answers usefully, and **record which one answered**. A fallback that
cannot be attributed is a fallback nobody notices is broken.

Chains (first match wins):
    US   yfinance → SEC EDGAR → local fixtures
    KR   Naver Finance → KIS (when a client is supplied) → local fixtures

Results are cached on disk because fundamentals change once a quarter but
get queried every sweep; the cache is what actually removes most
rate-limit failures.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from alpha_bot.models import FundamentalsQuarter, Market

logger = logging.getLogger(__name__)

# Fundamentals move once a quarter; a day-long cache costs nothing in
# freshness and removes almost every rate-limit failure.
CACHE_TTL_SECONDS = 24 * 3600
CACHE_DIR = Path("data/cache/fundamentals")

_TIMEOUT = 15.0

# SEC rejects (403) any automated client whose User-Agent lacks a contact
# address, so this must stay email-shaped. Override with SEC_USER_AGENT to
# use your own — SEC's stated policy is that the address be reachable.
_SEC_DEFAULT_UA = "AlphaBot personal-research alphabot@example.com"


@dataclass(frozen=True)
class FundamentalsResult:
    """Quarters plus provenance — which source answered, and why others didn't."""

    quarters: list[FundamentalsQuarter]
    source: str
    attempts: list[tuple[str, str]]  # (source name, outcome)

    @property
    def ok(self) -> bool:
        return bool(self.quarters)


class FundamentalsSource(Protocol):
    name: str

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        ...


# ── HTTP helper ──────────────────────────────────────────────────────


def _get(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return response.read()


# ── yfinance (US primary) ────────────────────────────────────────────


class YFinanceSource:
    """Wraps the existing yfinance path — split-adjusted and ETF-aware."""

    name = "yfinance"

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        if market != "US":
            return []
        from alpha_bot.data.fundamentals import fetch_us_fundamentals
        return fetch_us_fundamentals(ticker, limit=limit)


# ── SEC EDGAR (US fallback) ──────────────────────────────────────────


class SecEdgarSource:
    """Official XBRL company facts — free, keyless, and deeper than yfinance.

    Uses **NetIncomeLoss, not EPS, for the earnings leg.** EDGAR serves
    as-reported figures with no split adjustment, so NVDA's 2024 Q1 EPS is
    filed as 5.98 against a post-10:1-split 0.76 a year later — a naive YoY
    reads −87% on a quarter that actually grew. Net income is immune to
    splits and tracks EPS growth closely enough for a fallback (they differ
    only by share-count drift), which is a far better trade than a
    confidently wrong number.

    Only 10-Q/10-K quarterly durations are used; Q4 is absent from XBRL
    quarterly facts by construction (it lives inside the annual figure), so
    a missing Q4 is expected rather than an error.
    """

    name = "sec-edgar"

    _EARNINGS = ("NetIncomeLoss",)
    _REVENUE = (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    )

    def __init__(self, user_agent: str | None = None):
        import os
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", _SEC_DEFAULT_UA)
        self._cik_map: dict[str, str] | None = None

    def _headers(self) -> dict[str, str]:
        # No Accept-Encoding: urllib does not transparently decompress, and
        # advertising gzip we cannot decode yields bytes that json chokes on.
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def _cik(self, ticker: str) -> str | None:
        if self._cik_map is None:
            raw = _get(
                "https://www.sec.gov/files/company_tickers.json", headers=self._headers()
            )
            rows = json.loads(raw)
            self._cik_map = {
                str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
                for row in rows.values()
            }
        # EDGAR writes class shares with a hyphen (BRK-B); venues vary.
        symbol = ticker.upper()
        return self._cik_map.get(symbol) or self._cik_map.get(symbol.replace(".", "-"))

    def _concept(self, cik: str, concept: str) -> dict[str, float]:
        """Quarter-end date → value, for genuine ~90-day durations only."""

        try:
            raw = _get(
                f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json",
                headers=self._headers(),
            )
        except Exception:
            return {}
        payload = json.loads(raw)
        out: dict[str, float] = {}
        for facts in (payload.get("units") or {}).values():
            for fact in facts:
                start, end = fact.get("start"), fact.get("end")
                if not start or not end or fact.get("form") not in {"10-Q", "10-K"}:
                    continue
                try:
                    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError:
                    continue
                if not (60 <= span <= 100):  # a quarter, not a half or a year
                    continue
                value = fact.get("val")
                if value is None:
                    continue
                # Later filings restate earlier ones; last write wins because
                # facts arrive in filing order.
                out[end] = float(value)
        return out

    def _merged(self, cik: str, concepts: tuple[str, ...]) -> dict[str, float]:
        merged: dict[str, float] = {}
        for concept in concepts:
            for end, value in self._concept(cik, concept).items():
                merged.setdefault(end, value)
        return merged

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        if market != "US":
            return []
        cik = self._cik(ticker)
        if cik is None:
            return []
        # Concepts are merged, not first-match: filers switch tags over the
        # years (NVDA reports older quarters under Revenues and newer ones
        # under RevenueFromContractWithCustomer...), so stopping at the
        # first non-empty concept leaves holes exactly where the YoY pair
        # needs both ends. Earlier entries win on conflict.
        earnings = self._merged(cik, self._EARNINGS)
        revenue = self._merged(cik, self._REVENUE)
        if not earnings and not revenue:
            return []
        return build_quarters_from_series(earnings, revenue, limit=limit)


# ── Naver Finance (KR primary) ───────────────────────────────────────


class NaverFinanceSource:
    """Scrapes the 기업실적분석 table — quarterly EPS and revenue, no key.

    Replaces the KIS-only KR path, which stops working the moment the
    account moves to Toss. Naver publishes both annual and quarterly
    columns in one table and marks estimates with "(E)"; those are dropped,
    since a forecast is not a reported quarter.
    """

    name = "naver"
    _URL = "https://finance.naver.com/item/main.naver?code={code}"

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        if market != "KR":
            return []
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.info("beautifulsoup4 not installed; skipping Naver fundamentals")
            return []

        code = ticker.upper()
        raw = _get(self._URL.format(code=code), headers={"User-Agent": "Mozilla/5.0"})
        # The page is served UTF-8 despite Naver's historically EUC-KR pages.
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        table = next(
            (
                t for t in soup.find_all("table")
                if "실적" in str(t.get("summary") or "")
            ),
            None,
        )
        if table is None:
            return []

        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        periods = [h for h in headers if re.fullmatch(r"20\d{2}\.\d{2}(\(E\))?", h)]
        if not periods:
            return []

        rows: dict[str, list[str]] = {}
        for tr in table.select("tbody tr"):
            head = tr.find("th")
            if head is None:
                continue
            rows[head.get_text(strip=True)] = [
                td.get_text(strip=True) for td in tr.find_all("td")
            ]

        eps_row = rows.get("EPS(원)") or []
        rev_row = rows.get("매출액") or []
        if not eps_row and not rev_row:
            return []

        # Annual columns come first, then quarterly; estimates are "(E)".
        quarterly = [
            (index, label) for index, label in enumerate(periods)
            if "(E)" not in label and index >= len(periods) - _count_quarterly(periods)
        ]
        earnings: dict[str, float] = {}
        revenue: dict[str, float] = {}
        for index, label in quarterly:
            end = _naver_period_end(label)
            if end is None:
                continue
            eps = _naver_number(eps_row[index] if index < len(eps_row) else "")
            rev = _naver_number(rev_row[index] if index < len(rev_row) else "")
            if eps is not None:
                earnings[end] = eps
            if rev is not None:
                revenue[end] = rev
        if not earnings and not revenue:
            return []
        return build_quarters_from_series(earnings, revenue, limit=limit)


def _count_quarterly(periods: list[str]) -> int:
    """Naver shows 4 annual columns then the quarterly ones."""
    return max(0, len(periods) - 4)


def _naver_period_end(label: str) -> str | None:
    match = re.match(r"(20\d{2})\.(\d{2})", label)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    # Quarter-end day; only ordering and YoY pairing depend on this.
    day = 31 if month in {3, 12} else 30
    return f"{year:04d}-{month:02d}-{day:02d}"


def _naver_number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "N/A"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── KIS (KR, only when a client exists) ──────────────────────────────


class KisSource:
    name = "kis"

    def __init__(self, client_factory: Callable[[], Any] | None = None):
        self._client_factory = client_factory

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        if market != "KR" or self._client_factory is None:
            return []
        from alpha_bot.data.fundamentals import fetch_kr_fundamentals
        client = self._client_factory()
        if client is None:
            return []
        return fetch_kr_fundamentals(ticker, client, limit=limit)


# ── Local fixtures (last resort, both markets) ───────────────────────


class FixtureSource:
    name = "fixture"

    def __init__(self, data_dir: Path = Path("data")):
        self.data_dir = data_dir

    def fetch(self, ticker: str, market: Market, limit: int) -> list[FundamentalsQuarter]:
        path = self.data_dir / "fundamentals" / f"{market}_{ticker.upper()}.json"
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [FundamentalsQuarter.from_mapping(row) for row in rows][:limit]


# ── Shared quarter builder ───────────────────────────────────────────


def build_quarters_from_series(
    earnings: dict[str, float],
    revenue: dict[str, float],
    *,
    limit: int = 4,
) -> list[FundamentalsQuarter]:
    """Turn {quarter-end: value} series into YoY quarters, newest first.

    Pairs each quarter with the one four quarters earlier **by date**, not
    by list position: sources skip quarters (EDGAR has no standalone Q4),
    and positional pairing would silently compare Q1 against Q2.
    """

    ends = sorted(set(earnings) | set(revenue), reverse=True)
    out: list[FundamentalsQuarter] = []
    for end in ends[: limit + 4]:
        prior = _year_earlier(end, ends)
        eps_yoy = _yoy(earnings.get(end), earnings.get(prior) if prior else None)
        rev_yoy = _yoy(revenue.get(end), revenue.get(prior) if prior else None)
        if eps_yoy is None and rev_yoy is None:
            continue
        out.append(
            FundamentalsQuarter(
                period=_quarter_label(end),
                eps_yoy=eps_yoy,
                revenue_yoy=rev_yoy,
                reported_at=_parse_date(end),
            )
        )
        if len(out) >= limit:
            break
    return out


def _year_earlier(end: str, candidates: list[str]) -> str | None:
    """The candidate closest to one year before ``end`` (within ±45 days)."""

    target = _parse_date(end)
    if target is None:
        return None
    try:
        wanted = target.replace(year=target.year - 1)
    except ValueError:  # 29 Feb
        wanted = target.replace(year=target.year - 1, day=28)
    best, best_gap = None, 46
    for candidate in candidates:
        parsed = _parse_date(candidate)
        if parsed is None:
            continue
        gap = abs((parsed - wanted).days)
        if gap < best_gap:
            best, best_gap = candidate, gap
    return best


# A year-over-year multiple beyond this is a base effect, not operating
# growth: no company of any size sustainably earns 20x what it did a year
# ago. NVDA's genuine 2024 surge was ~7x, so the bar sits well clear of
# real outliers while rejecting recoveries off a rounding-error quarter.
_YOY_MAX_MULTIPLE = 20.0


def _yoy(now: float | None, prior: float | None) -> float | None:
    """Year-over-year percent, refusing comparisons that carry no meaning.

    Two ways the arithmetic lies. A non-positive prior quarter (a loss)
    makes the percentage meaningless in sign as well as magnitude. And a
    prior that is merely *tiny relative to now* produces figures like
    Ford's +3200% — the scorer's top tier starts at 25%, so pure noise
    would collect full marks. The guard is a ratio rather than an absolute
    floor because this function sees USD, KRW and 억-won alike.
    """

    if now is None or prior is None:
        return None
    if prior <= 0:
        return None
    if abs(now) > abs(prior) * _YOY_MAX_MULTIPLE:
        return None
    return (now - prior) / abs(prior) * 100.0


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _quarter_label(end: str) -> str:
    parsed = _parse_date(end)
    if parsed is None:
        return end
    return f"{parsed.year}Q{(parsed.month - 1) // 3 + 1}"


# ── Cache ────────────────────────────────────────────────────────────


def _cache_path(ticker: str, market: Market, cache_dir: Path) -> Path:
    return cache_dir / f"{market}_{ticker.upper()}.json"


def _read_cache(
    ticker: str, market: Market, cache_dir: Path, ttl: float
) -> FundamentalsResult | None:
    path = _cache_path(ticker, market, cache_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (time.time() - float(payload["fetched_epoch"])) > ttl:
            return None
        quarters = [FundamentalsQuarter.from_mapping(r) for r in payload["quarters"]]
    except Exception:
        return None
    if not quarters:
        return None
    return FundamentalsResult(quarters, f"{payload.get('source', '?')}(cache)", [])


def _write_cache(
    ticker: str, market: Market, cache_dir: Path, result: FundamentalsResult
) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(ticker, market, cache_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "ticker": ticker.upper(),
                    "market": market,
                    "source": result.source,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "fetched_epoch": time.time(),
                    "quarters": [q.to_dict() for q in result.quarters],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:  # caching must never break analysis
        logger.debug("Fundamentals cache write failed for %s: %s", ticker, exc)


# ── Resolution ───────────────────────────────────────────────────────


def default_sources(
    market: Market,
    *,
    data_dir: Path = Path("data"),
    kis_client_factory: Callable[[], Any] | None = None,
) -> list[FundamentalsSource]:
    if market == "US":
        return [YFinanceSource(), SecEdgarSource(), FixtureSource(data_dir)]
    return [NaverFinanceSource(), KisSource(kis_client_factory), FixtureSource(data_dir)]


def resolve_fundamentals(
    ticker: str,
    market: Market,
    *,
    limit: int = 4,
    sources: list[FundamentalsSource] | None = None,
    data_dir: Path = Path("data"),
    kis_client_factory: Callable[[], Any] | None = None,
    cache_dir: Path | None = None,
    cache_ttl: float = CACHE_TTL_SECONDS,
    use_cache: bool = True,
) -> FundamentalsResult:
    """First source that answers wins. Never raises."""

    cache_dir = cache_dir or CACHE_DIR
    if use_cache:
        cached = _read_cache(ticker, market, cache_dir, cache_ttl)
        if cached is not None:
            return cached

    chain = sources if sources is not None else default_sources(
        market, data_dir=data_dir, kis_client_factory=kis_client_factory
    )
    attempts: list[tuple[str, str]] = []
    for source in chain:
        try:
            quarters = source.fetch(ticker, market, limit)
        except Exception as exc:
            attempts.append((source.name, f"error: {exc}"))
            logger.info("Fundamentals source %s failed for %s: %s", source.name, ticker, exc)
            continue
        if quarters:
            attempts.append((source.name, f"ok: {len(quarters)}분기"))
            result = FundamentalsResult(quarters, source.name, attempts)
            if use_cache:
                _write_cache(ticker, market, cache_dir, result)
            return result
        attempts.append((source.name, "empty"))

    logger.warning(
        "No fundamentals for %s:%s — tried %s",
        market, ticker, ", ".join(f"{n}({o})" for n, o in attempts),
    )
    return FundamentalsResult([], "none", attempts)
