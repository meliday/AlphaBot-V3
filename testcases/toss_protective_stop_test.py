"""Stage-3 live verification: the broker-side protective stop, on a real share.

Stage 2 left one deliberately-kept share (F, bought with a real risk frame:
stop −7%, targets far away). This runner exercises the protective-stop
machinery against it through the **production path** — ``sync_with_broker →
manage_open_positions(protective_stops=True)`` — not a side channel, while
``config.protective_stop`` stays false so nothing else arms until this
stage passes and the operator opts in for real.

Phases (each a separate, explicit invocation — that is the confirmation):

  check    read-only: queue row, venue conditional-order detail, orphan-sweep
           silence. Run before and after every mutating phase.
  arm      one management pass with protective stops on → a SINGLE+MARKET
           conditional sell (1 share @ the recorded stop) appears at Toss.
           Requires the US market to be tradable (the production gate).
  again    a second management pass → must be a steady-state no-op
           (same id, no venue writes).
  rearm    raises the recorded stop by ~2% and re-syncs → verifies the
           cancel + idempotent-create path live (new id, old id gone).
  release  cancels the venue stop and clears the row — the state to leave
           things in if you stop here.

What "pass" looks like end to end: arm → WATCHING at the venue → again is
silent → rearm swaps ids → release leaves nothing armed and the app's
conditional-order list empty.

The stop is a real standing order: if F drops through it while armed, the
venue sells the share at market. That is the feature working, and the
blast radius is one ~$14 share.

Run:
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py check
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py arm
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py check
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py again
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py rearm
  PYTHONPATH=src python3.12 testcases/toss_protective_stop_test.py release
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

TICKER = "F"
QUEUE_PATH = Path("pending_orders.json")


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
    return find_held_buy(queue, "US", TICKER, broker)


def show_row(buy) -> None:
    if buy is None:
        print("  (보유 중인 테스트 포지션 없음 — tiny_order fill 먼저)")
        return
    print(
        f"  {buy.id}  {buy.filled_quantity}주 @ {buy.avg_fill_price}  "
        f"stop={buy.stop_loss}  trail={buy.trail_stop}"
    )
    print(
        f"  조건주문: id={buy.protective_stop_id}  "
        f"armed_price={buy.protective_stop_price}  qty={buy.protective_stop_quantity}"
    )


def show_venue(broker, buy) -> None:
    if buy is None or not buy.protective_stop_id:
        print("  거래소: (무장된 조건주문 없음)")
        return
    detail = broker.get_conditional_order(buy.protective_stop_id)
    first = detail.get("first") or {}
    print(
        f"  거래소: status={detail.get('status')}  type={detail.get('type')}/"
        f"{detail.get('orderType')}  qty={detail.get('quantity')}  "
        f"trigger={first.get('triggerPrice')}  expire={detail.get('expireDate')}"
    )


def manage_once(queue, broker, provider) -> None:
    """The production management slice, nothing else — no scanning, no buys."""
    from alpha_bot.auto.position_manager import manage_open_positions

    queue.sync_with_broker(broker)
    manage_open_positions(
        queue, broker, provider, lambda m: print(m),
        protective_stops=True,
    )


def cmd_check(args) -> None:
    from alpha_bot.auto.protective_stops import warn_unreferenced_stops

    _, broker, queue, _ = build()
    queue.sync_with_broker(broker)
    buy = held_buy(queue, broker)
    show_row(buy)
    show_venue(broker, buy)
    unknown = warn_unreferenced_stops(queue, broker, print)
    if not unknown:
        print("  orphan 스윕: 미추적 조건주문 없음 ✅")


def cmd_arm(args) -> None:
    from alpha_bot.market_hours import market_status

    settings, broker, queue, provider = build()
    if not settings.enable_live_orders:
        raise SystemExit("TOSS_ENABLE_LIVE_ORDERS=true 필요")
    status = market_status("US")
    print(f"세션: {status.reason}")
    if not status.is_open:
        raise SystemExit(
            "US 세션이 아니면 manage 루프가 포지션 평가를 건너뜁니다 — "
            "정규장(22:30–05:00 KST)에 실행하세요."
        )
    buy = held_buy(queue, broker)
    if buy is None:
        raise SystemExit("보유 테스트 포지션 없음 — tiny_order fill 먼저")
    print(f"무장 예정: {TICKER} {buy.filled_quantity}주, 스톱 {buy.stop_loss} (시장가 SINGLE)")
    manage_once(queue, broker, provider)
    buy = held_buy(queue, broker)
    show_row(buy)
    show_venue(broker, buy)
    ok = buy is not None and buy.protective_stop_id
    print("✅ 무장 완료 — 토스 앱 > 주문내역 > 조건주문에서도 보여야 합니다" if ok
          else "❌ 무장 실패 — 위 로그 확인")


def cmd_again(args) -> None:
    _, broker, queue, provider = build()
    before = held_buy(queue, broker)
    if before is None or not before.protective_stop_id:
        raise SystemExit("무장 상태가 아닙니다 — arm 먼저")
    manage_once(queue, broker, provider)
    after = held_buy(queue, broker)
    show_row(after)
    same = after is not None and after.protective_stop_id == before.protective_stop_id
    print("✅ 정상 상태 no-op (id 불변)" if same else "❌ id가 바뀜 — 불필요한 재무장 발생")


def cmd_rearm(args) -> None:
    _, broker, queue, provider = build()
    buy = held_buy(queue, broker)
    if buy is None or not buy.protective_stop_id:
        raise SystemExit("무장 상태가 아닙니다 — arm 먼저")
    old_id = buy.protective_stop_id
    new_stop = round((buy.stop_loss or 0) * 1.02, 2)
    print(f"스톱 상향: {buy.stop_loss} → {new_stop} (취소+멱등 재생성 경로 검증)")
    queue.update(replace(buy, stop_loss=new_stop))
    manage_once(queue, broker, provider)
    buy = held_buy(queue, broker)
    show_row(buy)
    show_venue(broker, buy)
    old_gone = broker.protective_stop_status(old_id) is None
    swapped = buy is not None and buy.protective_stop_id not in (None, old_id)
    print(f"  이전 id 소멸: {old_gone}")
    print("✅ 재무장 경로 정상 (cancel + create, 단일 스톱 유지)" if (old_gone and swapped)
          else "❌ 재무장 이상 — check 로 거래소 상태 대조")


def cmd_release(args) -> None:
    from alpha_bot.auto.protective_stops import release_protective_stop

    _, broker, queue, _ = build()
    buy = held_buy(queue, broker)
    if buy is None:
        raise SystemExit("보유 테스트 포지션 없음")
    if not buy.protective_stop_id:
        print("이미 해제 상태입니다.")
        show_row(buy)
        return
    old_id = buy.protective_stop_id
    buy, ok = release_protective_stop(queue, broker, buy, print)
    show_row(buy)
    print(f"  이전 id 소멸: {broker.protective_stop_status(old_id) is None}")
    print("✅ 해제 완료 — 포지션은 폴링 손절로만 보호되는 원래 상태" if ok else "❌ 해제 실패")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["check", "arm", "again", "rearm", "release"])
    args = parser.parse_args()
    {
        "check": cmd_check,
        "arm": cmd_arm,
        "again": cmd_again,
        "rearm": cmd_rearm,
        "release": cmd_release,
    }[args.phase](args)
    return 0


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
