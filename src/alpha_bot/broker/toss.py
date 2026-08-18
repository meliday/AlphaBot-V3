from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from alpha_bot.config import load_dotenv
from alpha_bot.errors import BrokerError, BrokerOrderRejected
from alpha_bot.models import (
    AccountBalance,
    Market,
    OrderFill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
)
from alpha_bot.pricing import round_aggressive_order_price, round_order_price

logger = logging.getLogger(__name__)

_TOSS_RETRYABLE_REJECTION_CODES = {
    "insufficient-buying-power",
    "order-hours-closed",
    "price-out-of-range",
    "opposite-pending-order-exists",
    "amount-order-outside-regular-hours",
    "fractional-quantity-outside-regular-hours",
}


@dataclass(frozen=True)
class TossSettings:
    client_id: str
    client_secret: str
    account_seq: int | None = None
    base_url: str = "https://openapi.tossinvest.com"
    timeout_seconds: float = 20.0
    enable_live_orders: bool = False
    # Explicit acknowledgement for orders at/above Toss's 100M KRW
    # fat-finger threshold. Left off so an oversized order surfaces as a
    # loud local refusal rather than reaching the venue.
    allow_high_value_orders: bool = False

    @classmethod
    def from_env(cls) -> "TossSettings":
        load_dotenv()
        raw_account = os.environ.get("TOSS_ACCOUNT_SEQ", "").strip()
        settings = cls(
            client_id=os.environ.get("TOSS_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("TOSS_CLIENT_SECRET", "").strip(),
            account_seq=int(raw_account) if raw_account else None,
            base_url=os.environ.get(
                "TOSS_BASE_URL", "https://openapi.tossinvest.com"
            ).rstrip("/"),
            timeout_seconds=float(os.environ.get("TOSS_TIMEOUT_SECONDS", "20")),
            enable_live_orders=os.environ.get(
                "TOSS_ENABLE_LIVE_ORDERS", ""
            ).strip().lower() in {"1", "true", "yes", "on"},
            allow_high_value_orders=os.environ.get(
                "TOSS_ALLOW_HIGH_VALUE_ORDERS", ""
            ).strip().lower() in {"1", "true", "yes", "on"},
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        if not self.client_id:
            missing.append("TOSS_CLIENT_ID")
        if not self.client_secret:
            missing.append("TOSS_CLIENT_SECRET")
        if missing:
            raise BrokerError(f"Missing Toss environment variables: {', '.join(missing)}")
        if self.account_seq is not None and self.account_seq <= 0:
            raise BrokerError("TOSS_ACCOUNT_SEQ must be a positive integer.")
        if not self.base_url.startswith("https://"):
            raise BrokerError("TOSS_BASE_URL must use HTTPS.")


class TossApiError(BrokerError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str = "",
        data: dict[str, Any] | None = None,
    ):
        self.status = status
        self.code = code
        self.request_id = request_id
        self.data = data or {}
        suffix = f" (requestId={request_id})" if request_id else ""
        super().__init__(f"Toss API {status} {code}: {message}{suffix}")


@dataclass(frozen=True)
class TossConditionLeg:
    order_side: Literal["buy", "sell"]
    trigger_price: float
    order_price: float | None = None
    # Protective stops should cross toward execution (sell down / buy up).
    aggressive: bool = False


@dataclass(frozen=True)
class TossConditionalOrderRequest:
    ticker: str
    market: Market
    strategy: Literal["SINGLE", "OCO", "OTO"]
    quantity: int
    order_type: Literal["limit", "market"]
    expire_date: date
    first: TossConditionLeg
    second: TossConditionLeg | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class TossConditionalOrderResult:
    conditional_order_id: str
    client_order_id: str | None
    raw: dict[str, Any]


class TossRestClient:
    """Small standard-library client for Toss Open API v1.2.14.

    Toss permits only one live access token per client. The disk cache and
    ``flock`` ensure separate CLI/web/monitor processes reuse the same token
    instead of invalidating one another in a refresh loop.
    """

    _TOKEN_MARGIN_SECONDS = 60
    _MAX_RATE_RETRIES = 3

    def __init__(self, settings: TossSettings):
        self.settings = settings
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def client_fingerprint(self) -> str:
        return hashlib.sha256(self.settings.client_id.encode()).hexdigest()[:16]

    @property
    def token_cache_path(self) -> Path:
        configured = os.environ.get("TOSS_TOKEN_CACHE", "").strip()
        if configured:
            return Path(configured)
        return Path(f".toss_token_{self.client_fingerprint}.json")

    @contextmanager
    def _token_lock(self):
        path = self.token_cache_path.with_suffix(self.token_cache_path.suffix + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def ensure_token(
        self,
        *,
        force_refresh: bool = False,
        stale_token: str | None = None,
    ) -> str:
        now = time.time()
        if not force_refresh and self._access_token and now < self._expires_at:
            return self._access_token

        with self._token_lock():
            cached = self._read_token_cache()
            if cached is not None and (
                not force_refresh or (stale_token and cached[0] != stale_token)
            ):
                # Another process may have refreshed while we waited for the
                # lock. Reuse that newer token instead of issuing yet another
                # one and invalidating the first process's fresh token.
                self._access_token, self._expires_at = cached
                return self._access_token

            form = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                self.settings.base_url + "/oauth2/token",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.settings.timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise BrokerError(f"Toss token HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise BrokerError(f"Toss token network error: {exc}") from exc

            token = str(payload.get("access_token") or "")
            if not token:
                raise BrokerError("Toss token response did not include access_token.")
            expires_in = int(payload.get("expires_in") or 0)
            if expires_in <= self._TOKEN_MARGIN_SECONDS:
                raise BrokerError("Toss token response included an invalid expires_in.")
            self._access_token = token
            self._expires_at = (
                time.time() + expires_in - self._TOKEN_MARGIN_SECONDS
            )
            self._write_token_cache(token, self._expires_at)
            return token

    def _read_token_cache(self) -> tuple[str, float] | None:
        path = self.token_cache_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("client_fingerprint") != self.client_fingerprint:
                return None
            token = str(payload.get("access_token") or "")
            expires_at = float(payload.get("expires_at") or 0)
            if token and time.time() < expires_at:
                return token, expires_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid Toss token cache: %s", exc)
        return None

    def _write_token_cache(self, token: str, expires_at: float) -> None:
        path = self.token_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "client_fingerprint": self.client_fingerprint,
                    "access_token": token,
                    "expires_at": expires_at,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        account_seq: int | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(
                {key: str(value) for key, value in params.items()}
            )
        url = self.settings.base_url + path + query
        body = json.dumps(payload).encode("utf-8") if payload is not None else None

        refreshed = False
        rate_attempt = 0
        while True:
            headers = {
                "Accept": "application/json",
                # Advertised deliberately: large payloads (stocks/all is
                # ~30KB gzipped) shrink, and _decode_body handles the
                # decompression the gateway performs either way.
                "Accept-Encoding": "gzip",
                "Authorization": f"Bearer {self.ensure_token()}",
            }
            attempted_token = self._access_token
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if account_seq is not None:
                headers["X-Tossinvest-Account"] = str(account_seq)
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method.upper()
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.settings.timeout_seconds
                ) as response:
                    raw = _decode_body(response.read(), response.headers)
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = _decode_body(exc.read(), exc.headers)
                parsed = _json_or_empty(raw)
                error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
                code = str(error.get("code") or parsed.get("error") or "http-error")
                message = str(
                    error.get("message")
                    or parsed.get("error_description")
                    or raw
                    or exc.reason
                )
                request_id = str(error.get("requestId") or "")
                data = error.get("data") if isinstance(error.get("data"), dict) else {}

                if exc.code == 401 and not refreshed:
                    self._access_token = None
                    self._expires_at = 0.0
                    self.ensure_token(
                        force_refresh=True, stale_token=attempted_token
                    )
                    refreshed = True
                    continue
                if (
                    exc.code == 429
                    and idempotent
                    and rate_attempt < self._MAX_RATE_RETRIES
                ):
                    retry_after = float(exc.headers.get("Retry-After", "1") or 1)
                    time.sleep(max(0.1, min(retry_after, 30.0)))
                    rate_attempt += 1
                    continue
                if (
                    exc.code == 409
                    and code == "request-in-progress"
                    and idempotent
                    and rate_attempt < self._MAX_RATE_RETRIES
                ):
                    # Toss can still be finishing the first request for the same
                    # clientOrderId. Replaying that exact idempotent request is
                    # safe, and is preferable to turning a transient 409 into an
                    # indefinitely unknown order.
                    time.sleep(0.5 * (2 ** rate_attempt))
                    rate_attempt += 1
                    continue
                if 500 <= exc.code < 600 and idempotent and rate_attempt < 2:
                    time.sleep(0.5 * (2 ** rate_attempt))
                    rate_attempt += 1
                    continue
                raise TossApiError(
                    exc.code, code, message, request_id=request_id, data=data
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                # Never silently retry a state-changing request here. The
                # approval queue must mark its outcome unknown and reconcile.
                raise BrokerError(f"Toss network error: {exc}") from exc


# Exchange designations that make a symbol unbuyable for this strategy.
# LIQUIDATION_TRADING and INVESTMENT_RISK are non-negotiable (delisting and
# pre-suspension states). INVESTMENT_WARNING and OVERHEATED are included
# because both raise the margin requirement and can flip to a trading halt
# mid-position — exactly the tail this bot has no way to manage. VI_* are
# momentary halts: blocking simply defers the entry to a later sweep.
BLOCKING_STOCK_WARNINGS = frozenset({
    "LIQUIDATION_TRADING",
    "INVESTMENT_RISK",
    "INVESTMENT_WARNING",
    "OVERHEATED",
    "VI_STATIC",
    "VI_DYNAMIC",
    "VI_STATIC_AND_DYNAMIC",
})

# Portfolio valuation costs four calls (holdings, two buying-power, FX), and
# the orchestrator sizes every buy candidate in one sweep. A short TTL keeps
# a watchlist-sized loop to one valuation without hiding intraday moves.
_PORTFOLIO_VALUE_TTL_SECONDS = 60.0


def _safe_amount(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Toss's fat-finger threshold: orders at or above this notional need explicit
# acknowledgement via confirmHighValueOrder.
_HIGH_VALUE_ORDER_KRW = 100_000_000

# Toss requires an explicit expiry on every conditional order. Positions can
# outlive it, so the sync layer re-arms whenever the venue reports EXPIRED.
_PROTECTIVE_STOP_EXPIRE_DAYS = 30


def _is_missing_conditional_order(exc: BrokerOrderRejected) -> bool:
    code = (exc.code or "").lower()
    return "not-found" in code or "not_found" in code


class TossBroker:
    name = "toss"

    def __init__(
        self,
        settings: TossSettings | None = None,
        client: TossRestClient | None = None,
    ):
        self.settings = settings or TossSettings.from_env()
        self.client = client or TossRestClient(self.settings)
        self._account_seq = self.settings.account_seq
        self._security_types: dict[str, str] = {}
        # currency → (monotonic timestamp, value). See portfolio_value().
        self._portfolio_value_cache: dict[str, tuple[float, float]] = {}

    @property
    def mode(self) -> str:
        return "live"

    @property
    def account_id(self) -> str:
        return f"toss-account:{self.account_seq}"

    @property
    def instance_id(self) -> str:
        fingerprint = hashlib.sha256(self.settings.client_id.encode()).hexdigest()[:12]
        return f"toss:live:{self.account_id}:{fingerprint}"

    @property
    def account_seq(self) -> int:
        if self._account_seq is None:
            self._account_seq = self._discover_account_seq()
        return self._account_seq

    def _discover_account_seq(self) -> int:
        raw = self.client.request("GET", "/api/v1/accounts", idempotent=True)
        accounts = raw.get("result") or []
        if not isinstance(accounts, list) or not accounts:
            raise BrokerError("Toss did not return an available BROKERAGE account.")
        if len(accounts) != 1:
            choices = [str(row.get("accountSeq")) for row in accounts]
            raise BrokerError(
                "Multiple Toss accounts are available; set TOSS_ACCOUNT_SEQ "
                f"explicitly (available: {', '.join(choices)})."
            )
        seq = int(accounts[0].get("accountSeq") or 0)
        if seq <= 0:
            raise BrokerError("Toss account response did not include accountSeq.")
        return seq

    def place_order(self, order: OrderRequest) -> OrderResult:
        return self._submit_order(self.normalize_order(order))

    def _guard_high_value(
        self, order: OrderRequest, payload: dict[str, Any]
    ) -> None:
        """Handle Toss's fat-finger threshold on orders worth >= 100M KRW.

        Toss rejects such orders with ``400 confirm-high-value-required``
        unless the caller explicitly acknowledges the size. Silently setting
        the flag would discard a real safety net, so the default is to refuse
        locally with a clear message instead of burning a venue rejection
        that the retry policy would then treat as a permanent block.

        Set ``TOSS_ALLOW_HIGH_VALUE_ORDERS=true`` once the bot's own caps
        (``risk_per_trade_pct``, ``max_position_pct``, the cash pre-flight)
        are trusted to be the controlling limit.
        """

        price = order.limit_price
        if order.order_type != "limit" or price is None:
            # Market orders carry no price to check. Toss applies the same
            # threshold server-side, so an oversized market order surfaces as
            # a rejection rather than slipping through unnoticed.
            return
        notional = price * order.quantity
        if order.market != "KR":
            return  # Threshold is defined in KRW; USD notionals are converted venue-side.
        if notional < _HIGH_VALUE_ORDER_KRW:
            return
        if not self.settings.allow_high_value_orders:
            raise BrokerOrderRejected(
                f"주문금액 {notional:,.0f}원이 고액주문 기준({_HIGH_VALUE_ORDER_KRW:,.0f}원) "
                "이상입니다. 사이징 설정을 확인하고, 의도한 규모라면 "
                "TOSS_ALLOW_HIGH_VALUE_ORDERS=true 로 명시 승인하세요.",
                code="confirm-high-value-required",
                retryable=False,
            )
        payload["confirmHighValueOrder"] = True

    def recover_order(self, order: OrderRequest) -> OrderResult:
        """Replay an unresolved order using Toss's 10-minute idempotency key."""

        if not order.client_order_id:
            raise BrokerError("Cannot recover a Toss order without clientOrderId.")
        return self._submit_order(self.normalize_order(order))

    def _submit_order(self, order: OrderRequest) -> OrderResult:
        if not self.settings.enable_live_orders:
            raise BrokerOrderRejected(
                "Toss live orders are disabled. Set TOSS_ENABLE_LIVE_ORDERS=true "
                "only after the read-only and tiny-order acceptance tests pass."
            )
        if order.quantity <= 0:
            raise BrokerOrderRejected("Order quantity must be positive.")
        if order.order_type == "limit" and order.limit_price is None:
            raise BrokerOrderRejected("Limit orders require limit_price.")
        payload: dict[str, Any] = {
            "clientOrderId": order.client_order_id,
            "symbol": order.ticker.upper(),
            "side": order.side.upper(),
            "orderType": order.order_type.upper(),
            "quantity": str(order.quantity),
        }
        if not order.client_order_id:
            payload.pop("clientOrderId")
        if order.order_type == "limit":
            payload["price"] = _decimal_string(
                order.limit_price, integer_only=order.market == "KR"
            )
        self._guard_high_value(order, payload)
        try:
            raw = self.client.request(
                "POST",
                "/api/v1/orders",
                payload=payload,
                account_seq=self.account_seq,
                idempotent=bool(order.client_order_id),
            )
        except TossApiError as exc:
            if exc.status in {400, 404, 422}:
                raise BrokerOrderRejected(
                    str(exc),
                    code=exc.code,
                    retryable=exc.code in _TOSS_RETRYABLE_REJECTION_CODES,
                ) from exc
            raise
        result = raw.get("result") or {}
        order_id = str(result.get("orderId") or "")
        if not order_id:
            raise BrokerError("Toss order response did not include orderId.")
        return OrderResult(self.name, True, order_id, "Toss order accepted.", raw)

    def normalize_order(self, order: OrderRequest) -> OrderRequest:
        if order.order_type != "limit" or order.limit_price is None:
            return order
        security_type = self._security_type(order.ticker) if order.market == "KR" else None
        normalized = round_order_price(
            order.limit_price,
            order.market,
            order.side,
            security_type=security_type,
        )
        return replace(order, limit_price=normalized)

    # ── TradabilityBroker capability ─────────────────────────────────

    def tradability_block(self, ticker: str, market: Market) -> str | None:
        """Reason this symbol must not be bought right now, or None.

        Covers the states a price/volume screen cannot see: delisting
        procedures, suspensions, exchange designations, and momentary VI
        halts. Raises on transport failure so the caller can fail closed —
        an unverifiable symbol is not a buyable symbol.
        """

        meta = self._stock_metadata(ticker)
        status = str(meta.get("status") or "").upper()
        if status and status != "ACTIVE":
            return f"상장 상태 {status}"

        detail = meta.get("koreanMarketDetail")
        if isinstance(detail, dict):
            if detail.get("liquidationTrading"):
                return "정리매매 진행 중"
            if detail.get("krxTradingSuspended"):
                return "KRX 거래정지"

        raw = self.client.request(
            "GET",
            "/api/v1/stocks/"
            + urllib.parse.quote(ticker.upper(), safe="")
            + "/warnings",
            account_seq=None,
            idempotent=True,
        )
        rows = raw.get("result") or []
        if not isinstance(rows, list):
            raise BrokerError(f"Toss warnings payload was not an array for {ticker}.")
        hits = [
            str(row.get("warningType") or "").upper()
            for row in rows
            if isinstance(row, dict)
            and str(row.get("warningType") or "").upper() in BLOCKING_STOCK_WARNINGS
        ]
        if hits:
            return "매수 유의 지정: " + ", ".join(sorted(set(hits)))
        return None

    def _stock_metadata(self, ticker: str) -> dict[str, Any]:
        symbol = ticker.upper()
        raw = self.client.request(
            "GET",
            "/api/v1/stocks",
            params={"symbols": symbol},
            idempotent=True,
        )
        rows = raw.get("result") or []
        match = next(
            (
                row for row in rows
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == symbol
            ),
            None,
        )
        if not match:
            raise BrokerError(f"Toss stock metadata not found for {symbol}.")
        kind = str(match.get("securityType") or "")
        if kind:
            self._security_types[symbol] = kind
        return match

    def _security_type(self, ticker: str) -> str:
        symbol = ticker.upper()
        if symbol in self._security_types:
            return self._security_types[symbol]
        raw = self.client.request(
            "GET",
            "/api/v1/stocks",
            params={"symbols": symbol},
            idempotent=True,
        )
        rows = raw.get("result") or []
        match = next(
            (row for row in rows if str(row.get("symbol") or "").upper() == symbol),
            None,
        )
        if not match:
            raise BrokerError(f"Toss stock metadata not found for {symbol}.")
        kind = str(match.get("securityType") or "")
        if not kind:
            raise BrokerError(f"Toss stock metadata omitted securityType for {symbol}.")
        self._security_types[symbol] = kind
        return kind

    def create_conditional_order(
        self, request: TossConditionalOrderRequest
    ) -> TossConditionalOrderResult:
        """Create a Toss server-side SINGLE/OCO/OTO order."""

        self._require_live_orders()
        payload = self._conditional_payload(request, require_client_id=True)
        raw = self._conditional_write(
            "POST",
            "/api/v1/conditional-orders",
            payload=payload,
            idempotent=True,
        )
        result = raw.get("result") or {}
        conditional_id = str(result.get("conditionalOrderId") or "")
        if not conditional_id:
            raise BrokerError(
                "Toss conditional-order response did not include conditionalOrderId."
            )
        return TossConditionalOrderResult(
            conditional_order_id=conditional_id,
            client_order_id=(
                str(result.get("clientOrderId"))
                if result.get("clientOrderId")
                else request.client_order_id
            ),
            raw=raw,
        )

    def list_conditional_orders(
        self, status: Literal["OPEN", "CLOSED"], *, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        if status not in {"OPEN", "CLOSED"}:
            raise BrokerError("Conditional-order status must be OPEN or CLOSED.")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"status": status, "limit": 100}
            if symbol:
                params["symbol"] = symbol.upper()
            if cursor:
                params["cursor"] = cursor
            raw = self.client.request(
                "GET",
                "/api/v1/conditional-orders",
                params=params,
                account_seq=self.account_seq,
                idempotent=True,
            )
            result = raw.get("result") or {}
            page = result.get("conditionalOrders") or []
            if not isinstance(page, list):
                raise BrokerError("Toss conditional-order list was not an array.")
            rows.extend(row for row in page if isinstance(row, dict))
            next_cursor = str(result.get("nextCursor") or "")
            if not result.get("hasNext"):
                break
            if not next_cursor or next_cursor in seen_cursors:
                raise BrokerError("Toss conditional-order pagination cursor repeated.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return rows

    def get_conditional_order(self, conditional_order_id: str) -> dict[str, Any]:
        raw = self.client.request(
            "GET",
            "/api/v1/conditional-orders/"
            + urllib.parse.quote(conditional_order_id, safe=""),
            account_seq=self.account_seq,
            idempotent=True,
        )
        result = raw.get("result")
        if not isinstance(result, dict):
            raise BrokerError("Toss conditional-order detail was missing.")
        return result

    def modify_conditional_order(
        self,
        conditional_order_id: str,
        request: TossConditionalOrderRequest,
    ) -> TossConditionalOrderResult:
        """Replace a condition; Toss returns a new ID and invalidates the old one."""

        self._require_live_orders()
        payload = self._conditional_payload(request, require_client_id=False)
        payload.pop("symbol", None)
        payload.pop("clientOrderId", None)
        raw = self._conditional_write(
            "POST",
            "/api/v1/conditional-orders/"
            + urllib.parse.quote(conditional_order_id, safe="")
            + "/modify",
            payload=payload,
            # This endpoint has no idempotency key. Never retry automatically:
            # a lost response may already have invalidated the old ID.
            idempotent=False,
        )
        result = raw.get("result") or {}
        new_id = str(result.get("conditionalOrderId") or "")
        if not new_id:
            raise BrokerError(
                "Toss conditional-order modify response did not include the new ID."
            )
        return TossConditionalOrderResult(new_id, None, raw)

    def cancel_conditional_order(self, conditional_order_id: str) -> None:
        self._require_live_orders()
        self._conditional_write(
            "DELETE",
            "/api/v1/conditional-orders/"
            + urllib.parse.quote(conditional_order_id, safe=""),
            payload=None,
            idempotent=False,
        )

    # ── ProtectiveStopBroker capability ──────────────────────────────
    #
    # A SINGLE + MARKET conditional sell mirroring the position's effective
    # stop. SINGLE (not OCO) because the polling ladder scales out half at
    # target-1, and an OCO shares one quantity across both legs — it cannot
    # express "sell half here, all of it there". Targets stay with the bot;
    # only the disaster brake lives at the venue.
    #
    # Coverage limit: Toss triggers KR conditional orders during the KRX
    # regular session only (US triggers in every tradable session). This
    # protects against the bot being down mid-session, not against overnight
    # gaps — nothing can, since the market is shut.

    def place_protective_stop(
        self,
        *,
        ticker: str,
        market: Market,
        quantity: int,
        stop_price: float,
        client_order_id: str,
    ) -> str:
        result = self.create_conditional_order(
            self._protective_stop_request(
                ticker=ticker,
                market=market,
                quantity=quantity,
                stop_price=stop_price,
                client_order_id=client_order_id,
            )
        )
        return result.conditional_order_id

    def list_protective_stop_ids(self, ticker: str) -> list[str]:
        """OPEN conditional orders on ``ticker`` shaped like our stops.

        SINGLE + MARKET is the bot's protective-stop shape. The list response
        has no ``clientOrderId``, so ownership cannot be proven — callers use
        this for warn-only reconciliation, never for cancellation.
        """

        rows = self.list_conditional_orders("OPEN", symbol=ticker)
        return [
            str(row.get("conditionalOrderId"))
            for row in rows
            if row.get("conditionalOrderId")
            and str(row.get("type") or "").upper() == "SINGLE"
            and str(row.get("orderType") or "").upper() == "MARKET"
        ]

    def cancel_protective_stop(self, stop_id: str) -> None:
        try:
            self.cancel_conditional_order(stop_id)
        except BrokerOrderRejected as exc:
            # Already gone (filled, expired, cancelled elsewhere) is the
            # desired end state — cancellation must be idempotent.
            if _is_missing_conditional_order(exc):
                logger.info("Protective stop %s already absent at Toss.", stop_id)
                return
            raise

    def protective_stop_status(self, stop_id: str) -> str | None:
        try:
            detail = self.get_conditional_order(stop_id)
        except TossApiError as exc:
            if exc.status == 404:
                return None
            # Observed live: Toss validates the id *format* before existence,
            # so a malformed id returns 400 invalid-request rather than 404.
            # An id that cannot name a stop is, for the caller, a stop that
            # does not exist. Other 400 codes (e.g. account-header-required)
            # are real request bugs and must keep raising.
            if exc.status == 400 and exc.code == "invalid-request":
                return None
            raise
        except BrokerOrderRejected as exc:
            if _is_missing_conditional_order(exc):
                return None
            raise
        return str(detail.get("status") or "") or None

    def _protective_stop_request(
        self,
        *,
        ticker: str,
        market: Market,
        quantity: int,
        stop_price: float,
        client_order_id: str | None,
    ) -> TossConditionalOrderRequest:
        return TossConditionalOrderRequest(
            ticker=ticker,
            market=market,
            strategy="SINGLE",
            quantity=quantity,
            order_type="market",
            expire_date=date.today() + timedelta(days=_PROTECTIVE_STOP_EXPIRE_DAYS),
            first=TossConditionLeg(
                order_side="sell",
                trigger_price=stop_price,
                # Round the trigger down so the armed stop is never tighter
                # than the level the strategy computed.
                aggressive=True,
            ),
            client_order_id=client_order_id,
        )

    def _require_live_orders(self) -> None:
        if not self.settings.enable_live_orders:
            raise BrokerOrderRejected(
                "Toss live orders are disabled. Set TOSS_ENABLE_LIVE_ORDERS=true "
                "only after the read-only and tiny-order acceptance tests pass."
            )

    def _conditional_write(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        idempotent: bool,
    ) -> dict[str, Any]:
        try:
            return self.client.request(
                method,
                path,
                payload=payload,
                account_seq=self.account_seq,
                idempotent=idempotent,
            )
        except TossApiError as exc:
            if exc.status in {400, 404, 422}:
                raise BrokerOrderRejected(
                    str(exc),
                    code=exc.code,
                    retryable=exc.code in _TOSS_RETRYABLE_REJECTION_CODES,
                ) from exc
            raise

    def _conditional_payload(
        self,
        request: TossConditionalOrderRequest,
        *,
        require_client_id: bool,
    ) -> dict[str, Any]:
        strategy = request.strategy.upper()
        order_type = request.order_type.upper()
        if strategy not in {"SINGLE", "OCO", "OTO"}:
            raise BrokerOrderRejected(f"Unsupported conditional strategy: {strategy}")
        if request.market not in {"KR", "US"}:
            raise BrokerOrderRejected(f"Unsupported market: {request.market}")
        if isinstance(request.quantity, bool) or request.quantity <= 0:
            raise BrokerOrderRejected("Conditional-order quantity must be positive.")
        if request.expire_date < date.today():
            raise BrokerOrderRejected("Conditional-order expire_date is in the past.")
        if require_client_id and not request.client_order_id:
            raise BrokerOrderRejected(
                "Bot conditional orders require client_order_id for idempotency."
            )
        if request.client_order_id and (
            len(request.client_order_id) > 36
            or any(
                not (char.isascii() and (char.isalnum() or char in "-_"))
                for char in request.client_order_id
            )
        ):
            raise BrokerOrderRejected(
                "client_order_id must be <=36 ASCII letters, digits, '-' or '_'."
            )
        if strategy == "SINGLE" and request.second is not None:
            raise BrokerOrderRejected("SINGLE conditional order cannot have second leg.")
        if strategy in {"OCO", "OTO"} and request.second is None:
            raise BrokerOrderRejected(f"{strategy} conditional order requires second leg.")
        if strategy in {"OCO", "OTO"} and order_type != "LIMIT":
            raise BrokerOrderRejected(f"{strategy} supports LIMIT orders only.")

        first_side = request.first.order_side.lower()
        second_side = request.second.order_side.lower() if request.second else None
        if strategy == "OCO" and (first_side != "sell" or second_side != "sell"):
            raise BrokerOrderRejected("OCO legs must both be SELL.")
        if strategy == "OTO" and (first_side != "buy" or second_side != "sell"):
            raise BrokerOrderRejected("OTO must be BUY first and SELL second.")

        security_type = (
            self._security_type(request.ticker) if request.market == "KR" else None
        )

        def leg_payload(leg: TossConditionLeg, *, aggressive: bool) -> dict[str, str]:
            side = leg.order_side.lower()
            if side not in {"buy", "sell"}:
                raise BrokerOrderRejected(f"Unsupported conditional side: {side}")
            rounder = (
                round_aggressive_order_price if aggressive else round_order_price
            )
            trigger = rounder(
                leg.trigger_price,
                request.market,
                side,  # type: ignore[arg-type]
                security_type=security_type,
            )
            result = {
                "orderSide": side.upper(),
                "triggerPrice": _decimal_string(
                    trigger, integer_only=request.market == "KR"
                ),
            }
            if order_type == "LIMIT":
                if leg.order_price is None:
                    raise BrokerOrderRejected(
                        "LIMIT conditional leg requires order_price."
                    )
                price = rounder(
                    leg.order_price,
                    request.market,
                    side,  # type: ignore[arg-type]
                    security_type=security_type,
                )
                result["orderPrice"] = _decimal_string(
                    price, integer_only=request.market == "KR"
                )
            elif leg.order_price is not None:
                raise BrokerOrderRejected(
                    "MARKET conditional leg must not include order_price."
                )
            return result

        first_aggressive = request.first.aggressive
        second_aggressive = request.second.aggressive if request.second else False
        if strategy == "OCO":
            # Toss defines first as the upper profit leg and second as the
            # lower protective leg. Force the stop limit toward execution.
            first_aggressive = False
            second_aggressive = True
        elif strategy == "OTO":
            first_aggressive = False
            second_aggressive = False

        first = leg_payload(request.first, aggressive=first_aggressive)
        second = (
            leg_payload(request.second, aggressive=second_aggressive)
            if request.second
            else None
        )
        if strategy == "OCO" and second is not None:
            first_trigger = Decimal(first["triggerPrice"])
            second_trigger = Decimal(second["triggerPrice"])
            if first_trigger <= second_trigger:
                raise BrokerOrderRejected(
                    "OCO first trigger must be above second trigger."
                )
            if Decimal(second["orderPrice"]) > second_trigger:
                raise BrokerOrderRejected(
                    "OCO protective SELL orderPrice must be <= its triggerPrice."
                )

        payload: dict[str, Any] = {
            "symbol": request.ticker.upper(),
            "type": strategy,
            "quantity": str(request.quantity),
            "orderType": order_type,
            "expireDate": request.expire_date.isoformat(),
            "first": first,
        }
        if request.client_order_id:
            payload["clientOrderId"] = request.client_order_id
        if second is not None:
            payload["second"] = second
        return payload

    def get_order_fill(
        self, broker_order_id: str, market: Market, ordered_quantity: int
    ) -> OrderFill:
        raw = self.client.request(
            "GET",
            f"/api/v1/orders/{urllib.parse.quote(broker_order_id, safe='')}",
            account_seq=self.account_seq,
            idempotent=True,
        )
        order = raw.get("result") or {}
        execution = order.get("execution") or {}
        filled = _whole_quantity(execution.get("filledQuantity", "0"), "filledQuantity")
        average = execution.get("averageFilledPrice")
        avg_fill_price = float(average) if average not in {None, ""} else None
        status = _map_toss_order_status(str(order.get("status") or ""), filled)
        return OrderFill(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled,
            ordered_quantity=ordered_quantity,
            avg_fill_price=avg_fill_price,
            message=f"Toss status={order.get('status', 'UNKNOWN')}",
            raw=raw,
        )

    def cancel_order(
        self, broker_order_id: str, market: Market, ticker: str, quantity: int
    ) -> OrderResult:
        try:
            raw = self.client.request(
                "POST",
                f"/api/v1/orders/{urllib.parse.quote(broker_order_id, safe='')}/cancel",
                payload={},
                account_seq=self.account_seq,
                idempotent=True,
            )
        except TossApiError as exc:
            # Observed live (stage-2 verification): cancellation is
            # asynchronous at Toss — the original order passes through
            # PENDING_CANCEL before landing on CANCELED, and a repeated
            # cancel meanwhile answers 409 already-canceled. That repeat is
            # the *desired end state*, not a failure: the stale-order sweep
            # and the tiny-order runner both legitimately re-cancel, so
            # treat it as idempotent success. 409 already-filled is a real
            # refusal (the shares exist; sync will pick the fill up), and
            # 409 already-processing resolves on the next sweep.
            if exc.status == 409 and exc.code == "already-canceled":
                return OrderResult(
                    self.name, True, broker_order_id,
                    "Toss cancel already effective (already-canceled).",
                    exc.data,
                )
            if exc.status in {400, 401, 403, 404, 409, 422}:
                return OrderResult(self.name, False, broker_order_id, str(exc), exc.data)
            raise
        result = raw.get("result") or {}
        returned_id = str(result.get("orderId") or broker_order_id)
        return OrderResult(self.name, True, returned_id, "Toss cancel accepted.", raw)

    def get_positions(self, market: Market) -> list[Position]:
        raw = self.client.request(
            "GET", "/api/v1/holdings", account_seq=self.account_seq, idempotent=True
        )
        result = raw.get("result") or {}
        positions: list[Position] = []
        for item in result.get("items") or []:
            if item.get("marketCountry") != market:
                continue
            quantity = _whole_quantity(item.get("quantity", "0"), "holding quantity")
            if quantity <= 0:
                continue
            avg = float(item.get("averagePurchasePrice") or 0)
            current = float(item.get("lastPrice") or 0)
            market_value = float((item.get("marketValue") or {}).get("amount") or 0)
            pnl_info = item.get("profitLoss") or {}
            pnl = float(pnl_info.get("amount") or 0)
            pnl_pct = float(pnl_info.get("rate") or 0) * 100.0
            positions.append(
                Position(
                    broker=self.name,
                    ticker=str(item.get("symbol") or "").upper(),
                    market=market,
                    quantity=quantity,
                    avg_price=avg,
                    current_price=current,
                    market_value=market_value,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    currency=str(item.get("currency") or ("KRW" if market == "KR" else "USD")),
                    raw=item,
                )
            )
        return positions

    # ── PortfolioValuationBroker capability ──────────────────────────

    def portfolio_value(self, currency: str) -> float:
        """Whole-account value expressed in ``currency``.

        ``get_cash_balance`` deliberately reports one market's sleeve, which
        is what a cash pre-flight needs. Risk sizing needs the opposite: a
        percentage of *the portfolio*, not of whichever sleeve the candidate
        happens to trade in. Without this, an account that is 90% KRW sizes
        US trades against the remaining 10% and the same setup gets a wildly
        different budget depending only on where the money currently sits.

        Toss reports holdings already split by currency and never converts
        across them, so the conversion happens here using the매매기준율
        (``midRate``) rather than the buy rate — a valuation should not
        include the dealing spread.
        """

        target = currency.upper()
        if target not in {"KRW", "USD"}:
            raise BrokerError(f"Unsupported valuation currency: {target}")

        now = time.monotonic()
        cached = self._portfolio_value_cache.get(target)
        if cached and (now - cached[0]) < _PORTFOLIO_VALUE_TTL_SECONDS:
            return cached[1]

        holdings_raw = self.client.request(
            "GET", "/api/v1/holdings", account_seq=self.account_seq, idempotent=True
        )
        amounts = (
            ((holdings_raw.get("result") or {}).get("marketValue") or {}).get("amount")
            or {}
        )
        krw = _safe_amount(amounts.get("krw"))
        usd = _safe_amount(amounts.get("usd"))

        for code in ("KRW", "USD"):
            raw = self.client.request(
                "GET",
                "/api/v1/buying-power",
                params={"currency": code},
                account_seq=self.account_seq,
                idempotent=True,
            )
            cash = _safe_amount((raw.get("result") or {}).get("cashBuyingPower"))
            if code == "KRW":
                krw += cash
            else:
                usd += cash

        rate = self.usd_krw_rate()
        total_krw = krw + usd * rate
        value = total_krw if target == "KRW" else total_krw / rate

        self._portfolio_value_cache[target] = (now, value)
        return value

    def usd_krw_rate(self) -> float:
        """USD→KRW 매매기준율. Raises when the venue cannot price the pair."""

        raw = self.client.request(
            "GET",
            "/api/v1/exchange-rate",
            params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
            idempotent=True,
        )
        result = raw.get("result") or {}
        # midRate is the interbank reference; `rate` bakes in the dealing
        # spread and would overstate the USD sleeve.
        rate = _safe_amount(result.get("midRate")) or _safe_amount(result.get("rate"))
        if rate <= 0:
            raise BrokerError("Toss exchange-rate response did not include a usable rate.")
        return rate

    def get_cash_balance(self, market: Market) -> AccountBalance:
        currency = "KRW" if market == "KR" else "USD"
        buying_raw = self.client.request(
            "GET",
            "/api/v1/buying-power",
            params={"currency": currency},
            account_seq=self.account_seq,
            idempotent=True,
        )
        cash = float((buying_raw.get("result") or {}).get("cashBuyingPower") or 0)
        holdings_raw = self.client.request(
            "GET", "/api/v1/holdings", account_seq=self.account_seq, idempotent=True
        )
        items = (holdings_raw.get("result") or {}).get("items") or []
        market_items = [item for item in items if item.get("marketCountry") == market]
        securities = sum(
            float((item.get("marketValue") or {}).get("amount") or 0)
            for item in market_items
        )
        pnl = sum(
            float((item.get("profitLoss") or {}).get("amount") or 0)
            for item in market_items
        )
        purchase = sum(
            float((item.get("marketValue") or {}).get("purchaseAmount") or 0)
            for item in market_items
        )
        return AccountBalance(
            broker=self.name,
            market=market,
            currency=currency,
            cash=cash,
            securities_value=securities,
            total_value=cash + securities,
            pnl=pnl,
            pnl_pct=(pnl / purchase * 100.0) if purchase > 0 else 0.0,
            raw={"buying_power": buying_raw, "holdings": holdings_raw},
        )


def _decode_body(data: bytes, headers: Any) -> str:
    """Decode a response body, decompressing when the gateway compressed it.

    Discovered live by the contract smoke test: Toss's gateway gzips at
    least some *error* bodies even when the client never advertised
    Accept-Encoding. Decoding those bytes as UTF-8 turned every error into
    mojibake with code "http-error", which silently disabled all
    code-based handling (429 Retry-After pacing, 409 request-in-progress
    replay, the rejection classifier). The magic-byte check covers
    responses the gateway compresses without labelling.
    """

    encoding = ""
    try:
        encoding = str(headers.get("Content-Encoding") or "").lower()
    except Exception:
        pass
    if data[:2] == b"\x1f\x8b" or "gzip" in encoding:
        try:
            data = gzip.decompress(data)
        except OSError:
            pass  # labelled gzip but is not — fall through to raw decode
    elif "deflate" in encoding:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                data = zlib.decompress(data, wbits)
                break
            except zlib.error:
                continue
    return data.decode("utf-8", errors="replace")


def _json_or_empty(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _decimal_string(value: Any, *, integer_only: bool = False) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerOrderRejected(f"Invalid decimal value: {value}") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise BrokerOrderRejected(f"Price must be positive: {value}")
    if integer_only:
        if decimal != decimal.to_integral_value():
            raise BrokerOrderRejected(f"KR price must be an integer: {value}")
        return str(int(decimal))
    return format(decimal, "f")


def _whole_quantity(value: Any, field: str) -> int:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerError(f"Invalid Toss {field}: {value}") from exc
    if quantity != quantity.to_integral_value():
        raise BrokerError(
            f"Toss returned fractional {field}={quantity}; this strategy currently "
            "supports whole-share positions only. Use a dedicated account or enable "
            "fractional quantity support before trading."
        )
    return int(quantity)


def _map_toss_order_status(status: str, filled_quantity: int) -> OrderStatus:
    if status == "FILLED":
        return "filled"
    if status == "PARTIAL_FILLED":
        return "partially_filled"
    if status in {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE"}:
        return "partially_filled" if filled_quantity > 0 else "submitted"
    if status in {"CANCELED", "REPLACED", "REJECTED"}:
        if filled_quantity > 0:
            return "partially_filled_cancelled"
        return "rejected" if status == "REJECTED" else "cancelled"
    if status in {"CANCEL_REJECTED", "REPLACE_REJECTED"}:
        return "partially_filled" if filled_quantity > 0 else "submitted"
    raise BrokerError(f"Unknown Toss order status: {status or '<empty>'}")
