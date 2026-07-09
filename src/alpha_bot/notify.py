"""Best-effort operator notifications via Telegram.

Configured entirely from the environment / ``.env``:

    TELEGRAM_BOT_TOKEN=123456:ABC...   (from @BotFather)
    TELEGRAM_CHAT_ID=987654321         (your chat / group id)

When either is missing this module is a silent no-op, matching the
project-wide rule that observability must never break trading. Failures
are logged and swallowed. Repeated identical messages are de-duplicated
for ``dedupe_ttl`` seconds so a 5-minute auto-pilot loop can't spam the
same circuit-breaker alert all day.

Uses stdlib urllib only (no ``requests`` dependency), same as broker/kis.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from alpha_bot.config import load_dotenv

logger = logging.getLogger(__name__)

_DEDUPE_LOCK = threading.Lock()
_LAST_SENT: dict[str, float] = {}  # dedupe key → monotonic timestamp
_DEFAULT_DEDUPE_TTL = 1800  # seconds


def is_configured() -> bool:
    load_dotenv()
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    )


def notify(
    message: str,
    *,
    dedupe_key: str | None = None,
    dedupe_ttl: int = _DEFAULT_DEDUPE_TTL,
) -> bool:
    """Send ``message`` to the configured Telegram chat.

    Returns True only when the API confirmed delivery. Never raises.
    ``dedupe_key`` (defaults to the message text) suppresses identical
    alerts within ``dedupe_ttl`` seconds.
    """
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    key = dedupe_key or message
    now = time.monotonic()
    with _DEDUPE_LOCK:
        last = _LAST_SENT.get(key)
        if last is not None and (now - last) < dedupe_ttl:
            return False
        _LAST_SENT[key] = now

    payload = json.dumps(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        ok = bool(body.get("ok"))
        if not ok:
            logger.warning("Telegram send rejected: %s", body)
        return ok
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Telegram send failed: %s", exc)
        # Allow a retry on the next event rather than waiting out the TTL.
        with _DEDUPE_LOCK:
            _LAST_SENT.pop(key, None)
        return False
