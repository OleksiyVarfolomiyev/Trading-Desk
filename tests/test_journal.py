"""The trade journal tags positions by strategy; results and the history warnings follow from it."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("DESK_JOURNAL_FILE", os.path.join(tempfile.mkdtemp(prefix="desk-journal-"), "trade_journal.json"))

from desk import journal, trade_rules  # noqa: E402

WALLET = "0x" + "ab" * 18 + "07f2"


class JournalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "journal.json"

    def test_one_open_entry_per_wallet_and_coin_with_only_the_last_four(self):
        journal.record_entry(WALLET, "ICP", "short", "directional", opened="2026-11-01T00:00:00", path=self.path)
        journal.record_entry(WALLET, "ICP", "short", "conviction", path=self.path)   # re-tagged, same entry
        entries = journal.load(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["wallet"], entries[0]["strategy"], entries[0]["opened"]), ("07f2", "conviction", "2026-11-01T00:00:00"))
        self.assertNotIn("ab" * 18, self.path.read_text())

    def test_closing_and_reopening_makes_a_new_entry(self):
        journal.record_entry(WALLET, "ICP", "short", "directional", opened="2026-11-01T00:00:00", path=self.path)
        self.assertEqual(journal.close_missing({("07f2", "VVV")}, path=self.path), 1)
        journal.record_entry(WALLET, "ICP", "long", "hedge", path=self.path)
        self.assertEqual([item["strategy"] for item in journal.load(self.path)], ["directional", "hedge"])

    def test_results_split_by_strategy(self):
        opened = "2026-11-01T00:00:00"
        entries = [{"wallet": "07f2", "coin": "VVV", "side": "SHORT", "strategy": "hedge", "opened": opened, "closed": None},
                   {"wallet": "07f2", "coin": "ICP", "side": "SHORT", "strategy": "directional", "opened": opened, "closed": None}]
        at = (time.time() - 3600) * 1000
        fills = {WALLET: [
            {"coin": "ICP", "px": "2.5", "sz": "100", "closedPnl": "20", "fee": "1", "time": at, "cloid": None},
            # the ladder buys back 2 VVV at 20 (HL books 3 of the short's profit); VVV is 19 now
            {"coin": "VVV", "side": "B", "px": "20", "sz": "2", "closedPnl": "3", "fee": "0.1", "time": at, "cloid": "0x1add" + "0" * 28},
            {"coin": "OLD", "side": "A", "px": "1", "sz": "10", "closedPnl": "-5", "fee": "0.2", "time": at, "cloid": None},
            {"coin": "@107", "side": "A", "px": "70", "sz": "1", "closedPnl": "9", "fee": "0", "time": at}]}
        positions = {WALLET: [{"coin": "VVV", "szi": "-100", "unrealizedPnl": "12", "positionValue": "1900"},
                              {"coin": "ICP", "szi": "-50", "unrealizedPnl": "-7", "positionValue": "120"}]}
        frame = journal.split_by_strategy(entries, fills, positions).set_index("Strategy")
        self.assertEqual(frame.loc["directional", "Realized PnL (30d)"], 19.0)
        self.assertEqual(frame.loc["directional", "Unrealized PnL"], -7.0)
        self.assertEqual(frame.loc["hedge", "Unrealized PnL"], 12.0)             # the position is the hedge's...
        # ...the ladder only gets the fill against holding: bought 2 at 20, worth 19 now, less the fee
        self.assertAlmostEqual(frame.loc["market making", "Realized PnL (30d)"], -2.1)
        self.assertAlmostEqual(frame.loc["hedge", "Realized PnL (30d)"], 5.0)    # the short's profit stays with the hedge
        self.assertAlmostEqual(frame.loc["market making", "Fees (30d)"], -0.1)  # paid: negative
        self.assertAlmostEqual(frame["Realized PnL (30d)"].sum(), 19.0 + 2.9 - 5.2)  # still HL's realized total
        self.assertAlmostEqual(frame.loc[journal.UNTAGGED, "Realized PnL (30d)"], -5.2)
        self.assertEqual(int(frame["Fills (30d)"].sum()), 3)                      # spot is left out

    def test_only_directional_trades_come_up_for_review(self):
        old = "2026-01-01T00:00:00"
        entries = [{"wallet": "07f2", "coin": "ICP", "strategy": "directional", "opened": old, "closed": None},
                   {"wallet": "07f2", "coin": "VVV", "strategy": "hedge", "opened": old, "closed": None},
                   {"wallet": "07f2", "coin": "GONE", "strategy": "directional", "opened": old, "closed": None}]
        due = journal.overdue(entries, {("07f2", "ICP"), ("07f2", "VVV")})
        self.assertEqual([item["coin"] for item in due], ["ICP"])


class TradeRuleTests(unittest.TestCase):
    def test_warnings_follow_the_history(self):
        self.assertEqual(trade_rules.entry_notes("ICP", "SHORT", 900.0), [])
        self.assertEqual(len(trade_rules.entry_notes("BTC", "LONG", 2_000.0)), 3)     # large, long, a major
        self.assertEqual(len(trade_rules.entry_notes("BTC", "LONG", 2_000.0, "hedge")), 0)   # a hedge is a different decision
        notes = trade_rules.entry_notes("ICP", "SHORT", 2_000.0, "conviction")
        self.assertEqual(len(notes), 1)
        self.assertIn("above $1,500", notes[0])


if __name__ == "__main__":
    unittest.main()
