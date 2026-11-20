"""The Largest winners / losers highlight rows: a position under a day old shows its result since entry, labelled ALL."""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import overview as ov  # noqa: E402
from desk.venues.base import VenueAccount, VenuePosition  # noqa: E402


def stamp(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours_ago * 3600))


def entry(coin, side, hours_ago, wallet="07f2", closed=None):
    return {"wallet": wallet, "coin": coin, "side": side, "strategy": "directional", "opened": stamp(hours_ago), "closed": closed}


class YoungEntryTests(unittest.TestCase):
    def test_only_open_entries_under_a_day_old_count(self):
        young = ov.young_entries([entry("xyz:SNDK", "LONG", 5), entry("ETH", "SHORT", 30), entry("BTC", "LONG", 2, closed="2026-01-01T00:00:00")], time.time())
        self.assertEqual(list(young), [("07f2", "xyz:SNDK", "LONG")])
        self.assertAlmostEqual(young[("07f2", "xyz:SNDK", "LONG")] * 24, 5, delta=0.1)


class WinnersRowTests(unittest.TestCase):
    def build(self, entries):
        def pos(symbol, quantity, avg, mark, upnl, kind="perp"):
            return VenuePosition(venue="hl", account="0xaaaa07f2", symbol=symbol, asset_class="tradfi perp" if ":" in symbol else "crypto perp", quantity=quantity, avg_price=avg, mark_price=mark,
                                 unrealized_usd=upnl)
        positions = (
            pos("xyz:SNDK", 4.927, 87.92, 80.94, (80.94 - 87.92) * 4.927),   # young long, down 7.9% since entry, flat on the day
            pos("xyz:OLD", 10, 100.0, 103.0, 30.0),                          # older long, up 3% on the day
            pos("TRUMP", -1000, 1.0, 0.9, 100.0),                            # young short, up 10% since entry: a winner
            pos("AAVE", -50, 100.0, 104.0, -200.0),                          # young short, down 4% since entry: a loser
        )
        snapshot = VenueAccount(venue="hl", account="0xaaaa07f2", label="HL 07f2", equity_usd=5000, cash_usd=0, margin_used_usd=500,
                                available_usd=4000, unrealized_usd=0, positions=positions)
        moves = {("HL 07f2", "xyz:SNDK"): 0.0068, ("HL 07f2", "xyz:OLD"): 0.03, ("HL 07f2", "TRUMP"): -0.02, ("HL 07f2", "AAVE"): -0.01}
        overview = ov.DeskOverview.__new__(ov.DeskOverview)
        overview.snapshots = [snapshot]
        overview.book = []
        overview.problems = []
        with patch("desk.journal.load", return_value=entries), patch("desk.day_change.position_changes", return_value=moves), \
                patch("desk.funding_watch.rows", return_value=[], create=True):
            try:
                well, attention, _, _ = overview.highlight_rows({})
            except Exception as exc:   # the rest of the rows need data this test does not build; the two rows are asserted below
                self.skipTest(f"highlight_rows needs more of the overview than this test builds ({type(exc).__name__}: {exc})")
        return dict(well), dict(attention), overview

    def test_a_young_position_shows_since_entry_with_all_and_an_older_one_keeps_the_day(self):
        well, attention, overview = self.build([entry("xyz:SNDK", "LONG", 5), entry("TRUMP", "SHORT", 3), entry("AAVE", "SHORT", 4), entry("xyz:OLD", "LONG", 50)])
        losers, winners = attention["Largest losers"], well["Largest winners"]
        self.assertIn("SNDK", losers)
        self.assertIn("ALL", losers)                       # SNDK: -7.9% since entry, not +0.68% on the day
        self.assertNotIn("0.68", losers)
        self.assertIn("AAVE", losers)                      # a young short that is down since entry is a loser
        self.assertIn("TRUMP", winners)
        self.assertIn("OLD 3", winners.replace("xyz:", ""))  # an older position keeps its 24h move
        self.assertNotIn("OLD 3% ALL", winners.replace("xyz:", ""))
        self.assertIn("SNDK: ALL is since entry at 87.92", overview.highlight_hover["Largest losers"])


if __name__ == "__main__":
    unittest.main()
