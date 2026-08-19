"""Stage-4 live verification: make the broker-side stop actually fire.

Everything up to here proved the stop can be *placed* — armed, re-armed
on a ratchet, released before a bot-initiated sell. None of it proved the
thing the feature exists for: that when price reaches the trigger, the
venue sells, and the bot then notices its position is gone. A stop that
arms but never fires is indistinguishable from a working one right up
until the day it matters, and a stop that fires while the bot keeps
believing it holds the share is worse than no stop at all.

So this runner walks the stop down to just under the live price and lets
ordinary intraday movement do the rest. Both halves are checked: the
venue side (conditional order → triggered, share sold) and the bot side
(reconciliation sees the external close and retires the stop).

Cost: the canary share is sold, a few cents below where it sits now.
That is the whole blast radius — one share of F, bought for exactly this.

Phases (a separate invocation each — that is the confirmation):

  check    read-only. Position, live price, venue stop, session, and
           whether anything else is running that could also sell.
  plan     compute the trigger and show the consequence. Changes nothing.
  arm      move the stop to that trigger through the production path
           (queue row → sync_protective_stop). Requires --confirm.
  watch    poll until it fires, then run reconciliation and check the bot
           agrees the position is closed.
  restore  put the original stop back. For when you stop before `watch`,
           or it never triggered.

Guards, all fail-closed:
  · TOSS_ENABLE_LIVE_ORDERS must be true
  · the US regular session must be open — a market sell into a thin
    pre/after-hours book is the one thing this must not demonstrate
  · `auto` must not be running, or two engines are selling the same share

Run:
  python3 testcases/toss_stop_fire_test.py check
  python3 testcases/toss_stop_fire_test.py plan
  python3 testcases/toss_stop_fire_test.py arm --confirm
  python3 testcases/toss_stop_fire_test.py watch
  python3 testcases/toss_stop_fire_test.py restore   # only if it never fired
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

TICKER = "F"
MARKET = "US"
QUEUE_PATH = Path("pending_orders.json")
STATE_PATH = Path("testcases/.stop_fire_state.json")

# Far enough under the live price that the venue will accept it as a stop
# rather than an immediate trigger, close enough that ordinary noise
# reaches it within minutes. F moves several cents a minute in regular
# hours; 0.2% of ~$14 is about three.
DEFAULT_MARGIN_PCT = 0.2
WATCH_TIMEOUT_SECONDS = 1800
WATCH_POLL_SECONDS = 20


def build():
    from alpha_bot.approval import ApprovalQueue
    from alpha_bot.auto.analysis import make_provider
    from alpha_bot.broker.toss import TossBroker, TossSettings

    settings = TossSettings.from_env()
    broker = TossBroker(settings)
    queue = ApprovalQueue(QUEUE_PATH)
    provider = make_provider("toss", Path("data"))
    return settings, broker, queue, provider


def held_buy(queue, broker):
    from alpha_bot.auto.position_manager import find_held_buy

    return find_held_buy(queue, MARKET, TICKER, broker)


def live_price(provider) -> float | None:
    return provider.get_current_price(TICKER, MARKET)


def manage_once(queue, broker, provider) -> None:
    """The production management slice — no scanning, no buys."""
    from alpha_bot.auto.position_manager import manage_open_positions

    queue.sync_with_broker(broker)
    manage_open_positions(
        queue, broker, provider, lambda m: print(f"    {m}"), protective_stops=True
    )


def auto_is_running() -> bool:
    """A second exit engine on the same share would muddy every result."""
    from alpha_bot.auto.watchdog import check_heartbeat

    return check_heartbeat("auto", 900.0).healthy


def require_preconditions(settings) -> None:
    from alpha_bot.market_hours import market_status

    if not settings.enable_live_orders:
        raise SystemExit("TOSS_ENABLE_LIVE_ORDERS=true 필요")

    status = market_status(MARKET)
    print(f"  세션: {status.reason}")
    if not status.is_open:
        raise SystemExit(
            "US 정규장이 아닙니다. 확장세션에도 조건주문은 발동하지만, 얇은 호가에\n"
            "   시장가 매도를 유도하는 것은 이 검증의 목적이 아닙니다 — 22:30~05:00 KST."
        )
    if auto_is_running():
        raise SystemExit(
            "auto 가 돌고 있습니다. 같은 종목을 두 엔진이 팔면 결과를 해석할 수 없습니다 —\n"
            "   auto 를 멈추고 다시 실행하세요."
        )


def show(broker, provider, buy) -> float | None:
    price = live_price(provider)
    print(f"  현재가: {price if price is not None else '(조회 실패)'}")
    if buy is None:
        print("  포지션: 없음 — 이미 청산되었거나 매수 체결이 없습니다")
        return price
    print(
        f"  포지션: {buy.filled_quantity}주 @ {buy.avg_fill_price}  "
        f"stop={buy.stop_loss}  trail={buy.trail_stop}"
    )
    if not buy.protective_stop_id:
        print("  거래소: (무장된 조건주문 없음)")
        return price
    detail = broker.get_conditional_order(buy.protective_stop_id)
    first = detail.get("first") or {}
    print(
        f"  거래소: id={buy.protective_stop_id[:12]}…  status={detail.get('status')}  "
        f"trigger={first.get('triggerPrice')}  qty={detail.get('quantity')}"
    )
    trigger = first.get("triggerPrice")
    if price and trigger:
        gap = (price - float(trigger)) / price * 100
        print(f"  트리거까지: {gap:+.2f}%")
    return price


def cmd_check(_args) -> int:
    _, broker, queue, provider = build()
    queue.sync_with_broker(broker)
    buy = held_buy(queue, broker)
    show(broker, provider, buy)
    print(f"  auto 가동 중: {auto_is_running()}")
    saved = _load_state()
    if saved:
        print(f"  ⚠️  복구 대기: 원래 스톱 {saved['original_stop']} (restore 로 되돌릴 수 있음)")
    return 0


def _trigger_for(price: float, margin_pct: float) -> float:
    return round(price * (1 - margin_pct / 100), 2)


def _load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_plan(args) -> int:
    _, broker, queue, provider = build()
    buy = held_buy(queue, broker)
    price = show(broker, provider, buy)
    if buy is None:
        raise SystemExit("포지션이 없습니다")
    if price is None:
        raise SystemExit("현재가를 못 읽었습니다 — 트리거를 계산할 수 없습니다")

    trigger = _trigger_for(price, args.margin_pct)
    entry = buy.avg_fill_price or 0
    qty = buy.filled_quantity or 0
    print(f"\n  제안 트리거: {trigger}  (현재가 −{args.margin_pct}%)")
    print(f"  발동 시 예상: {qty}주 시장가 매도 ≈ ${trigger * qty:.2f}")
    print(f"  진입가 대비 : ${(trigger - entry) * qty:+.2f}")
    print(f"  되돌릴 스톱 : {buy.stop_loss}")
    print("\n  arm 하면 이 트리거가 거래소에 실제로 걸립니다. 가격이 닿으면 팔립니다.")
    print("  실행: python3 testcases/toss_stop_fire_test.py arm --confirm")
    return 0


def cmd_arm(args) -> int:
    settings, broker, queue, provider = build()
    require_preconditions(settings)
    if not args.confirm:
        raise SystemExit("--confirm 이 필요합니다 (카나리아 1주가 실제로 매도됩니다)")

    buy = held_buy(queue, broker)
    price = show(broker, provider, buy)
    if buy is None or price is None:
        raise SystemExit("포지션 또는 현재가를 읽지 못했습니다")

    trigger = _trigger_for(price, args.margin_pct)
    original = buy.stop_loss
    # Record before mutating: restore must not depend on the operator
    # remembering a number from a previous terminal session.
    STATE_PATH.write_text(
        json.dumps({"original_stop": original, "trigger": trigger, "ticker": TICKER}),
        encoding="utf-8",
    )
    print(f"\n  스톱 이동: {original} → {trigger}")
    queue.update(replace(buy, stop_loss=trigger))
    manage_once(queue, broker, provider)

    buy = held_buy(queue, broker)
    show(broker, provider, buy)
    if buy is not None and buy.protective_stop_id:
        print("\n✅ 무장 완료. 이제 `watch` 로 발동을 지켜보세요.")
        return 0
    print("\n❌ 무장 실패 — restore 로 되돌리고 로그를 확인하세요.")
    return 1


def cmd_watch(args) -> int:
    _, broker, queue, provider = build()
    deadline = time.time() + args.timeout
    print(f"  최대 {args.timeout}초 동안 {args.poll}초 간격으로 확인합니다. Ctrl+C 로 중단.\n")

    while time.time() < deadline:
        queue.sync_with_broker(broker)
        buy = held_buy(queue, broker)
        price = live_price(provider)
        if buy is None:
            print("\n  포지션이 사라졌습니다 — 발동한 것으로 보입니다.")
            return _verify_after_fire(broker, queue, provider)

        status = (
            broker.protective_stop_status(buy.protective_stop_id)
            if buy.protective_stop_id else None
        )
        print(
            f"  [{time.strftime('%H:%M:%S')}] 가격={price} "
            f"스톱={buy.stop_loss} 조건주문={status}"
        )
        # A conditional order leaves WATCHING the moment it fires; the
        # position takes a little longer to disappear from the venue.
        if status and status.upper() not in {"WATCHING", "PENDING", "OPEN"}:
            print(f"\n  조건주문 상태 전이: {status} — 체결 확인으로 넘어갑니다.")
            time.sleep(args.poll)
            return _verify_after_fire(broker, queue, provider)
        time.sleep(args.poll)

    print("\n⏱️  시간 내에 발동하지 않았습니다.")
    print("   가격이 트리거까지 안 내려온 것뿐이라면 다시 `watch` 하거나,")
    print("   오늘은 여기까지라면 `restore` 로 원래 스톱을 되돌리세요.")
    return 1


def _verify_after_fire(broker, queue, provider) -> int:
    """The half that matters as much as the sell: does the bot agree?"""
    from alpha_bot.auto.protective_stops import warn_unreferenced_stops

    print("\n  ── 봇 쪽 정합성 확인 ──")
    queue.sync_with_broker(broker)
    manage_once(queue, broker, provider)

    buy = held_buy(queue, broker)
    positions = [p for p in broker.get_positions(MARKET) if p.ticker == TICKER]
    orphans = warn_unreferenced_stops(queue, broker, lambda m: print(f"    {m}"))

    print(f"  큐가 보유로 아는 행: {'없음 ✅' if buy is None else f'남아 있음 ❌ ({buy.id})'}")
    print(f"  거래소 보유       : {'없음 ✅' if not positions else f'남아 있음 ❌ ({positions})'}")
    print(f"  미추적 조건주문   : {'없음 ✅' if not orphans else f'{len(orphans)}건 ❌'}")

    ok = buy is None and not positions and not orphans
    if ok:
        STATE_PATH.unlink(missing_ok=True)
        print("\n✅ 거래소가 손절을 집행했고, 봇도 청산을 인지했습니다.")
        print("   마지막 미검증 경로가 닫혔습니다.")
        return 0
    print("\n❌ 한쪽이 어긋납니다 — 위 항목을 대조하세요.")
    return 1


def cmd_restore(args) -> int:
    _, broker, queue, provider = build()
    saved = _load_state()
    original = args.stop if args.stop is not None else (saved or {}).get("original_stop")
    if original is None:
        raise SystemExit("되돌릴 스톱 값을 모릅니다 — --stop 으로 지정하세요")

    buy = held_buy(queue, broker)
    if buy is None:
        print("포지션이 없습니다 — 이미 청산되었다면 되돌릴 것이 없습니다.")
        STATE_PATH.unlink(missing_ok=True)
        return 0

    print(f"  스톱 복구: {buy.stop_loss} → {original}")
    queue.update(replace(buy, stop_loss=float(original)))
    manage_once(queue, broker, provider)
    show(broker, provider, held_buy(queue, broker))
    STATE_PATH.unlink(missing_ok=True)
    print("\n✅ 원래 손절선으로 복구했습니다.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    for name in ("check", "plan", "arm", "watch", "restore"):
        p = sub.add_parser(name)
        if name in ("plan", "arm"):
            p.add_argument("--margin-pct", type=float, default=DEFAULT_MARGIN_PCT)
        if name == "arm":
            p.add_argument("--confirm", action="store_true")
        if name == "watch":
            p.add_argument("--timeout", type=int, default=WATCH_TIMEOUT_SECONDS)
            p.add_argument("--poll", type=int, default=WATCH_POLL_SECONDS)
        if name == "restore":
            p.add_argument("--stop", type=float, default=None)
    args = parser.parse_args()
    return {
        "check": cmd_check, "plan": cmd_plan, "arm": cmd_arm,
        "watch": cmd_watch, "restore": cmd_restore,
    }[args.phase](args)


def _explain_venue_error(exc: Exception) -> int:
    """Turn a venue rejection into something actionable at the keyboard.

    This runs while the market is open and the operator is watching a
    clock; a stack trace is the wrong thing to hand them.
    """
    text = str(exc)
    print(f"\n❌ 거래소 호출 실패: {text}")
    if "ip-not-allowed" in text:
        print("\n   토스 API가 이 IP를 허용하지 않습니다. 공유기 재부팅·VPN·다른 네트워크로")
        print("   공인 IP가 바뀌면 발생합니다. 토스 개발자센터 > 애플리케이션 > 허용 IP 에")
        print("   현재 IP를 등록한 뒤 다시 실행하세요.")
        print("   현재 IP 확인:  curl -s https://api.ipify.org")
    elif "401" in text or "unauthorized" in text.lower():
        print("\n   자격증명 문제입니다 — 설정 화면의 TOSS_CLIENT_ID/SECRET 을 확인하세요.")
    return 2


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n중단했습니다. 무장 상태는 그대로입니다 — check 로 확인하세요.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — the operator needs a sentence, not a trace
        from alpha_bot.errors import BrokerError

        if isinstance(exc, (BrokerError,)) or exc.__class__.__name__.startswith("Toss"):
            sys.exit(_explain_venue_error(exc))
        raise
