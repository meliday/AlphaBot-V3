"""Stage-2 live verification: one tiny order through the production path.

The contract smoke test proved the read-only surface; this proves the
write surface with the smallest possible real order — 1 share, notional
capped — routed through the exact code the auto-pilot uses
(``ApprovalQueue.enqueue → approve → TossBroker.place_order``), not a
side channel. What each phase verifies:

  preflight  session/cash/price/tick math, prints the would-be order. No order.
  place      1-share limit ~10% BELOW market (should rest unfilled):
             clientOrderId idempotency, tick rounding, scoping fields,
             queue status transitions.
  status     sync_with_broker → Toss status mapping (PENDING→submitted …).
  cancel     POST /orders/{id}/cancel → CANCELED → queue "cancelled".
  fill       1-share marketable limit (~+1%) that should execute:
             fill sync, avg_fill_price, holdings impact. The share is kept
             on purpose — it becomes the guinea pig for stage 3
             (protective_stop arming on a real position).
  sellback   market-sells the test share and links it as the buy's exit,
             if you would rather not proceed to stage 3 yet.

Every phase is a separate, explicit invocation — that is the human
confirmation step. The broker itself additionally refuses everything
until ``TOSS_ENABLE_LIVE_ORDERS=true``.

Run:
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py preflight
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py place
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py status
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py cancel
  # optional, executes a real 1-share purchase:
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py fill
  PYTHONPATH=src python3.12 testcases/toss_tiny_order_test.py sellback
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TICKER = "F"          # cheap, ultra-liquid; override with --ticker
MAX_NOTIONAL_USD = 30.0       # hard refusal above this, belt-and-braces
QUEUE_PATH = Path("pending_orders.json")  # the production queue, on purpose


def build() -> tuple:
    from alpha_bot.approval import ApprovalQueue
    from alpha_bot.broker.toss import TossBroker, TossSettings

    settings = TossSettings.from_env()
    broker = TossBroker(settings)
    queue = ApprovalQueue(QUEUE_PATH)
    return settings, broker, queue


def last_price(broker, ticker: str) -> float:
    raw = broker.client.request(
        "GET", "/api/v1/prices", params={"symbols": ticker}, idempotent=True
    )
    rows = raw.get("result") or []
    if not rows:
        raise SystemExit(f"시세 없음: {ticker}")
    return float(rows[0]["lastPrice"])


def session_note() -> str:
    from alpha_bot.market_calendar import get_sessions
    now = datetime.now(timezone.utc)
    sessions = get_sessions("US", now=now)
    if not sessions:
        return "세션 정보 없음 (캘린더 미응답) — 주문 시 order-hours-closed 가능"
    current = next((w for w in sessions if w.contains(now)), None)
    if current:
        return f"US 정규장 진행 중 (KST {current.end.astimezone().strftime('%H:%M')} 종료)"
    upcoming = next((w.start for w in sessions if w.start > now), None)
    return (
        f"US 정규장 아님 — 다음 세션 {upcoming.astimezone().strftime('%m-%d %H:%M KST')}"
        if upcoming else "US 정규장 아님"
    ) + " (토스 US는 확장세션도 주문 가능하나 지정가 검증은 정규장 권장)"


def find_test_buy(queue, ticker: str):
    """The most recent bot-side row for the test ticker, any state."""
    rows = [
        o for o in queue.list_orders()
        if o.request.ticker == ticker.upper() and o.request.side == "buy"
    ]
    return rows[-1] if rows else None


def show(order) -> None:
    if order is None:
        print("  (큐에 해당 주문 없음)")
        return
    print(
        f"  {order.id}  {order.request.side} {order.request.quantity}주 "
        f"@ {order.request.limit_price}  status={order.status}  "
        f"filled={order.filled_quantity}@{order.avg_fill_price}  "
        f"broker_ref={order.broker_order_id}  scope={order.broker_instance_id}"
    )


def guard_notional(price: float, cap: float) -> None:
    if price > cap:
        raise SystemExit(
            f"1주 가격 ${price:.2f} > 상한 ${cap:.2f} — 더 싼 티커를 "
            f"--ticker 로 지정하거나 --max-notional 을 조정하세요."
        )


def cmd_preflight(args) -> None:
    settings, broker, queue = build()
    price = last_price(broker, args.ticker)
    guard_notional(price, args.max_notional)
    limit_rest = round(price * 0.90, 2)
    bal = broker.get_cash_balance("US")
    print(f"세션      : {session_note()}")
    print(f"티커      : {args.ticker}  현재가 ${price}")
    print(f"휴면 지정가: ${limit_rest} (현재가 -10%, 틱 반올림은 전송 시 자동)")
    print(f"현금(USD) : {bal.cash}")
    print(f"live 주문 : {'허용됨' if settings.enable_live_orders else '차단됨 (TOSS_ENABLE_LIVE_ORDERS=false)'}")
    existing = find_test_buy(queue, args.ticker)
    if existing and existing.status in {"pending", "submitting", "unknown", "submitted", "partially_filled"}:
        print("⚠️ 활성 상태의 기존 테스트 주문이 있습니다 — cancel 부터 실행하세요:")
        show(existing)
    print("\n다음: place (휴면 지정가 1주 — 체결되지 않는 것이 정상)")


def _place(args, *, marketable: bool) -> None:
    from alpha_bot.models import OrderRequest

    settings, broker, queue = build()
    if not settings.enable_live_orders:
        raise SystemExit("TOSS_ENABLE_LIVE_ORDERS=true 로 바꾼 뒤 실행하세요.")
    price = last_price(broker, args.ticker)
    guard_notional(price, args.max_notional)
    limit = round(price * (1.01 if marketable else 0.90), 2)

    label = "체결형(+1%)" if marketable else "휴면형(-10%)"
    print(f"전송 예정: {args.ticker} 1주 지정가 ${limit} ({label}, 현재가 ${price})")
    order = queue.enqueue(
        OrderRequest(
            ticker=args.ticker.upper(), market="US", side="buy",
            quantity=1, order_type="limit", limit_price=limit,
            reason=f"tiny-order live verification ({label})",
        ),
        broker=broker,               # tick rounding + scoping happen here
        # A real risk frame so the kept share can drive stage 3
        # (protective-stop arming) without hand-editing the row later.
        stop_loss=round(price * 0.93, 2),
        target1=round(price * 1.50, 2),
        target2=round(price * 2.00, 2),
        analysis_signal="Buy",
    )
    print(f"큐 등록: {order.id} (clientOrderId={order.request.client_order_id})")
    approved, result = queue.approve(order.id, broker)
    print(f"브로커 응답: accepted={result.accepted} broker_ref={result.broker_order_id}")
    show(approved)
    print("\n다음: status (그리고 휴면형이면 cancel / 체결형이면 체결 확인)")


def cmd_place(args) -> None:
    _place(args, marketable=False)


def cmd_fill(args) -> None:
    print("⚠️ 이 명령은 실제로 1주를 매수합니다.")
    _place(args, marketable=True)


def cmd_status(args) -> None:
    _, broker, queue = build()
    changed = queue.sync_with_broker(broker)
    for o in changed:
        print(f"변경: {o.id} → {o.status} ({o.filled_quantity}/{o.request.quantity})")
    if not changed:
        print("(상태 변화 없음)")
    show(find_test_buy(queue, args.ticker))


def cmd_cancel(args) -> None:
    _, broker, queue = build()
    order = find_test_buy(queue, args.ticker)
    if order is None or not order.broker_order_id:
        raise SystemExit("취소할 브로커 주문이 없습니다 (place 먼저, status 로 확인).")
    if order.status not in {"submitted", "partially_filled", "unknown"}:
        raise SystemExit(f"취소 불가 상태: {order.status}")
    print(f"취소 요청: {order.id} → broker_ref {order.broker_order_id}")
    result = broker.cancel_order(
        order.broker_order_id, "US", order.request.ticker, order.request.quantity
    )
    print(f"브로커 응답: accepted={result.accepted} msg={result.message}")
    queue.sync_with_broker(broker)
    after = find_test_buy(queue, args.ticker)
    show(after)
    ok = after is not None and after.status in {"cancelled", "partially_filled_cancelled"}
    print("✅ 취소 → 큐 상태 매핑 정상" if ok else "⚠️ 상태가 아직 반영되지 않음 — status 재실행")


def cmd_sellback(args) -> None:
    from alpha_bot.models import OrderRequest

    _, broker, queue = build()
    queue.sync_with_broker(broker)
    buy = find_test_buy(queue, args.ticker)
    if buy is None or buy.status not in {"filled", "partially_filled"} or buy.filled_quantity <= 0:
        raise SystemExit("되팔 체결 포지션이 없습니다 (fill 이 filled 상태여야 함).")
    qty = buy.filled_quantity
    print(f"⚠️ 실제 매도: {args.ticker} {qty}주 시장가")
    sell = queue.enqueue(
        OrderRequest(
            ticker=buy.request.ticker, market="US", side="sell",
            quantity=qty, order_type="market", limit_price=None,
            reason="tiny-order verification sell-back",
        ),
        broker=broker,
    )
    # Link as the buy's exit so downstream accounting sees a closed pair,
    # exactly as trigger_forced_exit does.
    queue.update(replace(buy, exit_order_id=sell.id, exit_reason="verification"))
    approved, result = queue.approve(sell.id, broker)
    print(f"브로커 응답: accepted={result.accepted} broker_ref={result.broker_order_id}")
    show(approved)
    print("\nstatus 로 체결 확인 후 종료. (이 쌍은 7일 뒤 자동 아카이브)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["preflight", "place", "status", "cancel", "fill", "sellback"])
    parser.add_argument("--ticker", default=DEFAULT_TICKER)
    parser.add_argument("--max-notional", type=float, default=MAX_NOTIONAL_USD)
    args = parser.parse_args()
    {
        "preflight": cmd_preflight,
        "place": cmd_place,
        "status": cmd_status,
        "cancel": cmd_cancel,
        "fill": cmd_fill,
        "sellback": cmd_sellback,
    }[args.phase](args)
    return 0


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
