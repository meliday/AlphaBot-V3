from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
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
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
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
            if exc.status in {400, 401, 403, 404, 422}:
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
