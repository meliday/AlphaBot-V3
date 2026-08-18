"""CLI argument regressions.

`bot scan` shipped without the --kis-data/--toss-data flags while
_provider() read them unconditionally, so every scan invocation crashed
with AttributeError. Pin: every subcommand that reaches _provider must
parse cleanly and resolve a provider.
"""

from __future__ import annotations

import unittest

from alpha_bot.runner import _provider, build_parser
from pathlib import Path


class ProviderResolutionTests(unittest.TestCase):
    def _resolve(self, argv: list[str]):
        args = build_parser().parse_args(argv)
        return _provider(args, Path("data"))

    def test_scan_demo_resolves_a_provider(self):
        provider = self._resolve(["scan", "--universe", "w.yaml", "--demo"])
        self.assertEqual(type(provider).__name__, "SyntheticDataProvider")

    def test_scan_without_flags_falls_back_to_local(self):
        provider = self._resolve(["scan", "--universe", "w.yaml"])
        self.assertEqual(type(provider).__name__, "FixtureDataProvider")

    def test_analyze_demo_still_resolves(self):
        provider = self._resolve(
            ["analyze", "--ticker", "NVDA", "--market", "US", "--demo"]
        )
        self.assertEqual(type(provider).__name__, "SyntheticDataProvider")


if __name__ == "__main__":
    unittest.main()
