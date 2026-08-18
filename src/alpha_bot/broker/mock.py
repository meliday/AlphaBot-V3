from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from alpha_bot.errors import BrokerError
from alpha_bot.models import (
    AccountBalance,
    Market,
    OrderFill,
    OrderRequest,
    OrderResult,
    Position,
    utc_now_iso,
)


class MockBroker:
    """In-process broker simulator backed by a JSON ledger.

    Two files persist state:
      * ``mock_orders.json`` — append-only ledger of every order (buy/sell).
      * ``mock_state.json`` — config overrides (per-market starting cash).

    Cash is computed dynamically from the ledger:
        cash = starting_cash - Σ(buys × price) + Σ(sells × price)
    so manual orders from the Mock-Sim UI immediately move the balance.
    """

    name = "mock"
    DEFAULT_STARTING_CASH: dict[str, float] = {"KR": 10_000_000.0, "US": 10_000.0}

    def __init__(
        self,
        ledger_path: Path = Path("mock_orders.json"),
        state_path: Path = Path("mock_state.json"),
    ):
        self.ledger_path = ledger_path
        self.state_path = state_path

    @property
    def instance_id(self) -> str:
        raw = f"{self.ledger_path.resolve()}|{self.state_path.resolve()}"
        return f"mock:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    @property
    def account_id(self) -> str:
        return self.instance_id

    @property
    def mode(self) -> str:
        return "simulation"

    # ── Order placement ─────────────────────────────────────────────

    def place_order(self, order: OrderRequest) -> OrderResult:
        if order.quantity <= 0:
            raise BrokerError("Order quantity must be positive.")
        if order.order_type == "limit" and order.limit_price is None:
            raise BrokerError("Limit orders require limit_price.")

        broker_order_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
        row = {
            "broker_order_id": broker_order_id,
            "created_at": utc_now_iso(),
            "ticker": order.ticker,
            "market": order.market,
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
            "reason": order.reason,
        }
        rows = self._read()
        rows.append(row)
        self._write_ledger(rows)
        return OrderResult(self.name, True, broker_order_id, "Mock order accepted.", row)

    def place_manual_order(
        self,
        ticker: str,
        market: Market,
        side: str,
        quantity: int,
        price: float,
        note: str = "manual sim",
    ) -> OrderResult:
        """Place an order from the Mock-Sim UI, tagged so it's distinguishable."""
        if side not in {"buy", "sell"}:
            raise BrokerError(f"Invalid side: {side}")
        req = OrderRequest(
            ticker=ticker.upper(),
            market=market,
            side=side,  # type: ignore[arg-type]
            quantity=int(quantity),
            order_type="limit",
            limit_price=float(price),
            reason=note,
        )
        result = self.place_order(req)
        # Tag the most recent ledger row as manual.
        rows = self._read()
        for row in reversed(rows):
            if row.get("broker_order_id") == result.broker_order_id:
                row["manual"] = True
                break
        self._write_ledger(rows)
        return result

    def inject_position(
        self,
        ticker: str,
        market: Market,
        quantity: int,
        avg_price: float,
    ) -> OrderResult:
        """Simulate a pre-existing holding by writing a synthetic buy row.

        Useful for testing the bot's reconciliation logic: inject a position
        the bot never bought, then watch how it behaves on the next iteration.
        """
        broker_order_id = f"INJECT-{uuid.uuid4().hex[:10].upper()}"
        row = {
            "broker_order_id": broker_order_id,
            "created_at": utc_now_iso(),
            "ticker": ticker.upper(),
            "market": market,
            "side": "buy",
            "quantity": int(quantity),
            "order_type": "limit",
            "limit_price": float(avg_price),
            "reason": "injected (pre-existing holding)",
            "injected": True,
        }
        rows = self._read()
        rows.append(row)
        self._write_ledger(rows)
        return OrderResult(self.name, True, broker_order_id, "Position injected.", row)

    def cancel_order(
        self, broker_order_id: str, market: Market, ticker: str, quantity: int
    ) -> OrderResult:
        """Mark a ledger order cancelled. Cancelled rows stop counting toward
        cash, positions, and fills — mirroring a real broker voiding the
        unfilled remainder."""
        rows = self._read()
        for row in rows:
            if row.get("broker_order_id") == broker_order_id:
                if row.get("cancelled"):
                    return OrderResult(self.name, True, broker_order_id, "Already cancelled.", row)
                row["cancelled"] = True
                row["cancelled_at"] = utc_now_iso()
                self._write_ledger(rows)
                return OrderResult(self.name, True, broker_order_id, "Mock order cancelled.", row)
        return OrderResult(self.name, False, broker_order_id, "Order not found in ledger.", {})

    # ── State queries ───────────────────────────────────────────────

    def get_cash_balance(self, market: Market) -> AccountBalance:
        currency = "KRW" if market == "KR" else "USD"
        starting = self.get_starting_cash(market)
        cash = starting
        for row in self._read():
            if row.get("market") != market:
                continue
            if row.get("cancelled"):
                continue
            if row.get("injected"):
                # Pre-existing holdings shouldn't draw down the starting cash;
                # they represent assets the user "already had" before sim.
                continue
            qty = int(row.get("quantity", 0) or 0)
            price = float(row.get("limit_price") or 0)
            value = qty * price
            if row.get("side") == "buy":
                cash -= value
            elif row.get("side") == "sell":
                cash += value
        # Securities value = sum of position market values (cost-basis in mock).
        positions = self.get_positions(market)
        sec_value = sum(p.market_value for p in positions)
        total = cash + sec_value
        return AccountBalance(
            broker=self.name,
            market=market,
            currency=currency,
            cash=cash,
            securities_value=sec_value,
            total_value=total,
            pnl=0.0,
            pnl_pct=0.0,
            raw={"mock": True, "starting_cash": starting},
        )

    def get_positions(self, market: Market) -> list[Position]:
        rows = [
            row for row in self._read()
            if row.get("market") == market and not row.get("cancelled")
        ]
        holdings: dict[str, dict] = {}
        for row in rows:
            ticker = str(row["ticker"])
            qty = int(row["quantity"])
            price = float(row.get("limit_price") or 0.0)
            entry = holdings.setdefault(ticker, {"quantity": 0, "cost": 0.0})
            if row["side"] == "buy":
                entry["cost"] += qty * price
                entry["quantity"] += qty
                continue

            held_qty = entry["quantity"]
            if held_qty > 0:
                avg_cost = entry["cost"] / held_qty
                sold_qty = min(qty, held_qty)
                entry["cost"] -= avg_cost * sold_qty
                if sold_qty == held_qty:
                    entry["cost"] = 0.0
            entry["quantity"] -= qty
        currency = "KRW" if market == "KR" else "USD"
        out: list[Position] = []
        for ticker, info in holdings.items():
            qty = info["quantity"]
            if qty <= 0:
                continue
            avg = info["cost"] / qty if qty else 0.0
            value = avg * qty
            out.append(
                Position(
                    broker=self.name,
                    ticker=ticker,
                    market=market,
                    quantity=qty,
                    avg_price=avg,
                    current_price=avg,
                    market_value=value,
                    pnl=0.0,
                    pnl_pct=0.0,
                    currency=currency,
                    raw={"mock": True},
                )
            )
        return out

    def get_order_fill(
        self, broker_order_id: str, market: Market, ordered_quantity: int
    ) -> OrderFill:
        for row in self._read():
            if row.get("broker_order_id") == broker_order_id:
                if row.get("cancelled"):
                    return OrderFill(
                        broker_order_id=broker_order_id,
                        status="cancelled",
                        filled_quantity=0,
                        ordered_quantity=ordered_quantity,
                        avg_fill_price=None,
                        message="Mock order cancelled",
                        raw=row,
                    )
                qty = int(row.get("quantity", ordered_quantity))
                return OrderFill(
                    broker_order_id=broker_order_id,
                    status="filled",
                    filled_quantity=qty,
                    ordered_quantity=ordered_quantity or qty,
                    avg_fill_price=float(row.get("limit_price") or 0.0) or None,
                    message="Mock fill (assumed immediate)",
                    raw=row,
                )
        return OrderFill(
            broker_order_id=broker_order_id,
            status="submitted",
            filled_quantity=0,
            ordered_quantity=ordered_quantity,
            avg_fill_price=None,
            message="Mock order not found in ledger",
        )

    # ── Sim controls (used by the Mock-Sim UI) ──────────────────────

    def get_starting_cash(self, market: Market) -> float:
        state = self._read_state()
        cash = state.get("starting_cash", {}).get(market)
        if cash is None:
            return self.DEFAULT_STARTING_CASH.get(market, 0.0)
        return float(cash)

    def set_starting_cash(self, market: Market, amount: float) -> None:
        state = self._read_state()
        state.setdefault("starting_cash", {})[market] = float(amount)
        self._write_state(state)

    def delete_order(self, broker_order_id: str) -> bool:
        rows = self._read()
        kept = [r for r in rows if r.get("broker_order_id") != broker_order_id]
        if len(kept) == len(rows):
            return False
        self._write_ledger(kept)
        return True

    def reset_state(self, keep_starting_cash: bool = True) -> None:
        """Wipe the ledger. Optionally preserve starting-cash config."""
        self._write_ledger([])
        if not keep_starting_cash and self.state_path.exists():
            self.state_path.unlink()

    def list_orders_raw(self) -> list[dict]:
        """Return the full ledger (for the Mock-Sim UI)."""
        return self._read()

    # ── Private I/O ─────────────────────────────────────────────────

    # ── ProtectiveStopBroker capability (simulated) ─────────────────
    #
    # Stops are recorded in mock_state.json and stay ``WATCHING`` forever —
    # the mock never fires them. That is deliberate: this exists so the
    # arm/amend/cancel bookkeeping in the sync layer is exercisable without a
    # venue, not to simulate stop execution. Backtests remain the place where
    # stop fills are modelled.

    def place_protective_stop(
        self,
        *,
        ticker: str,
        market: Market,
        quantity: int,
        stop_price: float,
        client_order_id: str,
    ) -> str:
        if quantity <= 0:
            raise BrokerError("Protective stop quantity must be positive.")
        if stop_price <= 0:
            raise BrokerError("Protective stop price must be positive.")
        state = self._read_state()
        stops = state.setdefault("protective_stops", {})
        # Honour the venue's idempotency contract: replaying one
        # client_order_id must not create a second stop.
        for existing_id, row in stops.items():
            if row.get("client_order_id") == client_order_id:
                return existing_id
        stop_id = f"MOCKCOND-{uuid.uuid4().hex[:12].upper()}"
        stops[stop_id] = {
            "ticker": ticker.upper(),
            "market": market,
            "quantity": int(quantity),
            "stop_price": float(stop_price),
            "client_order_id": client_order_id,
            "status": "WATCHING",
        }
        self._write_state(state)
        return stop_id

    def list_protective_stop_ids(self, ticker: str) -> list[str]:
        stops = self._read_state().get("protective_stops", {})
        symbol = ticker.upper()
        return [
            stop_id for stop_id, row in stops.items()
            if str(row.get("ticker", "")).upper() == symbol
        ]

    def cancel_protective_stop(self, stop_id: str) -> None:
        state = self._read_state()
        stops = state.setdefault("protective_stops", {})
        if stops.pop(stop_id, None) is not None:
            self._write_state(state)

    def protective_stop_status(self, stop_id: str) -> str | None:
        row = self._read_state().get("protective_stops", {}).get(stop_id)
        return str(row["status"]) if row else None

    def _read(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write_ledger(self, rows: list[dict]) -> None:
        self.ledger_path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        self.state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
