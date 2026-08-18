"""config.yaml write-path tests.

These fields size real orders and gate real safety machinery, so the
browser cannot be the trust boundary: every value is bounds-checked
server-side, unknown keys are refused rather than silently written, and a
single bad field rejects the whole update — a half-applied risk config is
worse than no change at all.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_bot.config import load_config
from alpha_bot.web import handlers_config


SEED = """# AlphaBot config
default_market: US
broker: mock
max_positions: 5
risk_per_trade_pct: 1.0
min_score: 24
min_rr: 1.2

# ── 보호 종목 ──
protected_tickers: VOO, QQQM
protective_stop: false
"""


class ConfigSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.yaml"
        self.path.write_text(SEED, encoding="utf-8")
        patcher = patch.object(handlers_config, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, config: dict):
        return handlers_config.handle_save_config({"config": config})

    def test_a_valid_update_is_applied_and_reloads(self):
        result = self.save({"min_rr": 1.5, "protective_stop": True})
        self.assertEqual(result["saved"], ["min_rr", "protective_stop"])
        reloaded = load_config(self.path)
        self.assertEqual(reloaded.min_rr, 1.5)
        self.assertTrue(reloaded.protective_stop)

    def test_comments_and_untouched_keys_survive(self):
        self.save({"min_rr": 2.0})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# AlphaBot config", text)
        self.assertIn("# ── 보호 종목 ──", text)
        self.assertIn("broker: mock", text)

    def test_out_of_range_values_are_refused(self):
        for field, bad in [
            ("min_score", 99),            # scoreboard is out of 30
            ("risk_per_trade_pct", -1),
            ("max_positions", 0),
            ("max_position_pct", 150),
            ("stale_order_minutes", 99999),
        ]:
            result = self.save({field: bad})
            self.assertIsInstance(result, tuple, field)
            self.assertEqual(result[1], 400, field)

    def test_an_unknown_key_is_refused_not_silently_written(self):
        result = self.save({"totally_made_up": 1})
        self.assertIsInstance(result, tuple)
        self.assertIn("알 수 없는", result[0])
        self.assertNotIn("totally_made_up", self.path.read_text(encoding="utf-8"))

    def test_one_bad_field_rejects_the_whole_update(self):
        result = self.save({"min_rr": 1.5, "max_positions": 0})
        self.assertIsInstance(result, tuple)
        # The good field must not have landed either.
        self.assertEqual(load_config(self.path).min_rr, 1.2)

    def test_protected_tickers_normalise_and_validate(self):
        self.save({"protected_tickers": "voo, brk.b  schd"})
        self.assertEqual(
            load_config(self.path).protected_tickers,
            frozenset({"VOO", "BRK.B", "SCHD"}),
        )
        result = self.save({"protected_tickers": "VOO, bad;ticker"})
        self.assertIsInstance(result, tuple)

    def test_clearing_protected_tickers_is_allowed(self):
        # Deliberate: the operator must be able to un-protect a holding.
        self.save({"protected_tickers": ""})
        self.assertEqual(load_config(self.path).protected_tickers, frozenset())

    def test_booleans_accept_the_shapes_a_form_actually_sends(self):
        for value in (True, "true", "1", "on"):
            self.save({"protective_stop": value})
            self.assertTrue(load_config(self.path).protective_stop, value)
        for value in (False, "false", "0", "off"):
            self.save({"protective_stop": value})
            self.assertFalse(load_config(self.path).protective_stop, value)

    def test_broker_and_market_are_enumerated(self):
        self.assertEqual(self.save({"broker": "toss"})["saved"], ["broker"])
        self.assertIsInstance(self.save({"broker": "robinhood"}), tuple)
        self.assertIsInstance(self.save({"default_market": "JP"}), tuple)

    def test_an_empty_payload_is_refused(self):
        self.assertIsInstance(self.save({}), tuple)
        self.assertIsInstance(
            handlers_config.handle_save_config({"config": "nope"}), tuple
        )

    def test_a_new_key_absent_from_the_file_is_appended(self):
        self.path.write_text("broker: mock\n", encoding="utf-8")
        self.save({"protective_stop": True})
        self.assertTrue(load_config(self.path).protective_stop)



class SerialiserTests(unittest.TestCase):
    """Config round-trips through JSON, so every field type must survive it.

    frozenset fell through to str() and leaked "frozenset({'VOO'})" into
    the protected_tickers form input — which would then have been saved
    back verbatim as a ticker list.
    """

    def test_sets_serialise_as_sorted_arrays(self):
        from alpha_bot.web.server import _serialise
        self.assertEqual(_serialise(frozenset({"VOO", "BRK.B"})), ["BRK.B", "VOO"])
        self.assertEqual(_serialise(set()), [])

    def test_a_full_config_survives_serialisation(self):
        from alpha_bot.web.server import _serialise
        payload = _serialise(load_config(Path("config.yaml")))
        self.assertIsInstance(payload["protected_tickers"], list)
        self.assertNotIn("frozenset", str(payload))

if __name__ == "__main__":
    unittest.main()
