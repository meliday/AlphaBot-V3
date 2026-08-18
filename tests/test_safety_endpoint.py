"""Safety endpoint tests.

This endpoint powers a status bar on every screen, so its contract is as
much about *failure* behaviour as about happy paths: it must never raise,
a probe that could not answer must read as a warning rather than green,
and "halted" must mean new buys are genuinely blocked right now.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_bot.approval import ApprovalQueue
from alpha_bot.web.handlers_safety import _overall, handle_safety


def state(**overrides) -> dict:
    base = {
        "protective_stop_enabled": False,
        "kill_switch": {"active": False, "reason": ""},
        "heartbeats": {},
        "positions": {"held": [], "count": 0, "armed": 0, "pending_intent": 0},
        "broker_state": {
            "legacy_orders": 0, "unresolved_orders": 0,
            "breakers": {}, "portfolio_krw": 1000.0,
        },
    }
    base.update(overrides)
    return base


class VerdictTests(unittest.TestCase):
    def test_a_clean_state_is_ok(self):
        self.assertEqual(_overall(state())["level"], "ok")

    def test_kill_switch_halts(self):
        verdict = _overall(state(kill_switch={"active": True, "reason": "손절 점검"}))
        self.assertEqual(verdict["level"], "halted")
        self.assertIn("손절 점검", verdict["reasons"][0])

    def test_legacy_and_unresolved_orders_halt(self):
        for field in ("legacy_orders", "unresolved_orders"):
            broker_state = dict(state()["broker_state"])
            broker_state[field] = 2
            verdict = _overall(state(broker_state=broker_state))
            self.assertEqual(verdict["level"], "halted", field)

    def test_a_tripped_breaker_halts(self):
        broker_state = dict(state()["broker_state"])
        broker_state["breakers"] = {"US": {"tripped": True, "detail": "-3.2%"}}
        verdict = _overall(state(broker_state=broker_state))
        self.assertEqual(verdict["level"], "halted")
        self.assertIn("US", verdict["reasons"][0])

    def test_an_unreadable_probe_warns_rather_than_showing_green(self):
        # The whole point: unverified must never render as safe.
        verdict = _overall(state(kill_switch={"active": None, "reason": "확인 실패"}))
        self.assertEqual(verdict["level"], "warn")

    def test_unprotected_positions_warn_only_when_the_feature_is_on(self):
        positions = {"held": [{}], "count": 1, "armed": 0, "pending_intent": 0}
        self.assertEqual(_overall(state(positions=positions))["level"], "ok")
        verdict = _overall(state(positions=positions, protective_stop_enabled=True))
        self.assertEqual(verdict["level"], "warn")
        self.assertIn("미무장 1건", verdict["reasons"][0])

    def test_fully_armed_positions_are_ok(self):
        positions = {"held": [{}], "count": 1, "armed": 1, "pending_intent": 0}
        verdict = _overall(state(positions=positions, protective_stop_enabled=True))
        self.assertEqual(verdict["level"], "ok")

    def test_an_absent_heartbeat_is_not_an_alarm(self):
        # A stopped bot is a normal state; only a stale heartbeat is news.
        verdict = _overall(state(heartbeats={
            "auto": {"healthy": False, "detail": "heartbeat missing: runtime/auto.json"}
        }))
        self.assertEqual(verdict["level"], "ok")

    def test_a_stale_heartbeat_warns(self):
        verdict = _overall(state(heartbeats={
            "auto": {"healthy": False, "detail": "heartbeat stale: 4000s > 900s"}
        }))
        self.assertEqual(verdict["level"], "warn")

    def test_halted_reasons_include_warnings_too(self):
        verdict = _overall(state(
            kill_switch={"active": True, "reason": ""},
            heartbeats={"auto": {"healthy": False, "detail": "heartbeat stale: 9999s"}},
        ))
        self.assertEqual(verdict["level"], "halted")
        self.assertEqual(len(verdict["reasons"]), 2)


class ResilienceTests(unittest.TestCase):
    """A broker outage must degrade one field, never blank the bar."""

    def test_a_broker_failure_still_returns_a_usable_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            from alpha_bot.config import AppConfig

            config = AppConfig(approval_queue=Path(tmp) / "pending.json", broker="toss")
            with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
                 patch("alpha_bot.auto.analysis.make_broker",
                       side_effect=RuntimeError("venue unreachable")):
                payload = handle_safety()  # must not raise

            self.assertIsNone(payload["broker_state"]["legacy_orders"])
            self.assertIsNone(payload["broker_state"]["portfolio_krw"])
            # Unreadable order-binding state is a warning, not silence.
            self.assertEqual(payload["overall"]["level"], "warn")

    def test_config_fields_are_echoed_for_the_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            from alpha_bot.config import AppConfig

            config = AppConfig(
                approval_queue=Path(tmp) / "pending.json",
                broker="toss", protective_stop=True,
                protected_tickers=frozenset({"VOO", "SCHD"}),
            )
            with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
                 patch("alpha_bot.auto.analysis.make_broker",
                       side_effect=RuntimeError("no creds")):
                payload = handle_safety()

            self.assertEqual(payload["broker"], "toss")
            self.assertTrue(payload["protective_stop_enabled"])
            self.assertEqual(payload["protected_tickers"], ["SCHD", "VOO"])



class HeartbeatProbeTests(unittest.TestCase):
    """The probe must actually read the watchdog dataclass, not just not-crash.

    Caught by a stray log line: a wrong attribute name made every heartbeat
    read fail silently into {} — the probe wrapper turned a typo into a
    permanently blank field, which is exactly the "unverified looks fine"
    failure the endpoint is supposed to prevent.
    """

    def test_heartbeat_fields_are_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            from alpha_bot.auto.watchdog import write_heartbeat
            from alpha_bot.config import AppConfig

            directory = Path(tmp) / "hb"
            write_heartbeat("auto", directory=directory)
            config = AppConfig(approval_queue=Path(tmp) / "pending.json")
            with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
                 patch("alpha_bot.auto.watchdog.heartbeat_dir", return_value=directory):
                payload = handle_safety()

            auto = payload["heartbeats"]["auto"]
            self.assertTrue(auto["healthy"], auto)
            self.assertEqual(auto["detail"], "ok")
            self.assertIsNotNone(auto["age_seconds"])


class GateTests(unittest.TestCase):
    """The gate panel must mirror run_auto_iteration's real guard order.

    If it drifts, the dashboard confidently explains a decision path the
    bot does not actually follow — worse than showing nothing.
    """

    def _gates(self, tmp: str, watchlist: str, **config_kw):
        from alpha_bot.config import AppConfig
        from alpha_bot.web import handlers_safety

        (Path(tmp) / "watchlist.yaml").write_text(watchlist, encoding="utf-8")
        config = AppConfig(approval_queue=Path(tmp) / "pending.json", **config_kw)
        with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
             patch.object(handlers_safety, "Path", Path), \
             patch("alpha_bot.auto.analysis.make_broker",
                   side_effect=RuntimeError("no broker in tests")):
            import os
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                return handlers_safety.handle_gates()
            finally:
                os.chdir(cwd)

    def test_protected_tickers_report_their_block_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._gates(
                tmp,
                "universe:\n  - ticker: VOO\n    market: US\n"
                "  - ticker: NVDA\n    market: US\n",
                protected_tickers=frozenset({"VOO"}),
            )
            blocked = {t["ticker"]: t["blocked_by"] for t in payload["tickers"]}
            self.assertEqual(blocked["VOO"], "보호 종목")
            self.assertIsNone(blocked["NVDA"])

    def test_capacity_reflects_the_position_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._gates(
                tmp, "universe:\n  - ticker: NVDA\n    market: US\n",
                max_positions=3,
            )
            self.assertEqual(payload["capacity"]["max"], 3)
            self.assertFalse(payload["capacity"]["full"])

    def test_a_broker_outage_degrades_fields_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._gates(
                tmp, "universe:\n  - ticker: NVDA\n    market: US\n"
            )
            us = payload["markets"]["US"]
            self.assertIsNone(us["breaker"]["tripped"])
            self.assertIsNone(us["cash"]["available"])
            # Unknown gates must not read as passed.
            self.assertIn(us["breaker"]["detail"], {"브로커 없음", "확인 실패"})


class ReconcileTests(unittest.TestCase):
    """Bot belief vs venue truth — the gap nearly every live defect hid in."""

    def _reconcile(self, tmp: str, *, venue_items, seed_buy=None, **config_kw):
        from dataclasses import replace as _replace
        from alpha_bot.config import AppConfig
        from alpha_bot.models import OrderRequest
        from alpha_bot.web import handlers_safety

        config = AppConfig(approval_queue=Path(tmp) / "pending.json", **config_kw)
        queue = ApprovalQueue(config.approval_queue)
        if seed_buy:
            ticker, qty = seed_buy
            order = queue.enqueue(
                OrderRequest(ticker, "US", "buy", qty, "limit", 100.0),
                stop_loss=90.0, target1=120.0, target2=140.0,
            )
            queue.update(_replace(
                order, status="filled", filled_quantity=qty, avg_fill_price=100.0,
                broker="toss", broker_instance_id="toss:test",
                broker_account_id="acct", broker_mode="live",
            ))

        class Client:
            def request(self, method, path, **kwargs):
                return {"result": {"items": venue_items}}

        class Broker:
            name = "toss"
            client = Client()
            account_seq = 1

            def list_protective_stop_ids(self, ticker):
                return []

        with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
             patch("alpha_bot.auto.analysis.make_broker", return_value=Broker()):
            return handlers_safety.handle_reconcile({"market": "US"})

    def _item(self, symbol, quantity):
        return {
            "symbol": symbol, "marketCountry": "US", "quantity": str(quantity),
            "averagePurchasePrice": "100", "lastPrice": "101", "currency": "USD",
        }

    def test_holdings_are_classified_not_merely_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._reconcile(
                tmp,
                venue_items=[self._item("F", 1), self._item("VOO", 2),
                             self._item("TSLA", 3)],
                seed_buy=("F", 1),
                protected_tickers=frozenset({"VOO"}),
            )
            kinds = {h["ticker"]: h["kind"] for h in payload["holdings"]}
            self.assertEqual(kinds, {"F": "bot", "VOO": "protected", "TSLA": "manual"})

    def test_fractional_holdings_keep_their_real_quantity(self):
        # The bot's whole-share model floors these to zero; an account
        # screen that hides real holdings is lying.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._reconcile(
                tmp, venue_items=[self._item("BRK.B", "0.2257")],
                protected_tickers=frozenset({"BRK.B"}),
            )
            row = payload["holdings"][0]
            self.assertAlmostEqual(row["venue_quantity"], 0.2257)
            self.assertTrue(row["fractional"])

    def test_a_partial_external_sell_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._reconcile(
                tmp, venue_items=[self._item("F", 1)], seed_buy=("F", 5),
            )
            row = next(h for h in payload["holdings"] if h["ticker"] == "F")
            self.assertIn("부분 외부 매도", row["mismatch"])

    def test_a_full_external_sell_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._reconcile(tmp, venue_items=[], seed_buy=("F", 5))
            row = next(h for h in payload["holdings"] if h["ticker"] == "F")
            self.assertIn("거래소에 없음", row["mismatch"])

    def test_matching_quantities_report_no_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._reconcile(
                tmp, venue_items=[self._item("F", 1)], seed_buy=("F", 1),
            )
            self.assertIsNone(payload["holdings"][0]["mismatch"])

    def test_a_venue_outage_suppresses_diffing_rather_than_crying_wolf(self):
        # Without venue data every bot position would look externally sold.
        with tempfile.TemporaryDirectory() as tmp:
            from alpha_bot.config import AppConfig
            from alpha_bot.web import handlers_safety

            config = AppConfig(approval_queue=Path(tmp) / "pending.json")
            with patch("alpha_bot.web.handlers_safety.load_config", return_value=config), \
                 patch("alpha_bot.auto.analysis.make_broker",
                       side_effect=RuntimeError("venue down")):
                payload = handlers_safety.handle_reconcile({"market": "US"})

            self.assertFalse(payload["venue_available"])
            self.assertTrue(all(h["mismatch"] is None for h in payload["holdings"]))
            self.assertIsNone(payload["integrity"]["legacy_orders"])

if __name__ == "__main__":
    unittest.main()
