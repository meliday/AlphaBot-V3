from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_bot.auto.watchdog import check_heartbeat, write_heartbeat
from alpha_bot.runner import build_parser, cmd_auto


class HeartbeatTests(unittest.TestCase):
    def test_atomic_heartbeat_is_private_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertTrue(
                write_heartbeat("monitor", status="running", directory=directory)
            )
            path = directory / "monitor.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            health = check_heartbeat(
                "monitor", 60, directory=directory,
                now_epoch=record["updated_epoch"] + 10,
            )
            self.assertTrue(health.healthy)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_and_reported_stopped_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_heartbeat("auto", status="running", directory=directory)
            record = json.loads((directory / "auto.json").read_text(encoding="utf-8"))
            stale = check_heartbeat(
                "auto", 30, directory=directory,
                now_epoch=record["updated_epoch"] + 31,
            )
            self.assertFalse(stale.healthy)
            self.assertIn("stale", stale.reason)

            write_heartbeat("auto", status="stopped", directory=directory)
            stopped = check_heartbeat("auto", 30, directory=directory)
            self.assertFalse(stopped.healthy)
            self.assertIn("stopped", stopped.reason)

    def test_missing_and_malformed_heartbeats_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertFalse(
                check_heartbeat("monitor", 60, directory=directory).healthy
            )
            (directory / "monitor.json").write_text("not-json", encoding="utf-8")
            health = check_heartbeat("monitor", 60, directory=directory)
            self.assertFalse(health.healthy)
            self.assertIn("invalid", health.reason)


class RunnerRegressionTests(unittest.TestCase):
    def test_auto_enters_iteration_without_referencing_backtest_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            universe = Path(tmp) / "watchlist.yaml"
            universe.write_text(
                "universe:\n  - ticker: NVDA\n    market: US\n", encoding="utf-8"
            )
            args = build_parser().parse_args(
                ["--config", str(Path(tmp) / "missing.yaml"), "auto",
                 "--universe", str(universe), "--demo"]
            )
            with patch(
                "alpha_bot.runner.run_auto_iteration", side_effect=KeyboardInterrupt
            ) as iteration, patch(
                "alpha_bot.auto.watchdog.write_heartbeat"
            ):
                self.assertEqual(cmd_auto(args), 0)
            iteration.assert_called_once()


if __name__ == "__main__":
    unittest.main()
