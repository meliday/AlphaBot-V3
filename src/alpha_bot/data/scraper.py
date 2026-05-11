"""News scrapers used by the LLM news assessor.

Both fetchers return a single human-readable string of bullet-pointed recent
headlines (with optional summary + date + source). On any failure the message
is non-fatal and degraded — the assessor will fall back to a neutral verdict.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_TIMEOUT = 6


def fetch_news(ticker: str, market: str) -> str:
    market = (market or "").upper()
    if market == "US":
        return fetch_us_news(ticker)
    if market == "KR":
        return fetch_kr_news(ticker)
    return "지원하지 않는 시장입니다."


# ── US: yfinance (post-1.3.0 schema) ─────────────────────────────────


def fetch_us_news(ticker: str, limit: int = 8) -> str:
    try:
        import yfinance as yf
    except ImportError:
        return "yfinance 패키지가 설치되어 있지 않습니다."

    try:
        items = yf.Ticker(ticker).news or []
    except Exception as exc:
        logger.warning("yfinance news fetch failed for %s: %s", ticker, exc)
        return f"뉴스 수집 실패: {exc}"

    lines: list[str] = []
    for raw in items[:limit]:
        # New schema (yfinance >= 0.2.41): each item is {id, content: {...}}
        # Old schema kept some top-level keys; we read both for resilience.
        content = raw.get("content") if isinstance(raw, dict) else None
        if isinstance(content, dict):
            title = (content.get("title") or "").strip()
            summary = (content.get("summary") or content.get("description") or "").strip()
            provider = (content.get("provider") or {}).get("displayName", "")
            date = (content.get("displayTime") or content.get("pubDate") or "")[:10]
        else:
            title = str(raw.get("title", "")).strip()
            summary = str(raw.get("summary", "")).strip()
            provider = str(raw.get("publisher", ""))
            date = ""

        if not title:
            continue
        bullet = f"- [{date}|{provider}] {title}" if date or provider else f"- {title}"
        if summary:
            bullet += f"\n  · {_truncate(summary, 240)}"
        lines.append(bullet)

    if not lines:
        return "최근 뉴스 없음."
    return "\n".join(lines)


# ── KR: Naver mobile stock news JSON API ────────────────────────────


def fetch_kr_news(ticker: str, limit: int = 8) -> str:
    """Pull recent news from the Naver mobile stock API.

    Endpoint: https://m.stock.naver.com/api/news/stock/{6-digit code}
    Response is a list of pages; each page has an ``items`` array with
    ``title``, ``body``, ``officeName``, ``datetime`` (YYYYMMDDHHmm).
    """

    code = "".join(ch for ch in ticker if ch.isdigit())
    if not code:
        return f"잘못된 한국 종목코드: {ticker}"

    url = f"https://m.stock.naver.com/api/news/stock/{code}"
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://m.stock.naver.com/domestic/stock/{code}/news",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
        pages = json.loads(raw)
    except Exception as exc:
        logger.warning("Naver KR news fetch failed for %s: %s", ticker, exc)
        return f"뉴스 수집 실패: {exc}"

    items: list[dict] = []
    if isinstance(pages, list):
        for page in pages:
            items.extend(page.get("items", []) if isinstance(page, dict) else [])
    elif isinstance(pages, dict):
        items = pages.get("items", [])

    lines: list[str] = []
    for item in items[:limit]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        body = (item.get("body") or "").strip()
        office = (item.get("officeName") or "").strip()
        when = _format_naver_datetime(item.get("datetime", ""))
        bullet = f"- [{when}|{office}] {title}" if (when or office) else f"- {title}"
        if body:
            bullet += f"\n  · {_truncate(body, 240)}"
        lines.append(bullet)

    if not lines:
        return "최근 뉴스 없음."
    return "\n".join(lines)


# ── helpers ──────────────────────────────────────────────────────────


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _format_naver_datetime(s: str) -> str:
    s = str(s or "")
    if len(s) >= 12:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    if len(s) >= 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s
