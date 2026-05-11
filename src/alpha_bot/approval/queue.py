from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from alpha_bot.broker.base import Broker
from alpha_bot.errors import ApprovalError
from alpha_bot.models import OrderCandidate, OrderRequest, OrderResult, Signal, utc_now_iso

logger = logging.getLogger(__name__)

# Per-path locks so two ApprovalQueue instances pointing at the same file
# (web auto-pilot thread + a CLI command, for example) serialize their
# reads/writes through the same mutex. Cross-process protection still
# relies on the atomic rename below, but in practice this app is single-
# process and the lock removes the main race window.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()) if path.exists() else str(path)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class ApprovalQueue:
    def __init__(self, path: Path = Path("pending_orders.json")):
        self.path = path
        self._lock = _lock_for(path)

    def enqueue(
        self,
        request: OrderRequest,
        *,
        stop_loss: float | None = None,
        target1: float | None = None,
        target2: float | None = None,
        analysis_signal: Signal | None = None,
    ) -> OrderCandidate:
        if request.quantity <= 0:
            raise ApprovalError("Cannot enqueue an order with quantity <= 0.")
        if request.order_type == "limit" and request.limit_price is None:
            raise ApprovalError("Cannot enqueue a limit order without limit_price.")

        with self._lock:
            orders = self.list_orders()
            by_id = {o.id: o for o in orders}
            for order in orders:
                if order.request.ticker != request.ticker:
                    continue
                if order.request.market != request.market:
                    continue
                if order.request.side != request.side:
                    continue
                # Block duplicates for orders that are still active (pending or submitted).
                if order.status in {"pending", "submitted"}:
                    raise ApprovalError(
                        f"Active {order.status} order already exists for "
                        f"{request.market}:{request.ticker}: {order.id}"
                    )
                # For filled/partially_filled buys, block re-entry unless the position
                # has been fully exited.
                if order.status in {"filled", "partially_filled"} and request.side == "buy":
                    exit_o = by_id.get(order.exit_order_id or "")
                    if not (exit_o and exit_o.status == "filled"):
                        raise ApprovalError(
                            f"Open filled buy already exists for "
                            f"{request.market}:{request.ticker}: {order.id}"
                        )

            candidate = OrderCandidate(
                id=f"ORD-{uuid.uuid4().hex[:10].upper()}",
                request=request,
                status="pending",
                created_at=utc_now_iso(),
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                analysis_signal=analysis_signal,
            )
            orders.append(candidate)
            self._write(orders)
        logger.info(
            "Enqueued order %s: %s %s:%s qty=%d signal=%s",
            candidate.id, request.side, request.market, request.ticker,
            request.quantity, analysis_signal,
        )
        try:
            from alpha_bot.audit_log import log_queue as _log_q
            _log_q(
                order_id=candidate.id,
                ticker=request.ticker,
                market=request.market,
                side=request.side,
                quantity=request.quantity,
                limit_price=request.limit_price,
                signal=str(analysis_signal) if analysis_signal else None,
                stop_loss=stop_loss,
                target1=target1,
            )
        except Exception:
            pass
        return candidate

    def list_orders(self) -> list[OrderCandidate]:
        with self._lock:
            if not self.path.exists():
                return []
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        rows = raw.get("orders", raw if isinstance(raw, list) else [])
        return [OrderCandidate.from_mapping(row) for row in rows]

    def sync_with_broker(self, broker: Broker) -> list[OrderCandidate]:
        """Refresh fill state for orders that the broker is still working on.

        Polls each non-terminal order (``submitted`` or ``partially_filled``)
        via ``broker.get_order_fill`` and persists status, filled quantity,
        and average fill price. Returns the list of orders that actually
        changed state.
        """

        if not hasattr(broker, "get_order_fill"):
            return []

        with self._lock:
            orders = self.list_orders()
            changed: list[OrderCandidate] = []
            dirty = False
            for index, order in enumerate(orders):
                if order.status not in {"submitted", "partially_filled"}:
                    continue
                if not order.broker_order_id:
                    continue
                try:
                    fill = broker.get_order_fill(
                        order.broker_order_id, order.request.market, order.request.quantity
                    )
                except Exception as exc:
                    logger.warning("Sync failed for order %s: %s", order.id, exc)
                    continue
                new_status = fill.status
                updated = replace(
                    order,
                    status=new_status,
                    filled_quantity=fill.filled_quantity,
                    avg_fill_price=fill.avg_fill_price,
                    broker_message=fill.message or order.broker_message,
                    last_synced_at=utc_now_iso(),
                )
                if (
                    updated.status != order.status
                    or updated.filled_quantity != order.filled_quantity
                    or updated.avg_fill_price != order.avg_fill_price
                ):
                    logger.info(
                        "Order %s synced: %s → %s (filled %d/%d)",
                        order.id, order.status, updated.status,
                        fill.filled_quantity, order.request.quantity,
                    )
                    changed.append(updated)
                orders[index] = updated
                dirty = True
            if dirty:
                self._write(orders)
        return changed

    def update(self, order: OrderCandidate) -> None:
        """Persist a single in-memory ``OrderCandidate`` back to disk."""
        with self._lock:
            orders = self.list_orders()
            for index, existing in enumerate(orders):
                if existing.id == order.id:
                    orders[index] = order
                    self._write(orders)
                    return
        raise ApprovalError(f"Order not found: {order.id}")

    def mark_externally_closed(self, buy_id: str, broker_name: str = "external") -> OrderCandidate:
        """Record an externally-closed bot position by synthesising a filled sell.

        Used by reconciliation when the broker reports zero quantity for a
        position the queue still considers held — typically because the user
        sold manually through the broker UI. We create a synthetic ``filled``
        sell so downstream filters (``_find_held_buy``, ``manage_open_positions``,
        ``_count_open_positions``) immediately recognise the position as closed.
        """
        with self._lock:
            orders = self.list_orders()
            target = next((o for o in orders if o.id == buy_id), None)
            if target is None:
                raise ApprovalError(f"Buy not found: {buy_id}")
            qty = target.filled_quantity or target.request.quantity
            synthetic = OrderCandidate(
                id=f"EXT-{uuid.uuid4().hex[:10].upper()}",
                request=OrderRequest(
                    ticker=target.request.ticker,
                    market=target.request.market,
                    side="sell",
                    quantity=qty,
                    order_type="market",
                    limit_price=None,
                    reason="외부 매도 감지 — 봇 보유 기록 정리",
                ),
                status="filled",
                created_at=utc_now_iso(),
                submitted_at=utc_now_iso(),
                broker_order_id="EXTERNAL",
                broker_message="External close detected during reconciliation",
                broker=broker_name,
                filled_quantity=qty,
                avg_fill_price=None,
                last_synced_at=utc_now_iso(),
            )
            for index, existing in enumerate(orders):
                if existing.id == buy_id:
                    orders[index] = replace(
                        existing,
                        exit_order_id=synthetic.id,
                        exit_reason="external_close",
                    )
                    break
            orders.append(synthetic)
            self._write(orders)
        logger.info(
            "Marked %s as externally closed (synthetic sell %s, qty=%d)",
            buy_id, synthetic.id, qty,
        )
        return synthetic

    def approve(self, order_id: str, broker: Broker) -> tuple[OrderCandidate, OrderResult]:
        with self._lock:
            orders = self.list_orders()
            target_index = None
            target_order = None
            for index, order in enumerate(orders):
                if order.id == order_id:
                    target_index = index
                    target_order = order
                    break
            if target_order is None:
                raise ApprovalError(f"Order not found: {order_id}")
            if target_order.status != "pending":
                raise ApprovalError(
                    f"Order {order_id} is not pending; status={target_order.status}."
                )

        # Broker call happens outside the lock so we don't block other
        # queue readers while waiting on the network.
        try:
            result = broker.place_order(target_order.request)
        except Exception as exc:
            with self._lock:
                orders = self.list_orders()
                for index, order in enumerate(orders):
                    if order.id == order_id:
                        orders[index] = replace(
                            order,
                            status="rejected",
                            submitted_at=utc_now_iso(),
                            broker_message=str(exc),
                        )
                        self._write(orders)
                        break
            logger.error("Order %s rejected: %s", order_id, exc)
            raise

        with self._lock:
            orders = self.list_orders()
            updated: OrderCandidate | None = None
            for index, order in enumerate(orders):
                if order.id == order_id:
                    updated = replace(
                        order,
                        status="submitted" if result.accepted else "rejected",
                        submitted_at=utc_now_iso(),
                        broker_order_id=result.broker_order_id,
                        broker_message=result.message,
                        broker=broker.name,
                    )
                    orders[index] = updated
                    self._write(orders)
                    break
        if updated is None:
            raise ApprovalError(f"Order not found after broker call: {order_id}")
        logger.info(
            "Order %s %s via %s: %s",
            order_id, updated.status, broker.name, result.message,
        )
        try:
            from alpha_bot.audit_log import log_trade as _log_t
            _log_t(
                order_id=order_id,
                ticker=updated.request.ticker,
                market=updated.request.market,
                side=updated.request.side,
                status=updated.status,
                quantity=updated.request.quantity,
                broker_order_id=result.broker_order_id,
                broker_message=result.message,
            )
        except Exception:
            pass
        return updated, result

    def _write(self, orders: list[OrderCandidate]) -> None:
        """Atomic write: serialize to ``<path>.tmp`` then rename. Combined
        with ``self._lock`` this gives readers a consistent snapshot."""
        payload = {"orders": [order.to_dict() for order in orders]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
