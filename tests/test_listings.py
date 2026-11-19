"""Borrowing an underlying's history for a market that is new on Hyperliquid (desk/listings.py)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import degraded, listings  # noqa: E402


def series(start, values):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D", tz="UTC"), dtype=float)


class ListingTests(unittest.TestCase):
    def setUp(self):
        degraded.clear()

    def test_map_entries_resolve_with_or_without_dex_prefix(self):
        self.assertEqual(listings.listing_for("xyz:RELIANCE"), "RELIANCE.NS")
        self.assertEqual(listings.listing_for("PAXG"), "PAXG-USD")
        self.assertIsNone(listings.listing_for("xyz:JIO"))   # no long history to borrow
        self.assertIsNone(listings.listing_for("xyz:AAPL"))

    def test_history_is_spliced_scaled_to_meet_the_market(self):
        own = series("2026-11-10", [200.0, 202.0, 204.0])
        listed = series("2026-11-01", [2.0, 2.0, 2.2, 2.2, 2.4, 2.4, 2.6, 2.6, 2.8, 2.8, 3.0, 3.0])
        with mock.patch("desk.live_risk.yahoo_stock_closes", return_value=listed):
            out = listings.spliced("xyz:RELIANCE", own, 60)
        self.assertEqual(len(out), 9 + 3)
        self.assertEqual(list(out.iloc[-3:]), [200.0, 202.0, 204.0])   # the market's own prices stay
        before = out[out.index < own.index[0]]
        self.assertAlmostEqual(before.iloc[-1], 2.8 * 200.0 / 2.8)       # scaled so the listing's close on the first candle's day is the candle
        self.assertAlmostEqual(before.pct_change().iloc[2], listed.pct_change().iloc[2])   # returns unchanged by scaling

    def test_unmapped_market_is_left_alone(self):
        own = series("2026-11-10", [1.0, 2.0])
        self.assertIs(listings.spliced("xyz:JIO", own, 60), own)

    def test_yahoo_failure_is_reported_and_own_history_kept(self):
        own = series("2026-11-10", [1.0, 2.0])
        with mock.patch("desk.live_risk.yahoo_stock_closes", side_effect=RuntimeError("HTTP 429")):
            out = listings.spliced("PAXG", own, 60)
        self.assertIs(out, own)
        self.assertTrue(any("PAXG" in row["name"] for row in degraded.rows()))


if __name__ == "__main__":
    unittest.main()
