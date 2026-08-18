"""Encapsulated auto-pilot state and loop for the web dashboard.

Replaces the module-level global variables with a thread-safe class
instance. The auto-pilot background loop runs in a daemon thread and
reports progress via a bounded log buffer.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from alpha_bot.auto import AutoTradeOptions, run_auto_iteration
from alpha_bot.auto.watchdog import write_heartbeat
from alpha_bot.config import load_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")


class AutoPilotState:
    """Thread-safe container for auto-pilot runtime state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active = False
        self._logs: list[dict[str, str]] = []
        self._started_at: str | None = None
        self._interval: int = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def started_at(self) -> str | None:
        with self._lock:
            return self._started_at

    @property
    def interval(self) -> int:
        with self._lock:
            return self._interval

    @property
    def logs(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._logs)

    def log(self, msg: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._logs.append({"time": timestamp, "msg": msg})
            if len(self._logs) > 100:
                self._logs.pop(0)

    def start(
        self,
        watchlist: str,
        interval: int,
        broker: str,
        quantity: int,
        source: str,
        use_llm: bool = True,
        cooldown_hours: int = 24,
        cooldown_enabled: bool = True,
        auto_size: bool = False,
    ) -> bool:
        """Start the auto-pilot loop. Returns False if already running."""
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._interval = interval

        self._thread = threading.Thread(
            target=self._loop,
            args=(watchlist, interval, broker, quantity, source, use_llm,
                  cooldown_hours, cooldown_enabled, auto_size),
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._started_at = None
            self._interval = 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "logs": list(self._logs),
                "started_at": self._started_at,
                "interval": self._interval,
            }

    def _loop(
        self,
        watchlist: str,
        interval: int,
        broker_name: str,
        quantity: int,
        source: str,
        use_llm: bool,
        cooldown_hours: int,
        cooldown_enabled: bool,
        auto_size: bool,
    ) -> None:
        config = load_config(CONFIG_PATH)
        opts = AutoTradeOptions(
            watchlist=Path(watchlist),
            broker_name=broker_name,
            quantity=quantity,
            source=source,
            language="ko",
            cooldown_hours=cooldown_hours,
            cooldown_enabled=cooldown_enabled,
            use_llm=use_llm,
            auto_size=auto_size,
        )

        self.log(
            f"🚀 자동매매 봇 시작 (브로커: {broker_name}, 감시주기: {interval}초, "
            f"LLM: {use_llm}, 자동 사이징: {auto_size})"
        )
        write_heartbeat(
            "auto", status="starting", detail={"broker": broker_name, "source": "web"}
        )

        while self.active:
            try:
                write_heartbeat(
                    "auto", status="scanning",
                    detail={"broker": broker_name, "source": "web"},
                )
                self.log(f"🔄 [{watchlist}] 관심종목 스캔 시작...")
                run_auto_iteration(opts, config, log=self.log)

                if self.active:
                    write_heartbeat(
                        "auto", status="idle",
                        detail={"broker": broker_name, "source": "web"},
                    )
                    self.log(f"💤 스캔 완료. {interval}초 대기 중...")
                    for elapsed in range(interval):
                        if not self.active:
                            break
                        time.sleep(1)
                        if (elapsed + 1) % 30 == 0:
                            write_heartbeat(
                                "auto", status="idle",
                                detail={"broker": broker_name, "source": "web"},
                            )
            except Exception as exc:
                write_heartbeat(
                    "auto", status="failed",
                    detail={"broker": broker_name, "source": "web", "error": str(exc)[:200]},
                )
                self.log(f"🛑 루프 오류 발생: {exc}")
                time.sleep(5)

        write_heartbeat(
            "auto", status="stopped", detail={"broker": broker_name, "source": "web"}
        )
        self.log("🛑 자동매매 봇이 중지되었습니다.")


# Module-level singleton used by all web handler modules.
auto_pilot = AutoPilotState()
