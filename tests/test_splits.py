"""Stock splits on tradfi perps (desk/splits.py): a split is put on one scale, a crash or a crypto fall is not."""

import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

os.environ["HL_DISABLE_CACHE"] = "1"   # the record of splits stays in memory
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "execution")]

from desk import splits  # noqa: E402


def frame(closes, start="2026-11-01"):
    times = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    closes = np.array(closes, dtype=float)
    return pd.DataFrame({"time": times, "open": closes, "high": closes * 1.03, "low": closes * 0.97, "close": closes})


# SNDK: daily closes 838, 783, 754 then 85.1 on the day of the 10-for-1 split.
SNDK_BEFORE = [900, 950, 848, 879, 815, 838, 783, 754]


class SnapTests(unittest.TestCase):
    def test_clean_ratios_snap_and_others_do_not(self):
        self.assertEqual(splits.snap(0.1047), 0.1)          # SNDK against its price 24h before (the stock moved too)
        self.assertEqual(splits.snap(0.1129), 0.1)          # against the last completed close
        self.assertEqual(splits.snap(0.5), 0.5)
        self.assertEqual(splits.snap(10.2), 10.0)           # a reverse split
        self.assertIsNone(splits.snap(0.55))                # a -45% crash is not a 2-for-1 split
        self.assertIsNone(splits.snap(0.7))
        self.assertIsNone(splits.snap(1.02))
        self.assertIsNone(splits.snap(0))

    def setUp(self):
        splits._MEMORY.clear()


class DetectAndApplyTests(unittest.TestCase):
    def setUp(self):
        splits._MEMORY.clear()

    def test_split_inside_the_candles_is_detected_and_history_rescaled(self):
        data = frame(SNDK_BEFORE + [85.1, 88.0, 86.5])
        found = splits.detect(data)
        self.assertEqual([(s.date, s.factor) for s in found], [("2026-11-09", 0.1)])
        out = splits.apply(data, "SNDK", "xyz")
        self.assertAlmostEqual(out["close"].iloc[0], 90.0)
        self.assertAlmostEqual(out["close"].iloc[7], 75.4)
        self.assertAlmostEqual(out["close"].iloc[8], 85.1)            # the days on the new scale are untouched
        returns = out["close"].pct_change().abs().dropna()
        self.assertLess(returns.max(), 0.15)                            # no -90% day left

    def test_a_split_seen_live_before_any_new_candle_is_recorded_and_applied_once(self):
        data = frame(SNDK_BEFORE)                                        # completed candles are all pre-split
        out = splits.apply(data, "SNDK", "xyz", mark=85.1)               # the mark is a tenth of the last close
        self.assertAlmostEqual(out["close"].iloc[-1], 75.4)
        self.assertEqual(splits.active("SNDK", "xyz").text, "1:10")
        again = splits.apply(out, "SNDK", "xyz", mark=85.1)              # a frame already adjusted is not adjusted twice
        self.assertAlmostEqual(again["close"].iloc[-1], 75.4)
        later = splits.apply(frame(SNDK_BEFORE), "SNDK", "xyz")          # a fresh fetch on a later call uses the record
        self.assertAlmostEqual(later["close"].iloc[-1], 75.4)

    def test_recorded_and_detected_are_the_same_split(self):
        splits.observe("SNDK", "xyz", 85.1, 812.78, today=pd.Timestamp("2026-11-09", tz="UTC"))   # recorded on the day
        data = frame(SNDK_BEFORE + [85.1], start="2026-11-01")           # later the candles show it too (2026-11-09)
        record = splits.recorded("SNDK", "xyz")
        self.assertEqual(len(record), 1)
        out = splits.apply(data, "SNDK", "xyz")
        self.assertAlmostEqual(out["close"].iloc[7], 75.4)               # adjusted once, not 0.1 x 0.1

    def test_crash_crypto_and_reverse_split(self):
        crash = frame([100, 101, 99, 55, 54])                            # -45% in a day
        self.assertEqual(splits.detect(crash), [])
        self.assertTrue(crash.equals(splits.apply(crash, "XYZ", "xyz")))
        halved = frame([100, 101, 99, 50, 49])                           # crypto: never touched, even on a clean -50%
        self.assertTrue(halved.equals(splits.apply(halved, "DOGE", "")))
        self.assertIsNone(splits.observe("DOGE", "", 5.0, 100.0))
        reverse = frame([4, 4.1, 3.9, 40, 41])
        out = splits.apply(reverse, "XYZ", "xyz")
        self.assertAlmostEqual(out["close"].iloc[0], 40.0)

    def test_two_splits(self):
        data = frame([800, 810, 400, 405, 40.5, 41])                     # 2-for-1 then 10-for-1
        self.assertEqual([s.factor for s in splits.detect(data)], [0.5, 0.1])
        out = splits.apply(data, "ABC", "xyz")
        self.assertAlmostEqual(out["close"].iloc[0], 40.0)
        self.assertAlmostEqual(out["close"].iloc[2], 40.0)

    def test_previous_day_price_is_put_on_the_new_scale(self):
        self.assertAlmostEqual(splits.adjusted_previous("SNDK", "xyz", 85.102, 812.78), 81.278)
        self.assertAlmostEqual(85.102 / splits.adjusted_previous("SNDK", "xyz", 85.102, 812.78) - 1, 0.047, places=3)
        self.assertEqual(splits.adjusted_previous("BTC", "", 50000.0, 100000.0), 100000.0)     # crypto untouched
        self.assertEqual(splits.adjusted_previous("AAPL", "xyz", 290.0, 300.0), 300.0)         # no split


class SizingAndContextTests(unittest.TestCase):
    def setUp(self):
        splits._MEMORY.clear()

    def test_sndk_sizes_on_a_normal_atr_after_a_split(self):
        import hl_trade
        from hl_trade import Market, size_trade

        data = frame(SNDK_BEFORE * 14)[:110]                              # a volatile series on the old scale
        data["time"] = pd.date_range("2026-07-01", periods=len(data), freq="D", tz="UTC")
        market = Market("xyz:SNDK", "xyz", 85.1, 85.1, 3, 10)
        funding = pd.DataFrame({"time": pd.date_range("2026-11-01", periods=400, freq="h", tz="UTC"), "fundingRate": 0.0000125})
        planned = size_trade("xyz:SNDK", "xyz", "LONG", market, data, funding, equity=10_000, risk_headroom=5_000,
                             available_margin=5_000, maker_fee_rate=0.0001, taker_fee_rate=0.0004)
        notional = planned.quantity * market.mark_price
        self.assertGreater(notional, 1_200)                               # not the 95%-of-price cap's ~$430
        self.assertLess(planned.risk_budget, 10_000 * 0.8 * 0.05 + 1e-6)

    def test_market_context_gets_the_adjusted_previous_price(self):
        from desk.hl import account_analytics

        context = {"markPx": "85.102", "prevDayPx": "812.78"}
        fixed = account_analytics._split_adjusted_context("xyz", "xyz:SNDK", context)
        self.assertAlmostEqual(float(fixed["prevDayPx"]), 81.278)
        self.assertAlmostEqual(float(fixed["markPx"]) / float(fixed["prevDayPx"]) - 1, 0.047, places=3)
        self.assertEqual(account_analytics._split_adjusted_context("", "BTC", {"markPx": "5", "prevDayPx": "10"})["prevDayPx"], "10")


if __name__ == "__main__":
    unittest.main()
