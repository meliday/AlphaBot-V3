from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from alpha_bot.config import load_dotenv
from alpha_bot.errors import BrokerError
from alpha_bot.models import (
    AccountBalance,
    Market,
    OrderFill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
)
from alpha_bot.utils import infer_us_exchange_code

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KisSettings:
    app_key: str
    app_secret: str
    account_no: str
    account_product: str = "01"
    mode: str = "paper"
    hts_id: str = ""

    @property
    def base_url(self) -> str:
        if self.mode == "live":
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    @classmethod
    def from_env(cls) -> "KisSettings":
        load_dotenv()
        settings = cls(
            app_key=os.environ.get("KIS_APP_KEY", ""),
            app_secret=os.environ.get("KIS_APP_SECRET", ""),
            account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
            account_product=os.environ.get("KIS_ACCOUNT_PRODUCT", "01"),
            mode=os.environ.get("KIS_MODE", "paper").lower(),
            hts_id=os.environ.get("KIS_HTS_ID", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "KIS_APP_KEY": self.app_key,
                "KIS_APP_SECRET": self.app_secret,
                "KIS_ACCOUNT_NO": self.account_no,
                "KIS_ACCOUNT_PRODUCT": self.account_product,
            }.items()
            if not value
        ]
        if missing:
            raise BrokerError(f"Missing KIS environment variables: {', '.join(missing)}")
        if self.mode not in {"paper", "live"}:
            raise BrokerError("KIS_MODE must be paper or live.")


class KisRestClient:
    """Low-level HTTP client for the KIS Open API."""

    # Refresh the token 60 seconds before actual expiry to avoid races.
    _TOKEN_REFRESH_MARGIN = 60

    # Class-level cache to share token across multiple instances
    _token_cache: dict[str, tuple[str, float]] = {}

    # ── Rate limiting (class-level so multiple KisRestClient instances
    # that share the same APP_KEY actually share the throttle state).
    #
    # KIS quotas per APP_KEY are roughly:
    #   paper (모의):  ~1-2 req/sec, with some endpoints capped at 1 req/sec
    #   live  (실전): ~20 req/sec
    # We pick conservative gaps so that bursty callers (data provider +
    # broker queries) cannot collide and trigger EGW00201.
    _MIN_INTERVAL: dict[str, float] = {"paper": 0.5, "live": 0.1}
    _RATE_LIMIT_MAX_RETRIES = 5
    _RATE_LIMIT_BACKOFF_BASE = 0.6  # seconds; exponential per attempt
    _throttle_lock = threading.Lock()
    _last_request_at: dict[str, float] = {}

    def __init__(self, settings: KisSettings):
        self.settings = settings
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ── Throttle / retry ────────────────────────────────────────────

    def _throttle_key(self) -> str:
        return f"{self.settings.app_key}:{self.settings.mode}"

    def _throttle(self) -> None:
        min_interval = self._MIN_INTERVAL.get(self.settings.mode, 0.5)
        key = self._throttle_key()
        with self._throttle_lock:
            last = self._last_request_at.get(key, 0.0)
            now = time.monotonic()
            wait = min_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_request_at[key] = now

    def ensure_token(self) -> str:
        cache_key = f"{self.settings.app_key}:{self.settings.mode}"
        cache_file = Path(f".kis_token_{self.settings.mode}.json")

        # 1. Instance cache.
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        # 2. Class-level cache (shared across KisRestClient instances).
        if cache_key in KisRestClient._token_cache:
            cached_token, cached_expiry = KisRestClient._token_cache[cache_key]
            if time.time() < cached_expiry:
                self._access_token = cached_token
                self._token_expires_at = cached_expiry
                return cached_token

        # 3. Disk cache (survives process restarts).
        if cache_file.exists():
            try:
                disk_cache = json.loads(cache_file.read_text(encoding="utf-8"))
                cached_expiry = disk_cache.get("expires_at", 0)
                cached_token = disk_cache.get("access_token", "")
                if cached_token and time.time() < cached_expiry:
                    self._access_token = cached_token
                    self._token_expires_at = cached_expiry
                    KisRestClient._token_cache[cache_key] = (cached_token, cached_expiry)
                    return cached_token
            except Exception as exc:
                logger.warning("Failed to read disk token cache: %s", exc)

        # 4. Request a fresh token.
        logger.info("Requesting new KIS access token (mode=%s)", self.settings.mode)
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
        }
        response = self.post("/oauth2/tokenP", payload, include_auth=False)
        token = response.get("access_token")
        if not token:
            raise BrokerError("KIS token response did not include access_token.")

        self._access_token = str(token)
        expires_in = int(response.get("expires_in", 86_400))
        self._token_expires_at = time.time() + expires_in - self._TOKEN_REFRESH_MARGIN
        KisRestClient._token_cache[cache_key] = (self._access_token, self._token_expires_at)
        try:
            cache_file.write_text(
                json.dumps({
                    "access_token": self._access_token,
                    "expires_at": self._token_expires_at,
                }),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to write disk token cache: %s", exc)

        logger.info("KIS token acquired, expires in %d seconds", expires_in)
        return self._access_token

    def hashkey(self, payload: dict[str, Any]) -> str:
        response = self.post("/uapi/hashkey", payload, include_auth=False)
        hash_key = response.get("HASH") or response.get("hash")
        if not hash_key:
            raise BrokerError(f"KIS hashkey response did not include HASH: {response}")
        return str(hash_key)

    def post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        tr_id: str | None = None,
        include_auth: bool = True,
        include_hash: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
        }
        if include_auth:
            headers["authorization"] = f"Bearer {self.ensure_token()}"
        if tr_id:
            headers["tr_id"] = tr_id
        if include_hash:
            headers["hashkey"] = self.hashkey(payload)
        return self._request("POST", endpoint, headers, payload)

    def get(self, endpoint: str, params: dict[str, Any], *, tr_id: str) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.ensure_token()}",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
            "tr_id": tr_id,
        }
        query = urllib.parse.urlencode({key: str(value) for key, value in params.items()})
        return self._request("GET", f"{endpoint}?{query}", headers, None)

    def _request(
        self,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            self._throttle()
            try:
                return self._raw_request(method, endpoint, headers, payload)
            except BrokerError as exc:
                if "EGW00201" not in str(exc):
                    raise
                if attempt >= self._RATE_LIMIT_MAX_RETRIES:
                    logger.error(
                        "KIS rate-limit (EGW00201) gave up after %d retries on %s",
                        attempt, endpoint,
                    )
                    raise
                delay = self._RATE_LIMIT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3)
                logger.warning(
                    "KIS rate-limit (EGW00201) on %s — backoff %.2fs (attempt %d/%d)",
                    endpoint, delay, attempt + 1, self._RATE_LIMIT_MAX_RETRIES,
                )
                time.sleep(delay)
                attempt += 1

    def _raw_request(
        self,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.settings.base_url + endpoint,
            data=data,
            headers=headers,
            method=method,
        )
        logger.debug("KIS %s %s", method, endpoint)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # Sanitize: never include app_secret in error messages.
            sanitized = body.replace(self.settings.app_secret, "***")
            logger.error("KIS HTTP %d: %s", exc.code, sanitized)
            raise BrokerError(f"KIS HTTP {exc.code}: {sanitized}") from exc
        except urllib.error.URLError as exc:
            logger.error("KIS network error: %s", exc)
            raise BrokerError(f"KIS network error: {exc}") from exc
        return json.loads(raw)


class KisBroker:
    name = "kis"

    def __init__(self, settings: KisSettings | None = None, client: KisRestClient | None = None):
        self.settings = settings or KisSettings.from_env()
        self.client = client or KisRestClient(self.settings)

    def place_order(self, order: OrderRequest) -> OrderResult:
        if order.quantity <= 0:
            raise BrokerError("Order quantity must be positive.")
        if order.order_type == "limit" and order.limit_price is None:
            raise BrokerError("Limit orders require limit_price.")
        if order.market == "KR":
            endpoint, tr_id, payload = self._domestic_order(order)
        elif order.market == "US":
            endpoint, tr_id, payload = self._overseas_order(order)
        else:
            raise BrokerError(f"Unsupported market: {order.market}")

        logger.info(
            "Submitting KIS order: %s %s:%s qty=%d type=%s",
            order.side, order.market, order.ticker, order.quantity, order.order_type,
        )
        raw = self.client.post(endpoint, payload, tr_id=tr_id, include_auth=True, include_hash=True)
        accepted = str(raw.get("rt_cd", "0")) == "0"
        message = str(raw.get("msg1", raw.get("message", "")))
        output = raw.get("output", {}) if isinstance(raw.get("output"), dict) else {}
        broker_order_id = str(output.get("ODNO") or output.get("odno") or raw.get("msg_cd") or "")
        logger.info("KIS order result: accepted=%s id=%s msg=%s", accepted, broker_order_id, message)
        return OrderResult(self.name, accepted, broker_order_id, message, raw)

    def _domestic_order(self, order: OrderRequest) -> tuple[str, str, dict[str, Any]]:
        tr_id = _domestic_tr_id(self.settings.mode, order.side)
        price = 0 if order.order_type == "market" else int(round(float(order.limit_price or 0)))
        payload = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product,
            "PDNO": order.ticker,
            "ORD_DVSN": "01" if order.order_type == "market" else "00",
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": str(price),
        }
        return "/uapi/domestic-stock/v1/trading/order-cash", tr_id, payload

    def _overseas_order(self, order: OrderRequest) -> tuple[str, str, dict[str, Any]]:
        tr_id = _overseas_tr_id(self.settings.mode, order.side)
        price = "0" if order.order_type == "market" else f"{float(order.limit_price or 0):.2f}"
        payload = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product,
            "OVRS_EXCG_CD": infer_us_exchange_code(order.ticker),
            "PDNO": order.ticker,
            "ORD_DVSN": "01" if order.order_type == "market" else "00",
            "ORD_QTY": str(order.quantity),
            "OVRS_ORD_UNPR": price,
            "ORD_SVR_DVSN_CD": "0",
        }
        return "/uapi/overseas-stock/v1/trading/order", tr_id, payload

    # ── Order cancellation ───────────────────────────────────────────

    def cancel_order(
        self, broker_order_id: str, market: Market, ticker: str, quantity: int
    ) -> OrderResult:
        """Cancel the unfilled remainder of a working order (전량 취소).

        Uses the KIS 정정취소 endpoints with RVSE_CNCL_DVSN_CD="02" (cancel).
        NOTE: verify TR ids/payload once against 모의투자 before relying on
        this in live mode — KIS occasionally revs field requirements.
        """
        if not broker_order_id:
            raise BrokerError("cancel_order requires a broker_order_id.")
        if market == "KR":
            tr_id = "VTTC0803U" if self.settings.mode == "paper" else "TTTC0803U"
            endpoint = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
            payload = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product,
                "KRX_FWDG_ORD_ORGNO": "",
                "ORGN_ODNO": broker_order_id,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",  # 02 = cancel
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",  # cancel the entire unfilled remainder
            }
        elif market == "US":
            tr_id = "VTTT1004U" if self.settings.mode == "paper" else "TTTT1004U"
            endpoint = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
            payload = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product,
                "OVRS_EXCG_CD": infer_us_exchange_code(ticker),
                "PDNO": ticker,
                "ORGN_ODNO": broker_order_id,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(quantity),
                "OVRS_ORD_UNPR": "0",
                "ORD_SVR_DVSN_CD": "0",
            }
        else:
            raise BrokerError(f"Unsupported market: {market}")

        logger.info(
            "Cancelling KIS order %s (%s:%s qty=%d)",
            broker_order_id, market, ticker, quantity,
        )
        raw = self.client.post(endpoint, payload, tr_id=tr_id, include_auth=True, include_hash=True)
        accepted = str(raw.get("rt_cd", "0")) == "0"
        message = str(raw.get("msg1", raw.get("message", "")))
        output = raw.get("output", {}) if isinstance(raw.get("output"), dict) else {}
        cancel_id = str(output.get("ODNO") or output.get("odno") or broker_order_id)
        logger.info("KIS cancel result: accepted=%s id=%s msg=%s", accepted, cancel_id, message)
        return OrderResult(self.name, accepted, cancel_id, message, raw)

    # ── Account / balance queries ────────────────────────────────────

    def get_cash_balance(self, market: Market) -> AccountBalance:
        if market == "KR":
            return self._get_kr_balance()
        if market == "US":
            return self._get_us_balance()
        raise BrokerError(f"Unsupported market: {market}")

    def get_positions(self, market: Market) -> list[Position]:
        if market == "KR":
            return self._get_kr_balance(return_positions=True)  # type: ignore[return-value]
        if market == "US":
            return self._get_us_positions()
        raise BrokerError(f"Unsupported market: {market}")

    def _get_kr_balance(self, return_positions: bool = False):
        tr_id = "VTTC8434R" if self.settings.mode == "paper" else "TTTC8434R"
        positions: list[Position] = []
        ctx_fk100 = ""
        ctx_nk100 = ""
        cash = 0.0
        securities_value = 0.0
        total_pnl = 0.0
        total_cost = 0.0
        raw_dump: dict[str, Any] = {}

        for _ in range(20):  # safety cap
            params = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": ctx_fk100,
                "CTX_AREA_NK100": ctx_nk100,
            }
            raw = self.client.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                params,
                tr_id=tr_id,
            )
            raw_dump = raw
            for row in raw.get("output1", []) or []:
                try:
                    qty = int(float(row.get("hldg_qty") or 0))
                except (TypeError, ValueError):
                    qty = 0
                if qty <= 0:
                    continue
                avg = float(row.get("pchs_avg_pric") or 0)
                price = float(row.get("prpr") or 0)
                value = float(row.get("evlu_amt") or qty * price)
                pnl = float(row.get("evlu_pfls_amt") or (price - avg) * qty)
                pnl_pct = (pnl / (avg * qty) * 100) if (avg * qty) else 0.0
                positions.append(
                    Position(
                        broker=self.name,
                        ticker=str(row.get("pdno") or ""),
                        market="KR",
                        quantity=qty,
                        avg_price=avg,
                        current_price=price,
                        market_value=value,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        currency="KRW",
                        raw=row,
                    )
                )
                securities_value += value
                total_pnl += pnl
                total_cost += avg * qty
            summaries = raw.get("output2", []) or []
            if summaries:
                summary = summaries[0]
                cash = float(summary.get("dnca_tot_amt") or summary.get("prvs_rcdl_excc_amt") or 0)
                if not securities_value:
                    securities_value = float(summary.get("scts_evlu_amt") or 0)
                if not total_pnl:
                    total_pnl = float(summary.get("evlu_pfls_smtl_amt") or 0)
            if raw.get("tr_cont", "") not in {"M", "F"}:
                break
            ctx_fk100 = str(raw.get("ctx_area_fk100", "")).strip()
            ctx_nk100 = str(raw.get("ctx_area_nk100", "")).strip()
            if not (ctx_fk100 or ctx_nk100):
                break

        if return_positions:
            return positions

        total = cash + securities_value
        pnl_pct = (total_pnl / total_cost * 100) if total_cost else None
        return AccountBalance(
            broker=self.name,
            market="KR",
            currency="KRW",
            cash=cash,
            securities_value=securities_value,
            total_value=total,
            pnl=total_pnl or None,
            pnl_pct=pnl_pct,
            raw=raw_dump,
        )

    def _get_us_balance(self) -> AccountBalance:
        # KIS overseas: 외화예수금 + 보유잔고를 함께 보려면 inquire-present-balance.
        tr_id = "VTRP6504R" if self.settings.mode == "paper" else "CTRP6504R"
        params = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product,
            "WCRC_FRCR_DVSN_CD": "02",  # 외화기준
            "NATN_CD": "840",  # 미국
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }
        try:
            raw = self.client.get(
                "/uapi/overseas-stock/v1/trading/inquire-present-balance",
                params,
                tr_id=tr_id,
            )
        except BrokerError as exc:
            # Fall back to positions+0 cash if the present-balance API is unavailable.
            logger.warning("KIS US present-balance unavailable, falling back: %s", exc)
            positions = self._get_us_positions()
            securities_value = sum(p.market_value for p in positions)
            pnl = sum(p.pnl for p in positions)
            return AccountBalance(
                broker=self.name,
                market="US",
                currency="USD",
                cash=0.0,
                securities_value=securities_value,
                total_value=securities_value,
                pnl=pnl or None,
                pnl_pct=None,
                raw={"fallback": True},
            )

        cash = 0.0
        for row in raw.get("output2", []) or []:
            if str(row.get("crcy_cd") or "").upper() == "USD":
                cash = float(row.get("frcr_dncl_amt_2") or row.get("frcr_dncl_amt") or 0)
                break
        sec_value = 0.0
        pnl = 0.0
        cost = 0.0
        for row in raw.get("output1", []) or []:
            qty = int(float(row.get("ord_psbl_qty1") or row.get("ord_psbl_qty")
                             or row.get("cblc_qty13") or row.get("ovrs_cblc_qty") or 0))
            if qty <= 0:
                continue
            avg = float(row.get("pchs_avg_pric") or row.get("avg_unpr3") or 0)
            price = float(row.get("now_pric2") or row.get("ovrs_now_pric1") or 0)
            value = float(row.get("frcr_evlu_amt2") or qty * price)
            row_pnl = float(row.get("frcr_evlu_pfls_amt") or row.get("evlu_pfls_amt2")
                            or (price - avg) * qty)
            sec_value += value
            pnl += row_pnl
            cost += avg * qty

        # ── Fallback: present-balance returns 0 holdings for paper accounts
        # even when positions exist. Cross-check against inquire-balance and
        # use it as the source of truth when present-balance is empty.
        if sec_value == 0:
            try:
                positions = self._get_us_positions()
            except Exception as exc:
                logger.debug("US balance fallback to positions failed: %s", exc)
                positions = []
            if positions:
                sec_value = sum(p.market_value for p in positions)
                pnl = sum(p.pnl for p in positions)
                cost = sum(p.avg_price * p.quantity for p in positions)
                logger.info(
                    "US balance: present-balance returned 0 holdings; "
                    "using inquire-balance fallback (%d positions, $%.2f value)",
                    len(positions), sec_value,
                )

        # ── Cash fallback: KIS paper accounts report 0 in output2.frcr_dncl_amt_2
        # for all currencies even when foreign cash is allocated (USD/CNY/HKD/JPY).
        # output3.frcr_evlu_tota holds the KRW-equivalent total of foreign assets
        # (cash + securities). Subtracting the KRW-equivalent securities gives us
        # the available foreign cash, which we convert back to USD for sizing.
        if cash == 0:
            output3 = raw.get("output3") or {}
            frcr_evlu_tota_krw = float(output3.get("frcr_evlu_tota") or 0)
            usd_rate = 0.0
            for row in raw.get("output2", []) or []:
                if str(row.get("crcy_cd") or "").upper() == "USD":
                    usd_rate = float(row.get("frst_bltn_exrt") or 0)
                    break
            if frcr_evlu_tota_krw > 0 and usd_rate > 0:
                securities_krw = sec_value * usd_rate
                cash_krw_est = max(0.0, frcr_evlu_tota_krw - securities_krw)
                cash_est = cash_krw_est / usd_rate
                if cash_est > 0:
                    cash = cash_est
                    logger.info(
                        "US balance: paper account cash=0 in output2; "
                        "estimated $%.2f from frcr_evlu_tota=%.0f KRW - "
                        "securities=%.0f KRW @ rate=%.2f",
                        cash, frcr_evlu_tota_krw, securities_krw, usd_rate,
                    )

        total = cash + sec_value
        pnl_pct = (pnl / cost * 100) if cost else None
        return AccountBalance(
            broker=self.name,
            market="US",
            currency="USD",
            cash=cash,
            securities_value=sec_value,
            total_value=total,
            pnl=pnl or None,
            pnl_pct=pnl_pct,
            raw=raw,
        )

    def _get_us_positions(self) -> list[Position]:
        tr_id = "VTTS3012R" if self.settings.mode == "paper" else "TTTS3012R"
        out: list[Position] = []
        # KIS US balance returns up to 99 rows per page with continuation keys.
        for excd in ("NASD", "NYSE", "AMEX"):
            ctx_fk = ""
            ctx_nk = ""
            for _ in range(10):
                params = {
                    "CANO": self.settings.account_no,
                    "ACNT_PRDT_CD": self.settings.account_product,
                    "OVRS_EXCG_CD": excd,
                    "TR_CRCY_CD": "USD",
                    "CTX_AREA_FK200": ctx_fk,
                    "CTX_AREA_NK200": ctx_nk,
                }
                try:
                    raw = self.client.get(
                        "/uapi/overseas-stock/v1/trading/inquire-balance",
                        params,
                        tr_id=tr_id,
                    )
                except BrokerError as exc:
                    logger.warning("KIS US balance fetch failed (%s): %s", excd, exc)
                    break
                for row in raw.get("output1", []) or []:
                    qty = int(float(row.get("ovrs_cblc_qty") or 0))
                    if qty <= 0:
                        continue
                    avg = float(row.get("pchs_avg_pric") or 0)
                    price = float(row.get("now_pric2") or row.get("ovrs_now_pric1") or 0)
                    value = float(row.get("ovrs_stck_evlu_amt") or qty * price)
                    pnl = float(row.get("frcr_evlu_pfls_amt") or (price - avg) * qty)
                    cost = avg * qty
                    pnl_pct = (pnl / cost * 100) if cost else 0.0
                    out.append(
                        Position(
                            broker=self.name,
                            ticker=str(row.get("ovrs_pdno") or row.get("pdno") or ""),
                            market="US",
                            quantity=qty,
                            avg_price=avg,
                            current_price=price,
                            market_value=value,
                            pnl=pnl,
                            pnl_pct=pnl_pct,
                            currency="USD",
                            raw=row,
                        )
                    )
                if raw.get("tr_cont", "") not in {"M", "F"}:
                    break
                ctx_fk = str(raw.get("ctx_area_fk200", "")).strip()
                ctx_nk = str(raw.get("ctx_area_nk200", "")).strip()
                if not (ctx_fk or ctx_nk):
                    break
        return out

    # ── Order fill status ────────────────────────────────────────────

    def get_order_fill(
        self, broker_order_id: str, market: Market, ordered_quantity: int
    ) -> OrderFill:
        if not broker_order_id:
            return OrderFill(
                broker_order_id="",
                status="submitted",
                filled_quantity=0,
                ordered_quantity=ordered_quantity,
                avg_fill_price=None,
                message="Missing broker_order_id",
            )
        if market == "KR":
            return self._get_kr_fill(broker_order_id, ordered_quantity)
        if market == "US":
            return self._get_us_fill(broker_order_id, ordered_quantity)
        raise BrokerError(f"Unsupported market: {market}")

    def _get_kr_fill(self, broker_order_id: str, ordered_quantity: int) -> OrderFill:
        tr_id = "VTTC8001R" if self.settings.mode == "paper" else "TTTC8001R"
        today = date.today()
        start = (today - timedelta(days=7)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        ctx_fk = ""
        ctx_nk = ""
        matched: dict[str, Any] | None = None
        for _ in range(5):
            params = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product,
                "INQR_STRT_DT": start,
                "INQR_END_DT": end,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": broker_order_id,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk,
            }
            try:
                raw = self.client.get(
                    "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                    params,
                    tr_id=tr_id,
                )
            except BrokerError as exc:
                return OrderFill(
                    broker_order_id=broker_order_id,
                    status="submitted",
                    filled_quantity=0,
                    ordered_quantity=ordered_quantity,
                    avg_fill_price=None,
                    message=f"조회 실패: {exc}",
                )
            for row in raw.get("output1", []) or []:
                if str(row.get("odno") or "").strip() == broker_order_id.strip():
                    matched = row
                    break
            if matched:
                break
            if raw.get("tr_cont", "") not in {"M", "F"}:
                break
            ctx_fk = str(raw.get("ctx_area_fk100", "")).strip()
            ctx_nk = str(raw.get("ctx_area_nk100", "")).strip()
            if not (ctx_fk or ctx_nk):
                break
        if not matched:
            return OrderFill(
                broker_order_id=broker_order_id,
                status="submitted",
                filled_quantity=0,
                ordered_quantity=ordered_quantity,
                avg_fill_price=None,
                message="아직 체결 내역 없음",
            )
        ordered = int(float(matched.get("ord_qty") or ordered_quantity))
        filled = int(float(matched.get("tot_ccld_qty") or 0))
        avg = float(matched.get("avg_prvs") or matched.get("tot_ccld_amt", 0) and 0) or None
        if not avg:
            tot_amt = float(matched.get("tot_ccld_amt") or 0)
            avg = (tot_amt / filled) if filled else None
        cancel_qty = int(float(matched.get("cncl_cfrm_qty") or matched.get("rmn_qty") or 0))
        status = _classify_fill_status(ordered, filled, cancel_qty, matched.get("ccld_dvsn_name", ""))
        return OrderFill(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled,
            ordered_quantity=ordered,
            avg_fill_price=avg,
            message=str(matched.get("ccld_dvsn_name") or ""),
            raw=matched,
        )

    def _get_us_fill(self, broker_order_id: str, ordered_quantity: int) -> OrderFill:
        tr_id = "VTTS3035R" if self.settings.mode == "paper" else "TTTS3035R"
        today = date.today()
        start = (today - timedelta(days=14)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        ctx_fk = ""
        ctx_nk = ""
        matched: dict[str, Any] | None = None
        for _ in range(5):
            params = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product,
                "PDNO": "%",
                "ORD_STRT_DT": start,
                "ORD_END_DT": end,
                "SLL_BUY_DVSN": "00",
                "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": "%",
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": broker_order_id,
                "CTX_AREA_NK200": ctx_nk,
                "CTX_AREA_FK200": ctx_fk,
            }
            try:
                raw = self.client.get(
                    "/uapi/overseas-stock/v1/trading/inquire-ccnl",
                    params,
                    tr_id=tr_id,
                )
            except BrokerError as exc:
                return OrderFill(
                    broker_order_id=broker_order_id,
                    status="submitted",
                    filled_quantity=0,
                    ordered_quantity=ordered_quantity,
                    avg_fill_price=None,
                    message=f"조회 실패: {exc}",
                )
            for row in raw.get("output", []) or []:
                if str(row.get("odno") or "").strip() == broker_order_id.strip():
                    matched = row
                    break
            if matched:
                break
            if raw.get("tr_cont", "") not in {"M", "F"}:
                break
            ctx_fk = str(raw.get("ctx_area_fk200", "")).strip()
            ctx_nk = str(raw.get("ctx_area_nk200", "")).strip()
            if not (ctx_fk or ctx_nk):
                break
        if not matched:
            return OrderFill(
                broker_order_id=broker_order_id,
                status="submitted",
                filled_quantity=0,
                ordered_quantity=ordered_quantity,
                avg_fill_price=None,
                message="아직 체결 내역 없음",
            )
        ordered = int(float(matched.get("ft_ord_qty") or matched.get("ord_qty") or ordered_quantity))
        filled = int(float(matched.get("ft_ccld_qty") or matched.get("ccld_qty") or 0))
        avg = float(matched.get("ft_ccld_unpr3") or matched.get("ccld_unpr") or 0) or None
        status_label = str(matched.get("prcs_stat_name") or matched.get("ccld_dvsn_name") or "")
        cancel_qty = int(float(matched.get("rjct_qty") or 0))
        status = _classify_fill_status(ordered, filled, cancel_qty, status_label)
        return OrderFill(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled,
            ordered_quantity=ordered,
            avg_fill_price=avg,
            message=status_label,
            raw=matched,
        )


def _domestic_tr_id(mode: str, side: str) -> str:
    if mode == "paper":
        return "VTTC0802U" if side == "buy" else "VTTC0801U"
    return "TTTC0802U" if side == "buy" else "TTTC0801U"


def _overseas_tr_id(mode: str, side: str) -> str:
    if mode == "paper":
        return "VTTT1002U" if side == "buy" else "VTTT1001U"
    return "TTTT1002U" if side == "buy" else "TTTT1006U"


def _classify_fill_status(
    ordered: int, filled: int, cancel_qty: int, label: str
) -> OrderStatus:
    label_lower = (label or "").lower()
    if any(token in label for token in ("취소", "거부")) or "cancel" in label_lower or "reject" in label_lower:
        if filled > 0:
            return "partially_filled"
        return "cancelled"
    if ordered and filled >= ordered:
        return "filled"
    if filled > 0:
        return "partially_filled"
    return "submitted"
