"""Fundamentals fallback-chain tests.

A missing fundamentals fetch is not a neutral event: the analyzer scores
the name 0/10 with "평가 불가", which caps a 30-point total at 20 and makes
min_score unreachable — indistinguishable from a genuinely weak company.
So the chain's contract is that it never raises, always reports which
source answered, and only reaches the next source when the current one
truly has nothing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from alpha_bot.data.fundamentals_sources import (
    FixtureSource,
    build_quarters_from_series,
    resolve_fundamentals,
    _year_earlier,
    _yoy,
)
from alpha_bot.models import FundamentalsQuarter


class StubSource:
    def __init__(self, name, quarters=None, raises=None):
        self.name = name
        self._quarters = quarters or []
        self._raises = raises
        self.calls = 0

    def fetch(self, ticker, market, limit):
        self.calls += 1
        if self._raises:
            raise self._raises
        return list(self._quarters)


def quarter(period="2026Q1", eps=10.0):
    return FundamentalsQuarter(period=period, eps_yoy=eps, revenue_yoy=5.0)


class ChainTests(unittest.TestCase):
    def _resolve(self, sources, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            return resolve_fundamentals(
                "NVDA", "US", sources=sources, cache_dir=Path(tmp) / "c", **kw
            )

    def test_the_first_answering_source_wins_and_later_ones_are_untouched(self):
        first = StubSource("first", [quarter()])
        second = StubSource("second", [quarter()])
        result = self._resolve([first, second])
        self.assertEqual(result.source, "first")
        self.assertEqual(second.calls, 0)

    def test_an_empty_source_falls_through(self):
        empty = StubSource("empty", [])
        backup = StubSource("backup", [quarter()])
        result = self._resolve([empty, backup])
        self.assertEqual(result.source, "backup")
        self.assertEqual(result.attempts[0], ("empty", "empty"))

    def test_a_raising_source_falls_through_rather_than_propagating(self):
        broken = StubSource("broken", raises=RuntimeError("rate limited"))
        backup = StubSource("backup", [quarter()])
        result = self._resolve([broken, backup])
        self.assertEqual(result.source, "backup")
        self.assertIn("error", result.attempts[0][1])

    def test_total_failure_returns_empty_with_full_provenance(self):
        result = self._resolve([
            StubSource("a", []),
            StubSource("b", raises=ValueError("nope")),
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.source, "none")
        self.assertEqual([name for name, _ in result.attempts], ["a", "b"])

    def test_results_are_cached_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            source = StubSource("slow", [quarter()])
            first = resolve_fundamentals("NVDA", "US", sources=[source], cache_dir=cache)
            second = resolve_fundamentals("NVDA", "US", sources=[source], cache_dir=cache)
            self.assertEqual(source.calls, 1)
            self.assertEqual(first.quarters[0].period, second.quarters[0].period)
            self.assertIn("cache", second.source)

    def test_an_expired_cache_refetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            source = StubSource("s", [quarter()])
            resolve_fundamentals("NVDA", "US", sources=[source], cache_dir=cache)
            resolve_fundamentals(
                "NVDA", "US", sources=[source], cache_dir=cache, cache_ttl=-1
            )
            self.assertEqual(source.calls, 2)

    def test_empty_results_are_not_cached(self):
        # Caching "nothing" would freeze a transient outage in for a day.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            empty = StubSource("empty", [])
            resolve_fundamentals("NVDA", "US", sources=[empty], cache_dir=cache)
            later = StubSource("later", [quarter()])
            result = resolve_fundamentals("NVDA", "US", sources=[later], cache_dir=cache)
            self.assertEqual(result.source, "later")


class YoYTests(unittest.TestCase):
    def test_normal_growth(self):
        self.assertAlmostEqual(_yoy(120.0, 100.0), 20.0)
        self.assertAlmostEqual(_yoy(80.0, 100.0), -20.0)

    def test_a_loss_making_base_is_undefined(self):
        # A negative prior makes the percentage meaningless in sign too.
        self.assertIsNone(_yoy(10.0, 0.0))
        self.assertIsNone(_yoy(10.0, -5.0))

    def test_a_base_effect_is_undefined_not_a_huge_number(self):
        # Ford reported +3200% EPS YoY off a rounding-error quarter, and
        # the scorer hands anything >=25% full marks. Noise must not score.
        self.assertIsNone(_yoy(32.0, 0.001))
        self.assertIsNone(_yoy(1000.0, 1.0))

    def test_large_but_real_growth_still_reports(self):
        # NVDA's genuine ~7x surge must survive the guard.
        self.assertAlmostEqual(_yoy(700.0, 100.0), 600.0)

    def test_missing_inputs_are_undefined(self):
        self.assertIsNone(_yoy(None, 100.0))
        self.assertIsNone(_yoy(100.0, None))


class QuarterPairingTests(unittest.TestCase):
    """YoY pairs by date, never by list position."""

    def test_pairs_the_quarter_one_year_earlier(self):
        ends = ["2026-04-26", "2025-10-26", "2025-07-27", "2025-04-27"]
        self.assertEqual(_year_earlier("2026-04-26", ends), "2025-04-27")

    def test_a_gap_beyond_the_tolerance_yields_no_pair(self):
        self.assertIsNone(_year_earlier("2026-04-26", ["2024-01-01"]))

    def test_a_missing_quarter_does_not_shift_the_pairing(self):
        # EDGAR has no standalone Q4, so positional pairing would compare
        # Q1 against Q2 and report confident nonsense.
        earnings = {
            "2026-03-31": 200.0, "2025-09-30": 150.0,
            "2025-06-30": 140.0, "2025-03-31": 100.0,
        }
        quarters = build_quarters_from_series(earnings, {}, limit=4)
        newest = quarters[0]
        self.assertEqual(newest.period, "2026Q1")
        self.assertAlmostEqual(newest.eps_yoy, 100.0)  # 200 vs 100, not vs 150

    def test_quarters_come_back_newest_first_with_report_dates(self):
        earnings = {"2026-03-31": 120.0, "2025-03-31": 100.0}
        quarters = build_quarters_from_series(earnings, {}, limit=4)
        self.assertEqual(len(quarters), 1)
        self.assertEqual(quarters[0].reported_at, date(2026, 3, 31))

    def test_quarters_without_any_usable_leg_are_dropped(self):
        self.assertEqual(build_quarters_from_series({"2026-03-31": 5.0}, {}), [])


class FixtureSourceTests(unittest.TestCase):
    def test_reads_the_same_files_the_old_path_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fundamentals").mkdir(parents=True)
            (root / "fundamentals" / "US_NVDA.json").write_text(
                json.dumps([{"period": "2026Q1", "eps_yoy": 50.0, "revenue_yoy": 30.0}]),
                encoding="utf-8",
            )
            quarters = FixtureSource(root).fetch("nvda", "US", 4)
            self.assertEqual(len(quarters), 1)
            self.assertEqual(quarters[0].eps_yoy, 50.0)

    def test_a_missing_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(FixtureSource(Path(tmp)).fetch("NOPE", "US", 4), [])


class MarketRoutingTests(unittest.TestCase):
    def test_us_sources_decline_kr_tickers_and_vice_versa(self):
        from alpha_bot.data.fundamentals_sources import (
            NaverFinanceSource, SecEdgarSource, YFinanceSource,
        )
        self.assertEqual(YFinanceSource().fetch("005930", "KR", 4), [])
        self.assertEqual(SecEdgarSource().fetch("005930", "KR", 4), [])
        self.assertEqual(NaverFinanceSource().fetch("NVDA", "US", 4), [])

    def test_default_chains_are_ordered_by_market(self):
        from alpha_bot.data.fundamentals_sources import default_sources
        self.assertEqual(
            [s.name for s in default_sources("US")],
            ["yfinance", "sec-edgar", "fixture"],
        )
        self.assertEqual(
            [s.name for s in default_sources("KR")],
            ["naver", "kis", "fixture"],
        )


if __name__ == "__main__":
    unittest.main()
