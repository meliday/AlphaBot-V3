from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.broker.toss import (
    TossApiError,
    TossBroker,
    TossConditionLeg,
    TossConditionalOrderRequest,
    TossRestClient,
    TossSettings,
)
from alpha_bot.errors import BrokerError, BrokerOrderRejected
from alpha_bot.data import TossPriceDataProvider
from alpha_bot.models import OrderRequest


class FakeTossClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.order_status = "PENDING"
        self.filled_quantity = "0"
        self.average_price = None
        self.reject_order = False
        self.network_fail_once = False
        self.holding_quantity = "5"

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/v1/orders" and method == "POST":
            if self.network_fail_once:
                self.network_fail_once = False
                raise BrokerError("connection dropped after request")
            if self.reject_order:
                raise TossApiError(422, "stock-restricted", "restricted")
            return {"result": {"orderId": "TOSS-ORDER-1"}}
        if path == "/api/v1/stocks":
            return {
                "result": [
                    {"symbol": "069500", "securityType": "ETF"},
                    {"symbol": "005930", "securityType": "STOCK"},
                ]
            }
        if path == "/api/v1/orders/TOSS-ORDER-1" and method == "GET":
            return {
                "result": {
                    "status": self.order_status,
                    "execution": {
                        "filledQuantity": self.filled_quantity,
                        "averageFilledPrice": self.average_price,
                    },
                }
            }
        if path == "/api/v1/orders/TOSS-ORDER-1/cancel":
            return {"result": {"orderId": "TOSS-ORDER-1"}}
        if path == "/api/v1/holdings":
            return {
                "result": {
                    "items": [
                        {
                            "symbol": "NVDA",
                            "marketCountry": "US",
                            "currency": "USD",
                            "quantity": self.holding_quantity,
                            "lastPrice": "110",
                            "averagePurchasePrice": "100",
                            "marketValue": {
                                "amount": "550",
                                "purchaseAmount": "500",
                            },
                            "profitLoss": {"amount": "50", "rate": "0.1"},
                        }
                    ]
                }
            }
        if path == "/api/v1/buying-power":
            return {"result": {"currency": "USD", "cashBuyingPower": "1000"}}
        if path == "/api/v1/conditional-orders" and method == "POST":
            return {
                "result": {
                    "conditionalOrderId": "COND-1",
                    "clientOrderId": kwargs["payload"].get("clientOrderId"),
                }
            }
        if path == "/api/v1/conditional-orders" and method == "GET":
            cursor = (kwargs.get("params") or {}).get("cursor")
            if not cursor:
                return {
                    "result": {
                        "conditionalOrders": [{"conditionalOrderId": "COND-1"}],
                        "nextCursor": "next-1",
                        "hasNext": True,
                    }
                }
            return {
                "result": {
                    "conditionalOrders": [{"conditionalOrderId": "COND-2"}],
                    "nextCursor": None,
                    "hasNext": False,
                }
            }
        if path == "/api/v1/conditional-orders/COND-1" and method == "GET":
            return {"result": {"conditionalOrderId": "COND-1", "status": "WATCHING"}}
        if path == "/api/v1/conditional-orders/COND-1/modify" and method == "POST":
            return {"result": {"conditionalOrderId": "COND-NEW"}}
        if path == "/api/v1/conditional-orders/COND-1" and method == "DELETE":
            return {}
        raise AssertionError(f"unexpected Toss request: {method} {path}")


def make_broker(client: FakeTossClient) -> TossBroker:
    settings = TossSettings("client", "secret", account_seq=7, enable_live_orders=True)
    return TossBroker(settings, client=client)  # type: ignore[arg-type]


class TossBrokerContractTests(unittest.TestCase):
    def test_queue_id_becomes_toss_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTossClient()
            broker = make_broker(client)
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 2, "limit", 100.25),
                broker=broker,
            )
            updated, result = queue.approve(candidate.id, broker)

            self.assertEqual(updated.broker_order_id, "TOSS-ORDER-1")
            self.assertEqual(result.broker_order_id, "TOSS-ORDER-1")
            payload = client.calls[0][2]["payload"]
            self.assertEqual(payload["clientOrderId"], candidate.id)
            self.assertEqual(payload["price"], "100.25")
            self.assertEqual(client.calls[0][2]["account_seq"], 7)

    def test_direct_order_detail_maps_partial_and_terminal_partial(self):
        client = FakeTossClient()
        broker = make_broker(client)
        client.order_status = "PARTIAL_FILLED"
        client.filled_quantity = "2"
        client.average_price = "101.5"
        fill = broker.get_order_fill("TOSS-ORDER-1", "US", 5)
        self.assertEqual(fill.status, "partially_filled")
        self.assertEqual(fill.filled_quantity, 2)
        self.assertEqual(fill.avg_fill_price, 101.5)

        client.order_status = "CANCELED"
        fill = broker.get_order_fill("TOSS-ORDER-1", "US", 5)
        self.assertEqual(fill.status, "partially_filled_cancelled")

    def test_business_rejection_is_definitive(self):
        client = FakeTossClient()
        client.reject_order = True
        broker = make_broker(client)
        with self.assertRaises(BrokerOrderRejected):
            broker.place_order(OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0))

    def test_kr_etf_limit_is_rounded_and_persisted_before_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTossClient()
            broker = make_broker(client)
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(
                OrderRequest("069500", "KR", "buy", 1, "limit", 20_003),
                broker=broker,
            )
            self.assertEqual(candidate.request.limit_price, 20_000)
            queue.approve(candidate.id, broker)
            order_call = next(call for call in client.calls if call[1] == "/api/v1/orders")
            self.assertEqual(order_call[2]["payload"]["price"], "20000")

    def test_unknown_submission_is_recovered_with_same_client_order_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTossClient()
            client.network_fail_once = True
            broker = make_broker(client)
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                broker=broker,
            )
            with self.assertRaises(BrokerError):
                queue.approve(candidate.id, broker)
            self.assertEqual(queue.list_orders()[0].status, "unknown")

            recovered = queue.recover_unresolved_orders(broker)

            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].status, "submitted")
            self.assertEqual(recovered[0].broker_order_id, "TOSS-ORDER-1")
            post_calls = [call for call in client.calls if call[1] == "/api/v1/orders"]
            self.assertEqual(len(post_calls), 2)
            self.assertEqual(
                post_calls[0][2]["payload"]["clientOrderId"],
                post_calls[1][2]["payload"]["clientOrderId"],
            )

    def test_unknown_submission_outside_replay_window_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTossClient()
            broker = make_broker(client)
            queue = ApprovalQueue(Path(tmp) / "pending.json")
            candidate = queue.enqueue(
                OrderRequest("NVDA", "US", "buy", 1, "limit", 100.0),
                broker=broker,
            )
            queue.update(
                replace(
                    candidate,
                    status="unknown",
                    submitted_at="2020-01-01T00:00:00+00:00",
                )
            )

            self.assertEqual(queue.recover_unresolved_orders(broker), [])
            self.assertEqual(queue.list_orders()[0].status, "unknown")
            self.assertFalse(any(call[1] == "/api/v1/orders" for call in client.calls))

    def test_oco_payload_rounds_profit_up_and_protective_stop_down(self):
        client = FakeTossClient()
        broker = make_broker(client)
        request = TossConditionalOrderRequest(
            ticker="069500",
            market="KR",
            strategy="OCO",
            quantity=3,
            order_type="limit",
            expire_date=date.today() + timedelta(days=14),
            client_order_id="BOT-COND-1",
            first=TossConditionLeg("sell", 20_003, 20_003),
            second=TossConditionLeg("sell", 19_003, 19_003),
        )

        result = broker.create_conditional_order(request)

        self.assertEqual(result.conditional_order_id, "COND-1")
        call = next(
            call
            for call in client.calls
            if call[0] == "POST" and call[1] == "/api/v1/conditional-orders"
        )
        payload = call[2]["payload"]
        self.assertEqual(payload["first"]["triggerPrice"], "20005")
        self.assertEqual(payload["first"]["orderPrice"], "20005")
        self.assertEqual(payload["second"]["triggerPrice"], "19000")
        self.assertEqual(payload["second"]["orderPrice"], "19000")
        self.assertEqual(payload["clientOrderId"], "BOT-COND-1")
        self.assertTrue(call[2]["idempotent"])

    def test_conditional_history_paginates_and_modify_uses_new_id(self):
        client = FakeTossClient()
        broker = make_broker(client)
        rows = broker.list_conditional_orders("OPEN", symbol="nvda")
        self.assertEqual(
            [row["conditionalOrderId"] for row in rows], ["COND-1", "COND-2"]
        )
        self.assertEqual(broker.get_conditional_order("COND-1")["status"], "WATCHING")

        request = TossConditionalOrderRequest(
            ticker="NVDA",
            market="US",
            strategy="SINGLE",
            quantity=1,
            order_type="market",
            expire_date=date.today() + timedelta(days=1),
            first=TossConditionLeg("sell", 90),
        )
        modified = broker.modify_conditional_order("COND-1", request)
        self.assertEqual(modified.conditional_order_id, "COND-NEW")
        modify_call = next(call for call in client.calls if call[1].endswith("/modify"))
        self.assertFalse(modify_call[2]["idempotent"])
        self.assertNotIn("symbol", modify_call[2]["payload"])
        broker.cancel_conditional_order("COND-1")


class TossTokenAndAccountTests(unittest.TestCase):
    def test_force_refresh_reuses_token_created_by_another_process(self):
        settings = TossSettings("client", "secret", account_seq=7)
        client = TossRestClient(settings)
        client._access_token = "stale-token"
        with patch.object(client, "_token_lock", return_value=nullcontext()), patch.object(
            client,
            "_read_token_cache",
            return_value=("newer-token", time.time() + 3600),
        ), patch("urllib.request.urlopen") as urlopen:
            token = client.ensure_token(
                force_refresh=True, stale_token="stale-token"
            )

        self.assertEqual(token, "newer-token")
        urlopen.assert_not_called()

    def test_holdings_and_buying_power_map_to_existing_models(self):
        client = FakeTossClient()
        broker = make_broker(client)
        positions = broker.get_positions("US")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].quantity, 5)
        self.assertEqual(positions[0].pnl_pct, 10.0)
        balance = broker.get_cash_balance("US")
        self.assertEqual(balance.cash, 1000.0)
        self.assertEqual(balance.securities_value, 550.0)
        self.assertEqual(balance.total_value, 1550.0)

    def test_fractional_holding_fails_closed_instead_of_truncating(self):
        client = FakeTossClient()
        client.holding_quantity = "0.5"
        with self.assertRaises(BrokerError):
            make_broker(client).get_positions("US")


class FakeTossPriceClient:
    def __init__(self):
        end = date(2026, 8, 18)
        self.rows = []
        day = end
        while len(self.rows) < 260:
            if day.weekday() < 5:
                price = 100 + len(self.rows) * 0.1
                self.rows.append(
                    {
                        "timestamp": f"{day.isoformat()}T09:00:00+09:00",
                        "openPrice": str(price),
                        "highPrice": str(price + 1),
                        "lowPrice": str(price - 1),
                        "closePrice": str(price + 0.5),
                        "volume": "1000000",
                        "currency": "USD",
                    }
                )
            day -= timedelta(days=1)
        self.calls = 0
        self.params: list[dict] = []

    def request(self, method, path, **kwargs):
        self.calls += 1
        if path == "/api/v1/prices":
            return {
                "result": [
                    {
                        "symbol": "NVDA",
                        "timestamp": "2026-08-18T09:00:00+09:00",
                        "lastPrice": "123.45",
                        "currency": "USD",
                    }
                ]
            }
        params = kwargs["params"]
        self.params.append(dict(params))
        if "before" not in params:
            batch = self.rows[:200]
            cursor = self.rows[199]["timestamp"]
        else:
            # Inclusive cursor intentionally repeats the boundary candle;
            # provider must deduplicate it.
            batch = self.rows[199:]
            cursor = None
        return {"result": {"candles": batch, "nextBefore": cursor}}


class TossPriceProviderTests(unittest.TestCase):
    def test_adjusted_candle_pagination_and_quote(self):
        client = FakeTossPriceClient()
        settings = TossSettings("client", "secret", account_seq=7)
        provider = TossPriceDataProvider(
            settings=settings, client=client  # type: ignore[arg-type]
        )
        candles = provider.get_candles("NVDA", "US", lookback=260)
        self.assertEqual(len(candles), 260)
        self.assertLess(candles[0].date, candles[-1].date)
        self.assertEqual(client.params[1]["count"], 61)
        self.assertEqual(provider.get_current_price("NVDA", "US"), 123.45)


if __name__ == "__main__":
    unittest.main()


class GzipBodyTests(unittest.TestCase):
    """The live gateway gzips (at least) error bodies without being asked.

    Found by the contract smoke test: undecoded, every API error became
    mojibake with code "http-error", silently disabling all code-based
    handling (429 pacing, 409 replay, the rejection classifier). These pin
    the decode paths so the regression cannot return.
    """

    def test_decode_body_handles_gzip_identity_and_magic_bytes(self):
        import gzip
        from email.message import Message
        from alpha_bot.broker.toss import _decode_body

        labelled = Message()
        labelled["Content-Encoding"] = "gzip"
        self.assertEqual(
            _decode_body(gzip.compress("한글 본문 ok".encode()), labelled),
            "한글 본문 ok",
        )
        # Unlabelled but compressed — detected by the 0x1f8b magic bytes.
        self.assertEqual(
            _decode_body(gzip.compress(b"unlabelled"), Message()), "unlabelled"
        )
        # Plain identity body passes through untouched.
        self.assertEqual(_decode_body(b"plain", Message()), "plain")
        # Labelled gzip that is not actually gzip must not crash.
        self.assertEqual(_decode_body(b"not-gzip", labelled), "not-gzip")

    def test_error_codes_survive_a_gzipped_error_body(self):
        import gzip
        import io
        import json as _json
        import urllib.error
        from email.message import Message

        settings = TossSettings(client_id="c", client_secret="s", account_seq=1)
        client = TossRestClient(settings)
        body = gzip.compress(_json.dumps({
            "error": {
                "code": "invalid-request",
                "message": "형식이 잘못되었습니다.",
                "requestId": "R1",
            }
        }).encode())
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        err = urllib.error.HTTPError(
            "https://openapi.tossinvest.com/x", 400, "Bad Request",
            headers, io.BytesIO(body),
        )
        with patch.object(TossRestClient, "ensure_token", return_value="tok"), \
             patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TossApiError) as ctx:
                client.request("GET", "/api/v1/conditional-orders/xyz", idempotent=True)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid-request")  # not "http-error"
        self.assertIn("형식", str(ctx.exception))


class MissingStopStatusTests(unittest.TestCase):
    """Live finding: Toss validates the conditional-order id *format* before
    existence, so a malformed id yields 400 invalid-request, not the spec's
    404. Both mean "no such stop" to the re-arm logic."""

    def _broker(self, exc: TossApiError) -> TossBroker:
        class Client:
            def request(self, *args, **kwargs):
                raise exc

        settings = TossSettings(client_id="c", client_secret="s", account_seq=1)
        return TossBroker(settings, client=Client())  # type: ignore[arg-type]

    def test_a_404_maps_to_none(self):
        broker = self._broker(TossApiError(404, "conditional-order-not-found", "없음"))
        self.assertIsNone(broker.protective_stop_status("GONE"))

    def test_a_malformed_id_400_maps_to_none(self):
        broker = self._broker(TossApiError(400, "invalid-request", "형식 오류"))
        self.assertIsNone(broker.protective_stop_status("SMOKE-NONEXISTENT-ID"))

    def test_other_400_codes_keep_raising(self):
        broker = self._broker(TossApiError(400, "account-header-required", "헤더 누락"))
        with self.assertRaises(TossApiError):
            broker.protective_stop_status("ANY")


class CancelIdempotencyTests(unittest.TestCase):
    """Observed live: Toss cancellation is asynchronous (PENDING_CANCEL →
    CANCELED) and a repeated cancel answers 409 already-canceled. That is
    the desired end state, so the adapter reports idempotent success; a 409
    already-filled is a genuine refusal the caller must see."""

    def _broker(self, exc: TossApiError) -> TossBroker:
        class Client:
            def request(self, *args, **kwargs):
                raise exc

        settings = TossSettings(client_id="c", client_secret="s", account_seq=1)
        return TossBroker(settings, client=Client())  # type: ignore[arg-type]

    def test_already_canceled_is_idempotent_success(self):
        broker = self._broker(TossApiError(409, "already-canceled", "취소된 주문입니다."))
        result = broker.cancel_order("REF-1", "US", "F", 1)
        self.assertTrue(result.accepted)
        self.assertIn("already-canceled", result.message)

    def test_already_filled_is_a_real_refusal(self):
        broker = self._broker(TossApiError(409, "already-filled", "이미 체결된 주문입니다."))
        result = broker.cancel_order("REF-1", "US", "F", 1)
        self.assertFalse(result.accepted)

    def test_already_processing_is_a_soft_refusal_not_a_crash(self):
        broker = self._broker(TossApiError(409, "already-processing", "처리 중"))
        result = broker.cancel_order("REF-1", "US", "F", 1)
        self.assertFalse(result.accepted)  # next sweep retries
