"""KIS real-time WebSocket stream (KR equities, Phase 3a).

Connects to the KIS websocket gateway, subscribes to 실시간 체결가
(``H0STCNT0``) for a set of tickers, and delivers parsed :class:`Tick`
objects to a callback. Design goals:

  * **Parsing is pure.** ``parse_frame`` / ``build_subscribe_frame`` take
    and return plain values so the wire protocol is fully unit-testable
    without a socket. The socket-owning ``KisStreamClient`` stays thin.
  * **Fail-safe loop.** Auto-reconnect with capped exponential backoff,
    resubscribe of the desired ticker set on every (re)connect, and
    PINGPONG echo. A stream drop must never take the bot down — consumers
    (e.g. the live exit monitor) fall back to REST polling cadence.
  * **Paper-friendly.** URLs/keys follow ``KIS_MODE`` exactly like the
    REST client: paper → ``ops.koreainvestment.com:31000``, live →
    ``:21000``. The approval key comes from ``/oauth2/Approval``.

Field indices for H0STCNT0 follow the KIS 실시간 국내주식 체결가 spec
(caret-separated). Verify once against live paper data during the first
장중 검증 session — the indices are constants below, trivially fixable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from alpha_bot.broker.kis import KisRestClient, KisSettings
from alpha_bot.errors import BrokerError

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

# WebSocket gateway per mode (KIS open-trading-api reference).
_WS_URLS = {
    "paper": "ws://ops.koreainvestment.com:31000",
    "live": "ws://ops.koreainvestment.com:21000",
}

TR_KR_TICK = "H0STCNT0"  # 국내주식 실시간체결가

# H0STCNT0 caret-field indices (per KIS 실시간 시세 스펙).
_F_TICKER = 0       # MKSC_SHRN_ISCD  유가증권 단축 종목코드
_F_TIME = 1         # STCK_CNTG_HOUR  체결시간 HHMMSS
_F_PRICE = 2        # STCK_PRPR       현재가(체결가)
_F_TICK_VOLUME = 12  # CNTG_VOL       체결 거래량
_F_CUM_VOLUME = 13   # ACML_VOL       누적 거래량
_MIN_FIELDS = 14


@dataclass(frozen=True)
class Tick:
    """One trade print from the exchange."""

    ticker: str
    time: datetime      # exchange-local (KST), today's session
    price: float
    volume: int         # shares in this print
    cum_volume: int     # session-cumulative shares


def build_subscribe_frame(approval_key: str, tr_id: str, tr_key: str, *, subscribe: bool = True) -> str:
    """Subscription (or unsubscription) request frame, as JSON text."""
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
    )


def _tick_datetime(hhmmss: str, *, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(_KST)
    text = hhmmss.strip().zfill(6)
    return moment.replace(
        hour=int(text[0:2]), minute=int(text[2:4]), second=int(text[4:6]), microsecond=0
    )


def parse_frame(raw: str, *, now: datetime | None = None) -> tuple[str, object]:
    """Classify one websocket text frame.

    Returns ``(kind, payload)``:
      * ``("ticks", list[Tick])`` — H0STCNT0 data frame (may carry several
        records: ``암호화|TR_ID|건수|필드^필드^…`` with fields of all
        records concatenated in one caret stream);
      * ``("pingpong", raw)`` — keepalive; the caller must echo ``raw``;
      * ``("control", dict)`` — subscribe acks / notices (JSON);
      * ``("unknown", raw)`` — anything unparseable (log and move on).
    """
    text = raw.strip()
    if not text:
        return "unknown", raw

    if text[0] in "{[":  # JSON control plane
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return "unknown", raw
        tr_id = str(((payload.get("header") or {}).get("tr_id")) or "")
        if tr_id == "PINGPONG":
            return "pingpong", raw
        return "control", payload

    parts = text.split("|", 3)
    if len(parts) != 4:
        return "unknown", raw
    encrypted, tr_id, count_text, blob = parts
    if encrypted != "0":
        # Encrypted frames (체결통보 etc.) are out of scope for the price
        # stream; surface as control-ish noise rather than crashing.
        return "unknown", raw
    if tr_id != TR_KR_TICK:
        return "unknown", raw
    try:
        count = max(1, int(count_text))
    except ValueError:
        return "unknown", raw

    fields = blob.split("^")
    per_record = len(fields) // count
    if per_record < _MIN_FIELDS:
        return "unknown", raw

    ticks: list[Tick] = []
    for i in range(count):
        record = fields[i * per_record:(i + 1) * per_record]
        try:
            ticks.append(
                Tick(
                    ticker=record[_F_TICKER],
                    time=_tick_datetime(record[_F_TIME], now=now),
                    price=float(record[_F_PRICE]),
                    volume=int(float(record[_F_TICK_VOLUME])),
                    cum_volume=int(float(record[_F_CUM_VOLUME])),
                )
            )
        except (ValueError, IndexError) as exc:
            logger.warning("Unparseable tick record (%s): %.120s", exc, "^".join(record))
    return "ticks", ticks


class KisStreamClient:
    """Threaded KIS websocket consumer with reconnect + resubscribe.

    Usage::

        client = KisStreamClient(on_tick=cache.update)
        client.subscribe("005930")
        client.start()
        ...
        client.stop()
    """

    _BACKOFF_BASE = 2.0
    _BACKOFF_MAX = 60.0

    def __init__(
        self,
        settings: KisSettings | None = None,
        *,
        on_tick: Callable[[Tick], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.settings = settings or KisSettings.from_env()
        self.on_tick = on_tick or (lambda tick: None)
        self.on_status = on_status or (lambda msg: logger.info("stream: %s", msg))
        self._desired: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None  # type: ignore[assignment]
        self._approval_key: str | None = None

    # ── Subscription management ──────────────────────────────────────

    def subscribe(self, ticker: str) -> None:
        ticker = ticker.strip()
        with self._lock:
            if ticker in self._desired:
                return
            self._desired.add(ticker)
            ws = self._ws
        if ws is not None and self._approval_key:
            self._send_subscribe(ws, ticker, subscribe=True)

    def unsubscribe(self, ticker: str) -> None:
        ticker = ticker.strip()
        with self._lock:
            if ticker not in self._desired:
                return
            self._desired.discard(ticker)
            ws = self._ws
        if ws is not None and self._approval_key:
            self._send_subscribe(ws, ticker, subscribe=False)

    def desired(self) -> set[str]:
        with self._lock:
            return set(self._desired)

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="kis-stream")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    # ── Internals ────────────────────────────────────────────────────

    def _fetch_approval_key(self) -> str:
        """POST /oauth2/Approval → websocket approval key.

        Note: this endpoint takes ``secretkey`` (not ``appsecret``).
        """
        client = KisRestClient(self.settings)
        response = client.post(
            "/oauth2/Approval",
            {
                "grant_type": "client_credentials",
                "appkey": self.settings.app_key,
                "secretkey": self.settings.app_secret,
            },
            include_auth=False,
        )
        key = response.get("approval_key")
        if not key:
            raise BrokerError(f"KIS approval-key response missing approval_key: {response}")
        return str(key)

    def _send_subscribe(self, ws, ticker: str, *, subscribe: bool) -> None:
        try:
            ws.send(build_subscribe_frame(self._approval_key or "", TR_KR_TICK, ticker, subscribe=subscribe))
            self.on_status(f"{'구독' if subscribe else '구독 해지'}: {ticker}")
        except Exception as exc:
            logger.warning("Subscribe frame send failed for %s: %s", ticker, exc)

    def _run_loop(self) -> None:
        import websocket  # websocket-client; imported lazily so tests don't need it

        url = _WS_URLS.get(self.settings.mode, _WS_URLS["paper"])
        attempt = 0
        while not self._stop.is_set():
            try:
                self._approval_key = self._fetch_approval_key()
            except Exception as exc:
                self.on_status(f"approval_key 발급 실패: {exc}")
                if self._stop.wait(min(self._BACKOFF_MAX, self._BACKOFF_BASE * (2 ** attempt))):
                    return
                attempt += 1
                continue

            def on_open(ws) -> None:
                self.on_status(f"연결됨 ({url}, mode={self.settings.mode})")
                for ticker in sorted(self.desired()):
                    self._send_subscribe(ws, ticker, subscribe=True)

            def on_message(ws, raw: str) -> None:
                kind, payload = parse_frame(raw)
                if kind == "ticks":
                    for tick in payload:  # type: ignore[union-attr]
                        try:
                            self.on_tick(tick)
                        except Exception:
                            logger.exception("on_tick callback failed")
                elif kind == "pingpong":
                    try:
                        ws.send(payload)  # echo keepalive verbatim
                    except Exception as exc:
                        logger.warning("PINGPONG echo failed: %s", exc)
                elif kind == "control":
                    body = (payload or {}).get("body") or {}  # type: ignore[union-attr]
                    msg = body.get("msg1") or ""
                    if msg:
                        self.on_status(f"제어 응답: {msg}")
                else:
                    logger.debug("Unknown stream frame: %.120s", raw)

            def on_error(ws, error) -> None:
                logger.warning("Stream error: %s", error)

            ws_app = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_message, on_error=on_error
            )
            with self._lock:
                self._ws = ws_app
            attempt = 0
            try:
                ws_app.run_forever()
            except Exception as exc:
                logger.warning("Stream run_forever crashed: %s", exc)
            finally:
                with self._lock:
                    self._ws = None

            if self._stop.is_set():
                return
            delay = min(self._BACKOFF_MAX, self._BACKOFF_BASE * (2 ** attempt))
            self.on_status(f"연결 끊김 — {delay:.0f}s 후 재접속")
            if self._stop.wait(delay):
                return
            attempt += 1
