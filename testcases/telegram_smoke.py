"""Verify the alert channel actually delivers, before trusting it to.

notify() is a deliberate silent no-op when Telegram is unconfigured, and
it swallows delivery failures too — observability must never break
trading. The cost of that rule is that a channel which is merely *wrong*
(typo'd chat id, revoked token, bot never started by the user) looks
exactly like one that is working. The only way to know is to send
something and watch it arrive.

Run this once after filling TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the
settings page, and again any time they change.

  python3 testcases/telegram_smoke.py check   # read-only, sends nothing
  python3 testcases/telegram_smoke.py send    # sends one test message

Exit codes: 0 pass · 1 fail · 2 not configured.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime


def mask(value: str) -> str:
    return (value[:4] + "***") if len(value) > 4 else "***"


def read_env() -> tuple[str, str]:
    from alpha_bot.config import load_dotenv
    import os

    load_dotenv()
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def cmd_check(_args) -> int:
    from alpha_bot.notify import is_configured

    token, chat_id = read_env()
    print(f"  TELEGRAM_BOT_TOKEN  {mask(token) if token else '(비어 있음)'}")
    print(f"  TELEGRAM_CHAT_ID    {chat_id or '(비어 있음)'}")

    if not token or not chat_id:
        # Both halves are required and the module will not say which is
        # missing at runtime — it just returns False forever.
        missing = [
            name for name, value in
            (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not value
        ]
        print(f"\n⚠️  미설정: {', '.join(missing)}")
        print("   설정 화면 > 알림 · Telegram 에서 채운 뒤 다시 실행하세요.")
        print("   봇은 계속 동작하지만 어떤 알림도 나가지 않습니다.")
        return 2

    print(f"\n✅ 두 값 모두 설정됨 (is_configured={is_configured()})")
    print("   실제 전달 여부는 `send` 로 확인하세요 — 값이 있다는 것과")
    print("   메시지가 도착한다는 것은 다른 이야기입니다.")
    return 0


def cmd_send(_args) -> int:
    from alpha_bot.notify import notify

    token, chat_id = read_env()
    if not token or not chat_id:
        print("⚠️  미설정 상태에서는 보낼 수 없습니다 — 먼저 `check`.")
        return 2

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  → {chat_id} 로 전송 시도…")
    delivered = notify(
        f"🔔 AlphaBot 알림 경로 점검\n{stamp}\n"
        "이 메시지가 보이면 손실 브레이커·킬스위치·청산 알림도 도착합니다.",
        # A fresh key each run: the 30-minute de-duplication exists to stop
        # a 5-minute loop from spamming, and would otherwise silently
        # swallow a re-test.
        dedupe_key=f"smoke:{stamp}",
    )
    if delivered:
        print("\n✅ 전달 확인 (Telegram API가 200으로 응답)")
        print("   휴대폰에 실제로 떴는지 눈으로 확인하세요.")
        return 0

    print("\n❌ 전달 실패 — notify()는 예외를 삼키므로 원인은 아래 중 하나입니다:")
    print("   · 토큰 오타/폐기        @BotFather 에서 재발급")
    print("   · chat id 오타          봇에게 메시지 후 getUpdates 로 재확인")
    print("   · 봇과 대화 시작 안 함  텔레그램에서 봇에게 아무 메시지나 먼저 보내야 합니다")
    print("   · 네트워크 차단")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["check", "send"])
    args = parser.parse_args()
    return {"check": cmd_check, "send": cmd_send}[args.phase](args)


if __name__ == "__main__":
    sys.exit(main())
