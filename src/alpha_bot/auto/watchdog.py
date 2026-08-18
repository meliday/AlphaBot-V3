"""Process heartbeat files and a small out-of-process liveness watchdog."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_bot.broker.base import Broker, broker_scope

logger = logging.getLogger(__name__)


def heartbeat_dir() -> Path:
    return Path(os.environ.get("BOT_HEARTBEAT_DIR", "runtime/heartbeats"))


def heartbeat_path(component: str, directory: Path | None = None) -> Path:
    if component not in {"auto", "monitor"}:
        raise ValueError(f"Unsupported heartbeat component: {component}")
    return (directory or heartbeat_dir()) / f"{component}.json"


def write_heartbeat(
    component: str,
    *,
    broker: Broker | None = None,
    status: str = "running",
    detail: dict[str, Any] | None = None,
    directory: Path | None = None,
) -> bool:
    """Atomically publish a heartbeat. Failures are logged, never raised."""

    path = heartbeat_path(component, directory)
    now = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "version": 1,
        "component": component,
        "pid": os.getpid(),
        "status": status,
        "updated_at": now.replace(microsecond=0).isoformat(),
        "updated_epoch": now.timestamp(),
    }
    if broker is not None:
        scope = broker_scope(broker)
        record["broker"] = scope.name
        record["broker_instance_id"] = scope.instance_id
        record["broker_account_id"] = scope.account_id
        record["broker_mode"] = scope.mode
    if detail:
        record["detail"] = detail

    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("Heartbeat write failed for %s: %s", component, exc)
        return False


@dataclass(frozen=True)
class HeartbeatHealth:
    component: str
    healthy: bool
    reason: str
    age_seconds: float | None = None
    record: dict[str, Any] | None = None


def check_heartbeat(
    component: str,
    max_age_seconds: float,
    *,
    directory: Path | None = None,
    now_epoch: float | None = None,
) -> HeartbeatHealth:
    """Read and validate one heartbeat without trusting file mtime."""

    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    path = heartbeat_path(component, directory)
    if not path.exists():
        return HeartbeatHealth(component, False, f"heartbeat missing: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("component") != component:
            raise ValueError("component mismatch")
        updated = float(record["updated_epoch"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return HeartbeatHealth(component, False, f"heartbeat invalid: {exc}")

    now = time.time() if now_epoch is None else now_epoch
    age = now - updated
    status = str(record.get("status", "running"))
    if status in {"stopped", "failed"}:
        return HeartbeatHealth(
            component, False, f"process reported status={status}", max(age, 0.0), record
        )
    if age < -30:
        return HeartbeatHealth(
            component, False, f"heartbeat timestamp is {abs(age):.0f}s in the future", age, record
        )
    if age > max_age_seconds:
        return HeartbeatHealth(
            component, False,
            f"heartbeat stale: {age:.0f}s > {max_age_seconds:.0f}s",
            age, record,
        )
    return HeartbeatHealth(component, True, "ok", max(age, 0.0), record)


def sleep_with_heartbeat(
    seconds: float,
    component: str,
    *,
    broker: Broker | None = None,
    heartbeat_interval: float = 30.0,
) -> None:
    """Sleep while keeping the process heartbeat fresh."""

    deadline = time.monotonic() + max(seconds, 0.0)
    interval = max(1.0, heartbeat_interval)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval, remaining))
        write_heartbeat(component, broker=broker, status="idle")
