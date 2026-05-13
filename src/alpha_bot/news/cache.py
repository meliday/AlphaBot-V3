"""Time-based cache for LLM news assessments.

News headlines do not change minute-to-minute, but every scan currently
triggers a fresh LLM call. This cache keeps the last assessment per
``(market, ticker)`` on disk so repeated scans within ``ttl_seconds``
reuse the prior result and avoid burning OpenAI tokens.

Only assessments produced by the LLM are cached. Neutral fallbacks
(missing key / network failure / parse error) are not cached so the next
scan gets a real chance at calling the LLM again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from alpha_bot.models import Market, NewsAssessment

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600
DEFAULT_PATH = Path("news_cache.json")

_LOCK = threading.Lock()


def cache_path() -> Path:
    return Path(os.environ.get("NEWS_CACHE_PATH", str(DEFAULT_PATH)))


def cache_ttl_seconds() -> int:
    raw = os.environ.get("NEWS_CACHE_TTL_SECONDS")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _key(ticker: str, market: Market) -> str:
    return f"{market}:{ticker}"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("news cache read failed (%s): %s", path, exc)
        return {}


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("news cache write failed (%s): %s", path, exc)


def load(ticker: str, market: Market) -> NewsAssessment | None:
    """Return a cached assessment if still within TTL, else ``None``."""
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return None
    path = cache_path()
    with _LOCK:
        data = _read(path)
    entry = data.get(_key(ticker, market))
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cached_at"])
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    age = (datetime.now(timezone.utc) - cached_at).total_seconds()
    if age > ttl:
        return None
    payload = entry.get("assessment")
    if not isinstance(payload, dict):
        return None
    try:
        return NewsAssessment(**payload)
    except TypeError:
        return None


def save(ticker: str, market: Market, assessment: NewsAssessment) -> None:
    """Persist ``assessment``. Neutral fallbacks (source='default') are skipped."""
    if assessment.source == "default":
        return
    path = cache_path()
    with _LOCK:
        data = _read(path)
        data[_key(ticker, market)] = {
            "cached_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "assessment": asdict(assessment),
        }
        _write(path, data)
