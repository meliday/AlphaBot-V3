"""Order management web API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alpha_bot.approval import ApprovalQueue
from alpha_bot.auto import make_broker
from alpha_bot.config import load_config

CONFIG_PATH = Path("config.yaml")


def handle_orders(serialise: Any) -> list[dict[str, Any]]:
    config = load_config(CONFIG_PATH)
    orders = ApprovalQueue(config.approval_queue).list_orders()
    return [serialise(o) for o in orders]


def handle_approve(body: dict[str, str], serialise: Any) -> dict[str, Any]:
    order_id = body.get("order_id", "")
    broker_name = body.get("broker", "mock")
    config = load_config(CONFIG_PATH)
    broker = make_broker(broker_name)
    updated, result = ApprovalQueue(config.approval_queue).approve(order_id, broker)
    return {
        "order": serialise(updated),
        "result": serialise(result),
    }


def handle_sync_orders(body: dict[str, Any], serialise: Any) -> dict[str, Any] | tuple[str, int]:
    broker_name = str(body.get("broker", "toss"))
    broker = make_broker(broker_name)
    config = load_config(CONFIG_PATH)
    queue = ApprovalQueue(config.approval_queue)
    try:
        recovered = queue.recover_unresolved_orders(broker)
        changed = queue.sync_with_broker(broker)
    except Exception as exc:
        return (f"동기화 실패: {exc}", 500)
    return {
        "broker": broker_name,
        "recovered": [serialise(o) for o in recovered],
        "changed": [serialise(o) for o in changed],
        "count": len(recovered) + len(changed),
    }
