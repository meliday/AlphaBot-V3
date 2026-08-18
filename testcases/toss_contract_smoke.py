"""Read-only contract smoke test against the live Toss Open API.

Everything this bot knows about Toss was verified against the OpenAPI
document, not the wire. This script closes that gap the moment credentials
exist: it hits every **read-only** endpoint the bot depends on and checks
the exact parser assumptions the code makes — field names, decimal-string
formats, enum values, pagination shapes, and the 404 behaviour the
protective-stop status check relies on.

It never places, modifies, or cancels anything. ``TOSS_ENABLE_LIVE_ORDERS``
is irrelevant here and can stay false.

Run:  PYTHONPATH=src python3.12 testcases/toss_contract_smoke.py

Exit codes: 0 = all checks passed · 1 = at least one FAIL · 2 = no credentials.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str):
    def wrap(fn):
        def run(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs)
                RESULTS.append(("PASS", name, detail or ""))
            except SkipCheck as exc:
                RESULTS.append(("SKIP", name, str(exc)))
            except Exception as exc:
                RESULTS.append(("FAIL", name, f"{exc.__class__.__name__}: {exc}"))
                traceback.print_exc()
        return run
    return wrap


class SkipCheck(Exception):
    pass


def _decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise AssertionError(f"{field} is not a decimal string: {value!r}") from exc


def main() -> int:
    try:
        from alpha_bot.broker.toss import TossBroker, TossRestClient, TossSettings
        settings = TossSettings.from_env()
    except Exception as exc:
        print(f"토스 자격증명이 없습니다 — .env에 TOSS_CLIENT_ID/SECRET을 넣고 재실행하세요. ({exc})")
        return 2

    client = TossRestClient(settings)
    broker = TossBroker(settings, client=client)

    @check("oauth token")
    def token():
        tok = client.ensure_token() if hasattr(client, "ensure_token") else None
        if tok is None:
            # Token acquisition is implicit in the first request; force one.
            client.request("GET", "/api/v1/market-calendar/KR", idempotent=True)
            return "implicit via first request"
        return f"len={len(tok)}"

    @check("accounts")
    def accounts():
        raw = client.request("GET", "/api/v1/accounts", idempotent=True)
        rows = raw.get("result")
        assert isinstance(rows, list), "accounts result is not an array"
        if not rows:
            return "no BROKERAGE account yet (주문·잔고 검사는 SKIP됩니다)"
        assert all("accountSeq" in r for r in rows), "accountSeq missing"
        return f"{len(rows)} account(s)"

    @check("market calendar KR — parser yields regular sessions")
    def calendar_kr():
        from alpha_bot.market_calendar import _regular_windows
        raw = client.request("GET", "/api/v1/market-calendar/KR", idempotent=True)
        windows = _regular_windows(raw.get("result") or {}, "KR")
        assert windows, "no regular session parsed from a 3-day KR payload"
        return f"{len(windows)} session(s), first {windows[0].start.isoformat()}"

    @check("market calendar US — KST-midnight spanning sessions")
    def calendar_us():
        from alpha_bot.market_calendar import _regular_windows
        raw = client.request("GET", "/api/v1/market-calendar/US", idempotent=True)
        windows = _regular_windows(raw.get("result") or {}, "US")
        assert windows, "no regular session parsed from a 3-day US payload"
        spans_midnight = any(w.start.date() != w.end.date() for w in windows)
        return f"{len(windows)} session(s), midnight-span={spans_midnight}"

    @check("stocks metadata (005930, AAPL)")
    def stocks():
        raw = client.request(
            "GET", "/api/v1/stocks", params={"symbols": "005930,AAPL"}, idempotent=True
        )
        rows = {str(r.get("symbol")): r for r in raw.get("result") or []}
        assert "005930" in rows and "AAPL" in rows, f"missing symbols: {list(rows)}"
        kr = rows["005930"]
        assert kr.get("securityType"), "securityType missing (tick-size table depends on it)"
        detail = kr.get("koreanMarketDetail")
        assert isinstance(detail, dict), "koreanMarketDetail missing for a KR symbol"
        for key in ("liquidationTrading", "krxTradingSuspended"):
            assert key in detail, f"{key} missing from koreanMarketDetail"
        assert rows["AAPL"].get("koreanMarketDetail") is None, "US symbol carries KR detail"
        return f"KR status={kr.get('status')} type={kr.get('securityType')}"

    @check("warnings endpoint shape (005930)")
    def warnings():
        raw = client.request(
            "GET", "/api/v1/stocks/005930/warnings", idempotent=True
        )
        rows = raw.get("result")
        assert isinstance(rows, list), "warnings result is not an array"
        types = [str(r.get("warningType")) for r in rows if isinstance(r, dict)]
        return f"{len(rows)} active: {types or '없음'}"

    @check("tradability gate end-to-end (005930)")
    def tradability():
        reason = broker.tradability_block("005930", "KR")
        return f"block={reason or 'tradable'}"

    @check("prices batch")
    def prices():
        raw = client.request(
            "GET", "/api/v1/prices", params={"symbols": "005930,AAPL"}, idempotent=True
        )
        rows = raw.get("result") or []
        assert rows, "empty prices result"
        for row in rows:
            _decimal(row.get("lastPrice"), f"lastPrice[{row.get('symbol')}]")
        return ", ".join(f"{r.get('symbol')}={r.get('lastPrice')}" for r in rows)

    @check("daily candles + pagination (005930)")
    def candles():
        raw = client.request(
            "GET", "/api/v1/candles",
            params={"symbol": "005930", "interval": "1d", "count": 5},
            idempotent=True,
        )
        result = raw.get("result") or {}
        rows = result.get("candles") or []
        assert rows, "no candles returned"
        for field in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume"):
            _decimal(rows[0].get(field), field)
        assert "nextBefore" in result, "nextBefore missing (pagination contract)"
        ts = rows[0].get("timestamp", "")
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))  # must parse
        return f"{len(rows)} bars, nextBefore={'set' if result.get('nextBefore') else 'null'}"

    @check("exchange rate — midRate usable")
    def fx():
        rate = broker.usd_krw_rate()
        assert 500 < rate < 5000, f"USD/KRW {rate} outside sanity band"
        return f"USD/KRW mid ≈ {rate}"

    @check("holdings summary shape")
    def holdings():
        raw = client.request(
            "GET", "/api/v1/holdings", account_seq=broker.account_seq, idempotent=True
        )
        result = raw.get("result") or {}
        amount = (result.get("marketValue") or {}).get("amount")
        assert isinstance(amount, dict) and "krw" in amount, \
            "marketValue.amount.{krw,usd} shape changed (portfolio_value depends on it)"
        return f"krw={amount.get('krw')} usd={amount.get('usd')}"

    @check("buying power KRW/USD")
    def buying_power():
        out = []
        for cur in ("KRW", "USD"):
            raw = client.request(
                "GET", "/api/v1/buying-power",
                params={"currency": cur},
                account_seq=broker.account_seq, idempotent=True,
            )
            value = _decimal(
                (raw.get("result") or {}).get("cashBuyingPower"), f"cashBuyingPower[{cur}]"
            )
            out.append(f"{cur}={value}")
        return " ".join(out)

    @check("portfolio valuation end-to-end")
    def portfolio():
        krw = broker.portfolio_value("KRW")
        usd = broker.portfolio_value("USD")
        assert krw >= 0 and usd >= 0
        return f"₩{krw:,.0f} / ${usd:,.2f}"

    @check("conditional-order list (OPEN) + protective-stop filter")
    def conditional_list():
        rows = broker.list_conditional_orders("OPEN")
        for row in rows:
            assert row.get("conditionalOrderId"), "conditionalOrderId missing"
            assert row.get("type") in {"SINGLE", "OCO", "OTO"}, f"unknown type {row.get('type')}"
        like_ours = broker.list_protective_stop_ids("005930")
        no_client_id = all("clientOrderId" not in row for row in rows)
        return (
            f"{len(rows)} open, stop-shaped on 005930: {len(like_ours)}, "
            f"clientOrderId omitted from list (예상대로): {no_client_id}"
        )

    @check("protective_stop_status(nonexistent) → None")
    def missing_stop_status():
        # Live finding: a malformed id gets 400 invalid-request (format is
        # validated before existence), not the spec's 404 — both must map
        # to None or the re-arm logic would crash instead of recovering.
        status = broker.protective_stop_status("SMOKE-NONEXISTENT-ID")
        assert status is None, f"expected None for a missing stop, got {status!r}"
        return "400/404 → None (재무장 로직의 소멸 감지 경로 검증)"

    token()
    accounts()
    calendar_kr()
    calendar_us()
    stocks()
    warnings()
    tradability()
    prices()
    candles()
    fx()

    has_account = any(
        status == "PASS" and "account(s)" in detail
        for status, name, detail in RESULTS if name == "accounts"
    )
    if has_account:
        holdings()
        buying_power()
        portfolio()
        conditional_list()
        missing_stop_status()
    else:
        for name in (
            "holdings summary shape", "buying power KRW/USD",
            "portfolio valuation end-to-end",
            "conditional-order list (OPEN) + protective-stop filter",
            "protective_stop_status(nonexistent) → None",
        ):
            RESULTS.append(("SKIP", name, "계좌 없음"))

    width = max(len(name) for _, name, _ in RESULTS)
    print()
    for status, name, detail in RESULTS:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}[status]
        print(f"{icon} {status:4s} {name:{width}s}  {detail}")
    fails = sum(1 for status, *_ in RESULTS if status == "FAIL")
    passes = sum(1 for status, *_ in RESULTS if status == "PASS")
    print(f"\n{passes} passed, {fails} failed, "
          f"{sum(1 for s, *_ in RESULTS if s == 'SKIP')} skipped "
          f"@ {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if fails:
        print("→ FAIL 항목의 파서 가정이 실서버와 다릅니다. 해당 코드부터 고치세요.")
    else:
        print("→ 파서 가정이 실서버와 일치합니다. 다음 단계: 1주 지정가 수동 매수/취소.")
    return 1 if fails else 0


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
