"""KIS websocket frame parsing and tick→bar aggregation (all offline)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from alpha_bot.data.bars import BarAggregator
from alpha_bot.data.stream import Tick, build_subscribe_frame, parse_frame

_NOW = datetime(2026, 7, 10, 10, 0, 0)


def _record(ticker="005930", hhmmss="093015", price="71900", tick_vol="150", cum_vol="123456"):
    """A minimal H0STCNT0 record: indices 0/1/2/12/13 meaningful, rest filler."""
    fields = ["0"] * 20
    fields[0] = ticker
    fields[1] = hhmmss
    fields[2] = price
    fields[12] = tick_vol
    fields[13] = cum_vol
    return fields


class FrameParsingTests(unittest.TestCase):
    def test_subscribe_frame_shape(self):
        frame = json.loads(build_subscribe_frame("KEY", "H0STCNT0", "005930"))
        self.assertEqual(frame["header"]["approval_key"], "KEY")
        self.assertEqual(frame["header"]["tr_type"], "1")
        self.assertEqual(frame["body"]["input"], {"tr_id": "H0STCNT0", "tr_key": "005930"})
        off = json.loads(build_subscribe_frame("KEY", "H0STCNT0", "005930", subscribe=False))
        self.assertEqual(off["header"]["tr_type"], "2")

    def test_single_tick_frame(self):
        raw = "0|H0STCNT0|001|" + "^".join(_record())
        kind, ticks = parse_frame(raw, now=_NOW)
        self.assertEqual(kind, "ticks")
        self.assertEqual(len(ticks), 1)
        tick = ticks[0]
        self.assertEqual(tick.ticker, "005930")
        self.assertEqual(tick.price, 71900.0)
        self.assertEqual(tick.volume, 150)
        self.assertEqual(tick.cum_volume, 123456)
        self.assertEqual((tick.time.hour, tick.time.minute, tick.time.second), (9, 30, 15))

    def test_multi_record_frame(self):
        blob = "^".join(_record(hhmmss="093015", price="71900")
                        + _record(hhmmss="093016", price="71950", tick_vol="30"))
        kind, ticks = parse_frame(f"0|H0STCNT0|002|{blob}", now=_NOW)
        self.assertEqual(kind, "ticks")
        self.assertEqual([t.price for t in ticks], [71900.0, 71950.0])
        self.assertEqual(ticks[1].volume, 30)

    def test_pingpong_must_be_echoed(self):
        raw = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260710100000"}})
        kind, payload = parse_frame(raw)
        self.assertEqual(kind, "pingpong")
        self.assertEqual(payload, raw)  # echo verbatim

    def test_subscribe_ack_is_control(self):
        raw = json.dumps({
            "header": {"tr_id": "H0STCNT0", "tr_key": "005930", "encrypt": "N"},
            "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS"},
        })
        kind, payload = parse_frame(raw)
        self.assertEqual(kind, "control")
        self.assertEqual(payload["body"]["msg1"], "SUBSCRIBE SUCCESS")

    def test_encrypted_and_malformed_frames_are_unknown(self):
        self.assertEqual(parse_frame("1|H0STCNI0|001|abc")[0], "unknown")
        self.assertEqual(parse_frame("0|H0STCNT0|001")[0], "unknown")
        self.assertEqual(parse_frame("0|H0STCNT0|001|a^b^c")[0], "unknown")  # too few fields
        self.assertEqual(parse_frame("")[0], "unknown")
        self.assertEqual(parse_frame("{not json")[0], "unknown")


def _tick(hh, mm, ss, price, vol, ticker="005930"):
    return Tick(ticker, datetime(2026, 7, 10, hh, mm, ss), price, vol, 0)


class BarAggregatorTests(unittest.TestCase):
    def test_bar_completes_on_next_interval(self):
        agg = BarAggregator(60)
        self.assertIsNone(agg.on_tick(_tick(9, 30, 5, 100.0, 10)))
        self.assertIsNone(agg.on_tick(_tick(9, 30, 20, 102.0, 20)))
        self.assertIsNone(agg.on_tick(_tick(9, 30, 55, 99.0, 5)))
        bar = agg.on_tick(_tick(9, 31, 1, 101.0, 7))  # next minute → close 09:30 bar
        self.assertIsNotNone(bar)
        self.assertEqual((bar.start.hour, bar.start.minute), (9, 30))
        self.assertEqual(bar.open, 100.0)
        self.assertEqual(bar.high, 102.0)
        self.assertEqual(bar.low, 99.0)
        self.assertEqual(bar.close, 99.0)
        self.assertEqual(bar.volume, 35)
        self.assertEqual(bar.tick_count, 3)

    def test_session_vwap_accumulates_across_bars(self):
        agg = BarAggregator(60)
        agg.on_tick(_tick(9, 30, 5, 100.0, 10))
        agg.on_tick(_tick(9, 30, 40, 102.0, 20))
        bar1 = agg.on_tick(_tick(9, 31, 0, 104.0, 30))
        expected1 = (100 * 10 + 102 * 20 + 104 * 30) / 60  # includes the tick that closed bar1
        self.assertAlmostEqual(bar1.vwap, expected1, places=4)
        self.assertAlmostEqual(agg.session_vwap("005930"), expected1, places=4)

    def test_gap_skips_empty_intervals(self):
        agg = BarAggregator(60)
        agg.on_tick(_tick(9, 31, 10, 100.0, 1))
        bar = agg.on_tick(_tick(9, 34, 2, 105.0, 1))  # 2-minute gap
        self.assertEqual((bar.start.hour, bar.start.minute), (9, 31))
        bars_after = agg.force_close("005930")
        self.assertEqual((bars_after[0].start.hour, bars_after[0].start.minute), (9, 34))

    def test_stale_tick_never_rewrites_history(self):
        agg = BarAggregator(60)
        agg.on_tick(_tick(9, 31, 10, 100.0, 1))
        self.assertIsNone(agg.on_tick(_tick(9, 30, 59, 999.0, 1)))  # out-of-order
        bar = agg.on_tick(_tick(9, 32, 0, 101.0, 1))
        self.assertEqual(bar.high, 100.0)  # stale 999 never entered the bar

    def test_zero_volume_tick_moves_price_not_vwap(self):
        agg = BarAggregator(60)
        agg.on_tick(_tick(9, 30, 1, 100.0, 10))
        agg.on_tick(_tick(9, 30, 2, 200.0, 0))  # quote-ish print
        self.assertAlmostEqual(agg.session_vwap("005930"), 100.0)
        bar = agg.force_close("005930")[0]
        self.assertEqual(bar.high, 200.0)

    def test_reset_session_clears_state(self):
        agg = BarAggregator(60)
        agg.on_tick(_tick(9, 30, 1, 100.0, 10))
        agg.reset_session()
        self.assertIsNone(agg.session_vwap("005930"))
        self.assertEqual(agg.bars("005930"), [])


if __name__ == "__main__":
    unittest.main()
