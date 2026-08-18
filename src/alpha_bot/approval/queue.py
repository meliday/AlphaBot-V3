from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_bot.broker.base import Broker, BrokerScope, broker_scope
from alpha_bot.errors import ApprovalError, BrokerOrderRejected
from alpha_bot.models import (
    OrderCandidate,
    OrderFill,
    OrderRequest,
    OrderResult,
    Signal,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

# Per-path mutexes protect threads. A companion ``flock`` below protects
# independent CLI/web/monitor processes during read-modify-write transactions.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


_BROKER_ACTIVE_STATUSES = {
    "submitting", "unknown", "submitted", "partially_filled", "filled",
}


def order_belongs_to_broker(order: OrderCandidate, broker: Broker) -> bool:
    """Whether an order is bound to this exact broker account instance.

    Legacy rows without ``broker_instance_id`` intentionally do not match.
    Guessing their account would recreate the cross-account liquidation bug
    this check is meant to prevent.
    """

    scope = broker_scope(broker)
    if not order.broker_instance_id:
        return False
    if order.broker != scope.name or order.broker_instance_id != scope.instance_id:
        return False
    if order.broker_account_id and order.broker_account_id != scope.account_id:
        return False
    if order.broker_mode and order.broker_mode != scope.mode:
        return False
    return True


def _scope_fields(scope: BrokerScope) -> dict[str, str]:
    return {
        "broker": scope.name,
        "broker_instance_id": scope.instance_id,
        "broker_account_id": scope.account_id,
        "broker_mode": scope.mode,
    }


def _scopes_overlap(order: OrderCandidate, scope: BrokerScope | None) -> bool:
    """Unbound intents overlap every account; bound rows only their account."""

    if scope is None or not order.broker_instance_id:
        return True
    return order.broker_instance_id == scope.instance_id


def _confirmed_remaining_quantity(
    buy: OrderCandidate, by_id: dict[str, OrderCandidate]
) -> int:
    """Net confirmed linked sell fills from a buy's confirmed fill quantity."""

    remaining = buy.filled_quantity or 0
    seen: set[str] = set()
    sell_ids = [*buy.partial_exit_ids]
    if buy.exit_order_id:
        sell_ids.append(buy.exit_order_id)
    for sell_id in sell_ids:
        if sell_id in seen:
            continue
        seen.add(sell_id)
        sell = by_id.get(sell_id)
        if sell is None:
            continue
        filled = sell.filled_quantity or 0
        if sell.status == "filled" and filled <= 0:
            # Backward compatibility for legacy filled rows that predate the
            # explicit filled_quantity field.
            filled = sell.request.quantity
        remaining -= filled
    return max(remaining, 0)


def _linked_sell_is_working(
    buy: OrderCandidate, by_id: dict[str, OrderCandidate]
) -> bool:
    sell_ids = [*buy.partial_exit_ids]
    if buy.exit_order_id:
        sell_ids.append(buy.exit_order_id)
    return any(
        (sell := by_id.get(sell_id)) is not None
        and sell.status in {"pending", "submitting", "unknown", "submitted", "partially_filled"}
        for sell_id in sell_ids
    )


def _merge_fill(order: OrderCandidate, fill: OrderFill) -> OrderCandidate:
    """Merge a broker observation without losing previously confirmed fills."""

    reported_qty = max(0, min(int(fill.filled_quantity), order.request.quantity))
    confirmed_qty = max(order.filled_quantity, reported_qty)
    if reported_qty < order.filled_quantity:
        logger.warning(
            "Ignoring regressive fill quantity for %s: broker=%d local=%d",
            order.id, reported_qty, order.filled_quantity,
        )

    if order.status == "filled" or fill.status == "filled":
        status = "filled"
    elif fill.status in {"cancelled", "rejected", "partially_filled_cancelled"}:
        status = fill.status
    elif confirmed_qty > 0:
        status = "partially_filled"
    elif order.status in {"submitting", "unknown", "submitted"} and fill.status == "pending":
        status = "submitted"
    else:
        status = fill.status

    avg_fill_price = order.avg_fill_price
    if fill.avg_fill_price is not None and reported_qty >= order.filled_quantity:
        avg_fill_price = fill.avg_fill_price

    return replace(
        order,
        status=status,
        filled_quantity=confirmed_qty,
        avg_fill_price=avg_fill_price,
        broker_message=fill.message or order.broker_message,
        last_synced_at=utc_now_iso(),
    )


class ApprovalQueue:
    def __init__(self, path: Path = Path("pending_orders.json")):
        self.path = path
        self._lock = _lock_for(path)

    def enqueue(
        self,
        request: OrderRequest,
        *,
        broker: Broker | None = None,
        stop_loss: float | None = None,
        target1: float | None = None,
        target2: float | None = None,
        analysis_signal: Signal | None = None,
    ) -> OrderCandidate:
        if request.quantity <= 0:
            raise ApprovalError("Cannot enqueue an order with quantity <= 0.")
        if broker is not None and hasattr(broker, "normalize_order"):
            request = broker.normalize_order(request)  # type: ignore[attr-defined]
        if request.order_type == "limit" and request.limit_price is None:
            raise ApprovalError("Cannot enqueue a limit order without limit_price.")

        scope = broker_scope(broker) if broker is not None else None
        with self._orders_transaction() as orders:
            by_id = {o.id: o for o in orders}
            for order in orders:
                if order.request.ticker != request.ticker:
                    continue
                if order.request.market != request.market:
                    continue
                if order.request.side != request.side:
                    continue
                if not _scopes_overlap(order, scope):
                    continue
                # Block duplicates for orders that are still active.
                if order.status in {
                    "pending", "submitting", "unknown", "submitted", "partially_filled",
                }:
                    raise ApprovalError(
                        f"Active {order.status} order already exists for "
                        f"{request.market}:{request.ticker}: {order.id}"
                    )
                # For filled/partially_filled buys, block re-entry unless the position
                # has been fully exited.
                if order.status in {
                    "filled", "partially_filled", "partially_filled_cancelled",
                } and request.side == "buy":
                    if (
                        _confirmed_remaining_quantity(order, by_id) > 0
                        or _linked_sell_is_working(order, by_id)
                    ):
                        raise ApprovalError(
                            f"Open filled buy already exists for "
                            f"{request.market}:{request.ticker}: {order.id}"
                        )

            candidate_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
            bound_request = request
            if not request.client_order_id:
                bound_request = replace(request, client_order_id=candidate_id)
            candidate = OrderCandidate(
                id=candidate_id,
                request=bound_request,
                status="pending",
                created_at=utc_now_iso(),
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                analysis_signal=analysis_signal,
                **(_scope_fields(scope) if scope else {}),
            )
            orders.append(candidate)
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
        with self._lock, self._file_lock(exclusive=False):
            return self._read_unlocked()

    def sync_with_broker(self, broker: Broker) -> list[OrderCandidate]:
        """Refresh fill state for orders that the broker is still working on.

        Polls each non-terminal order (``submitted`` or ``partially_filled``)
        via ``broker.get_order_fill`` and persists status, filled quantity,
        and average fill price. Returns the list of orders that actually
        changed state.
        """

        if not hasattr(broker, "get_order_fill"):
            return []

        with self._orders_transaction() as orders:
            changed: list[OrderCandidate] = []
            for index, order in enumerate(orders):
                if order.status not in {"submitted", "partially_filled"}:
                    continue
                if not order_belongs_to_broker(order, broker):
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
                updated = _merge_fill(order, fill)
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
        return changed

    def recover_unresolved_orders(
        self, broker: Broker, *, max_replay_age_minutes: int = 9
    ) -> list[OrderCandidate]:
        """Safely replay recent ambiguous submissions through broker idempotency.

        Toss keeps ``clientOrderId`` idempotency for ten minutes. We use a
        conservative nine-minute window so a delayed request never crosses the
        expiry boundary and creates a duplicate order. Older rows remain
        ``unknown`` and continue to block new trading until explicitly
        reconciled.
        """

        if not hasattr(broker, "recover_order") or max_replay_age_minutes <= 0:
            return []
        now = datetime.now(timezone.utc)
        recovered: list[OrderCandidate] = []
        for snapshot in self.unresolved_orders(broker):
            stamp = snapshot.submitted_at or snapshot.created_at
            try:
                submitted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                logger.warning("Cannot parse unresolved order timestamp: %s", snapshot.id)
                continue
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            age = now - submitted
            if age < timedelta(0) or age >= timedelta(minutes=max_replay_age_minutes):
                continue
            if not snapshot.request.client_order_id:
                continue
            rejection: BrokerOrderRejected | None = None
            try:
                result = broker.recover_order(snapshot.request)  # type: ignore[attr-defined]
            except BrokerOrderRejected as exc:
                rejection = exc
                result = OrderResult(broker.name, False, "", str(exc))
            except Exception as exc:
                logger.warning("Recovery failed for order %s: %s", snapshot.id, exc)
                continue

            with self._orders_transaction() as orders:
                updated: OrderCandidate | None = None
                for index, current in enumerate(orders):
                    if current.id != snapshot.id:
                        continue
                    if current.status not in {"submitting", "unknown"}:
                        break
                    if not order_belongs_to_broker(current, broker):
                        break
                    updated = replace(
                        current,
                        status="submitted" if result.accepted else "rejected",
                        broker_order_id=result.broker_order_id or current.broker_order_id,
                        broker_message=(
                            "Recovered through broker idempotency: " + result.message
                        ),
                        rejection_code=(
                            None if result.accepted else rejection.code if rejection else None
                        ),
                        rejection_retryable=(
                            None
                            if result.accepted
                            else rejection.retryable if rejection else False
                        ),
                        last_synced_at=utc_now_iso(),
                    )
                    orders[index] = updated
                    break
            if updated is not None:
                recovered.append(updated)
        return recovered

    def cancel_stale_orders(
        self, broker: Broker, max_age_minutes: int
    ) -> list[OrderCandidate]:
        """Cancel limit orders that have sat unfilled for too long.

        A limit buy placed at close×1.01 that gaps away can linger all
        session and then fill at a now-stale price when the market dips back
        — the setup that justified the entry no longer exists by then. We
        cancel any ``submitted`` order with zero fills older than
        ``max_age_minutes`` so the next iteration re-evaluates from scratch.

        Partially-filled buys have only their unfilled remainder cancelled;
        confirmed fills remain held as ``partially_filled_cancelled``. Sell
        orders are intentionally excluded so a protective/scale-out exit is
        never cancelled by entry freshness policy.

        Returns the orders that were successfully cancelled.
        """
        if max_age_minutes <= 0 or not hasattr(broker, "cancel_order"):
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        cancelled: list[OrderCandidate] = []
        orders = self.list_orders()
        for order in orders:
            if order.status not in {"submitted", "partially_filled"}:
                continue
            if not order_belongs_to_broker(order, broker):
                continue
            if order.request.side != "buy":
                continue
            if order.request.order_type != "limit":
                continue
            if not order.broker_order_id:
                continue
            stamp = order.submitted_at or order.created_at
            try:
                submitted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            if submitted > cutoff:
                continue
            try:
                result = broker.cancel_order(
                    order.broker_order_id,
                    order.request.market,
                    order.request.ticker,
                    order.request.quantity,
                )
            except Exception as exc:
                logger.warning("Stale-cancel failed for %s: %s", order.id, exc)
                continue
            if not result.accepted:
                logger.warning(
                    "Stale-cancel rejected for %s: %s", order.id, result.message
                )
                continue
            updated = replace(
                order,
                status=(
                    "partially_filled_cancelled"
                    if order.filled_quantity > 0
                    else "cancelled"
                ),
                broker_message=f"스테일 주문 자동 취소 ({max_age_minutes}분 초과): {result.message}",
                last_synced_at=utc_now_iso(),
            )
            try:
                self.update(updated)
                cancelled.append(updated)
                logger.info(
                    "Cancelled stale order %s (%s:%s, submitted %s)",
                    order.id, order.request.market, order.request.ticker, stamp,
                )
            except Exception as exc:
                logger.warning("Stale-cancel bookkeeping failed for %s: %s", order.id, exc)
        return cancelled

    def archive_closed_orders(
        self,
        *,
        archive_dir: Path = Path("logs/orders_archive"),
        min_age_days: int = 7,
    ) -> int:
        """Move long-settled order groups out of the live queue file.

        The queue file is re-read and fully rewritten on every ``update()``,
        so it must stay small — but rows are also the audit trail linking
        buys to their exits, so nothing may leave while any part of its
        story is still open. A *group* (one buy plus every sell it
        references) is archived only when every member is terminal, the
        buy's confirmed shares are fully accounted for, no protective-stop
        state is armed or pending, and the group's last activity is older
        than ``min_age_days``. Unreferenced sells and anything unprovable
        (missing timestamps) stay put.

        Groups land in ``archive_dir/YYYY-MM.json`` (dedup by id, so a crash
        between the archive write and the queue rewrite re-archives rather
        than loses). Returns the number of rows moved.
        """

        cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
        terminal_sell = {"filled", "cancelled", "rejected", "partially_filled_cancelled"}

        def last_activity(order: OrderCandidate) -> datetime | None:
            for stamp in (order.last_synced_at, order.submitted_at, order.created_at):
                if not stamp:
                    continue
                try:
                    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return None

        moved: list[OrderCandidate] = []
        with self._orders_transaction() as orders:
            by_id = {o.id: o for o in orders}
            archive_ids: set[str] = set()
            for buy in orders:
                if buy.request.side != "buy":
                    continue
                if buy.protective_stop_id or buy.protective_stop_quantity > 0:
                    continue  # venue stop armed or unresolved — story not over
                linked_ids = [*buy.partial_exit_ids]
                if buy.exit_order_id:
                    linked_ids.append(buy.exit_order_id)
                linked = [by_id[i] for i in linked_ids if i in by_id]

                if buy.status in {"rejected", "cancelled"} and buy.filled_quantity == 0:
                    closed = not linked  # a dead intent with no exits attached
                elif buy.status in {"filled", "partially_filled", "partially_filled_cancelled"}:
                    closed = (
                        _confirmed_remaining_quantity(buy, by_id) == 0
                        and not _linked_sell_is_working(buy, by_id)
                        and all(sell.status in terminal_sell for sell in linked)
                    )
                else:
                    closed = False
                if not closed:
                    continue

                group = [buy, *linked]
                stamps = [last_activity(o) for o in group]
                if any(t is None for t in stamps):
                    continue  # cannot prove age — keep
                if max(stamps) >= cutoff:
                    continue  # settled too recently
                archive_ids.update(o.id for o in group)

            if not archive_ids:
                return 0
            moved = [o for o in orders if o.id in archive_ids]

            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / f"{datetime.now(timezone.utc):%Y-%m}.json"
            existing: list[dict] = []
            if target.exists():
                try:
                    existing = json.loads(target.read_text(encoding="utf-8")).get("orders", [])
                except Exception:
                    logger.warning("Unreadable archive %s — starting a fresh list", target)
            known = {str(row.get("id")) for row in existing}
            existing.extend(o.to_dict() for o in moved if o.id not in known)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"orders": existing}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, target)

            orders[:] = [o for o in orders if o.id not in archive_ids]
        logger.info("Archived %d settled order rows to %s", len(moved), archive_dir)
        return len(moved)

    def update(self, order: OrderCandidate) -> None:
        """Persist a single in-memory ``OrderCandidate`` back to disk."""
        with self._orders_transaction() as orders:
            for index, existing in enumerate(orders):
                if existing.id == order.id:
                    orders[index] = order
                    return
        raise ApprovalError(f"Order not found: {order.id}")

    def mark_externally_closed(
        self,
        buy_id: str,
        broker_name: str = "external",
        broker: Broker | None = None,
    ) -> OrderCandidate:
        """Record an externally-closed bot position by synthesising a filled sell.

        Used by reconciliation when the broker reports zero quantity for a
        position the queue still considers held — typically because the user
        sold manually through the broker UI. We create a synthetic ``filled``
        sell so downstream filters (``_find_held_buy``, ``manage_open_positions``,
        ``_count_open_positions``) immediately recognise the position as closed.
        """
        with self._orders_transaction() as orders:
            target = next((o for o in orders if o.id == buy_id), None)
            if target is None:
                raise ApprovalError(f"Buy not found: {buy_id}")
            if broker is not None and not order_belongs_to_broker(target, broker):
                raise ApprovalError(
                    f"Order {buy_id} does not belong to broker instance "
                    f"{broker_scope(broker).instance_id}."
                )
            by_id = {o.id: o for o in orders}
            qty = _confirmed_remaining_quantity(target, by_id)
            if qty <= 0:
                raise ApprovalError(f"Buy {buy_id} has no confirmed remaining quantity.")
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
                broker=target.broker if target.broker_instance_id else broker_name,
                broker_instance_id=target.broker_instance_id,
                broker_account_id=target.broker_account_id,
                broker_mode=target.broker_mode,
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
        logger.info(
            "Marked %s as externally closed (synthetic sell %s, qty=%d)",
            buy_id, synthetic.id, qty,
        )
        return synthetic

    def approve(self, order_id: str, broker: Broker) -> tuple[OrderCandidate, OrderResult]:
        scope = broker_scope(broker)
        with self._orders_transaction() as orders:
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
            if target_order.broker_instance_id and not order_belongs_to_broker(target_order, broker):
                raise ApprovalError(
                    f"Order {order_id} belongs to {target_order.broker_instance_id}, "
                    f"not {scope.instance_id}."
                )
            target_order = replace(
                target_order,
                status="submitting",
                submitted_at=utc_now_iso(),
                broker_message="Submitting to broker.",
                **_scope_fields(scope),
            )
            orders[target_index] = target_order

        # Broker call happens outside the lock so we don't block other
        # queue readers while waiting on the network. The order is marked
        # ``submitting`` first so a second approval cannot place it again.
        try:
            result = broker.place_order(target_order.request)
        except BrokerOrderRejected as exc:
            with self._orders_transaction() as orders:
                for index, order in enumerate(orders):
                    if order.id == order_id:
                        orders[index] = replace(
                            order,
                            status="rejected",
                            submitted_at=utc_now_iso(),
                            broker_message=str(exc),
                            rejection_code=exc.code,
                            rejection_retryable=exc.retryable,
                            **_scope_fields(scope),
                        )
                        break
            logger.error("Order %s rejected: %s", order_id, exc)
            raise
        except Exception as exc:
            with self._orders_transaction() as orders:
                for index, order in enumerate(orders):
                    if order.id == order_id:
                        orders[index] = replace(
                            order,
                            # A network exception does not prove rejection: the
                            # broker may have accepted the order before the
                            # connection failed.  Keep this fail-closed until
                            # account/order reconciliation resolves it.
                            status="unknown",
                            submitted_at=utc_now_iso(),
                            broker_message=f"Order outcome unknown: {exc}",
                            **_scope_fields(scope),
                        )
                        break
            logger.error("Order %s outcome unknown: %s", order_id, exc)
            raise

        with self._orders_transaction() as orders:
            updated: OrderCandidate | None = None
            for index, order in enumerate(orders):
                if order.id == order_id:
                    updated = replace(
                        order,
                        status="submitted" if result.accepted else "rejected",
                        submitted_at=utc_now_iso(),
                        broker_order_id=result.broker_order_id,
                        broker_message=result.message,
                        rejection_code=(
                            None
                            if result.accepted
                            else str(
                                result.raw.get("msg_cd")
                                or result.raw.get("code")
                                or result.broker_order_id
                                or "broker-rejected"
                            )
                        ),
                        rejection_retryable=(None if result.accepted else False),
                        **_scope_fields(scope),
                    )
                    orders[index] = updated
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

    def unresolved_orders(self, broker: Broker) -> list[OrderCandidate]:
        """Orders whose broker outcome cannot safely be inferred or retried."""

        return [
            order for order in self.list_orders()
            if order.status in {"submitting", "unknown"}
            and order_belongs_to_broker(order, broker)
        ]

    def unscoped_broker_orders(self, broker: Broker) -> list[OrderCandidate]:
        """Legacy broker-active rows that require explicit account binding."""

        return [
            order for order in self.list_orders()
            if not order.broker_instance_id
            and order.broker == broker.name
            and order.status in _BROKER_ACTIVE_STATUSES
        ]

    @contextmanager
    def _file_lock(self, *, exclusive: bool):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _orders_transaction(self):
        """Cross-process atomic read-modify-write transaction."""

        with self._lock, self._file_lock(exclusive=True):
            orders = self._read_unlocked()
            try:
                yield orders
            except Exception:
                raise
            else:
                self._write_unlocked(orders)

    def _read_unlocked(self) -> list[OrderCandidate]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        rows = raw.get("orders", raw if isinstance(raw, list) else [])
        return [OrderCandidate.from_mapping(row) for row in rows]

    def _write_unlocked(self, orders: list[OrderCandidate]) -> None:
        """Serialize to ``<path>.tmp`` and atomically replace the snapshot."""
        payload = {"orders": [order.to_dict() for order in orders]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
