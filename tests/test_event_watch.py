"""Event watch: VVV's monthly burn is spotted over the trickle, announced ahead, and debriefed after."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from desk import event_watch as ew


class SupplyTests(unittest.TestCase):
    def history(self):
        days = {f"2026-11-{day:02d}": 33_000_000.0 + 1_000 * day for day in range(1, 10)}  # 1,000 VVV a day
        days["2026-11-09"] += 21_657                                                     # the monthly burn
        return {"VVV burned": days}

    def test_the_monthly_burn_stands_out_from_the_trickle(self):
        history = self.history()
        burned = ew.day_change(history, "VVV burned", "2026-11-09")
        usual = ew.usual_burn(history, "VVV burned", "2026-11-09")
        self.assertAlmostEqual(burned, 22_657)
        self.assertAlmostEqual(usual, 1_000)
        self.assertTrue(ew.is_discretionary_burn(burned, usual, price=21.0))          # $476K, 22x the trickle
        self.assertFalse(ew.is_discretionary_burn(1_000, usual, price=21.0))          # the trickle
        self.assertFalse(ew.is_discretionary_burn(22_657, usual, price=2.0))          # under $100K

    def test_the_heads_up_comes_three_days_before_the_expected_burn(self):
        burns = [("2026-10-09", 416_000.0), ("2026-11-09", 458_000.0)]
        self.assertEqual(ew.next_burn(burns), date(2026, 12, 9))
        title, text = ew.heads_up(burns, date(2026, 12, 6))
        self.assertEqual(title, "Burn approaching")
        self.assertIn("expected in 3 days (~Dec 9); last Nov 9, $458K", text)
        self.assertIn("tomorrow", ew.heads_up(burns, date(2026, 12, 8))[1])
        self.assertIsNone(ew.heads_up(burns, date(2026, 12, 1)))
        self.assertIsNone(ew.heads_up(burns, date(2026, 12, 12)))
        self.assertEqual(ew.next_burn([("2027-01-31", 1.0)]), date(2027, 2, 28))


class DebriefTests(unittest.TestCase):
    def test_outcomes_fill_at_each_horizon_and_a_burn_gets_its_debrief(self):
        meta = {"universe": [{"name": "BTC"}, {"name": "VVV"}, {"name": "SOL"}, {"name": "DOGE"}]}
        contexts = [{"markPx": "100", "dayNtlVlm": "9"}, {"markPx": "20", "dayNtlVlm": "5"},
                    {"markPx": "10", "dayNtlVlm": "8"}, {"markPx": "1", "dayNtlVlm": "7"}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            t0 = 1_794_200_000.0
            ew.record("burn", {"coin": "VVV", "day": "2026-11-09", "usd": 458_000}, ew.prices(meta, contexts, ["VVV"]),
                      path=path, now=t0)
            events = ew.load_events(path)
            marks = {"BTC": 101.0, "VVV": 21.0, "SOL": 10.0, "DOGE": 0.98}
            self.assertEqual(ew.due_outcomes(events, marks, t0 + 3600), [])           # not a day yet
            filled = ew.due_outcomes(events, marks, t0 + 86400 + 60)
            self.assertEqual([horizon for _, horizon in filled], ["1d"])
            ew.save_events(events, path)
            result = ew.load_events(path)[0]["outcomes"]["1d"]
        self.assertAlmostEqual(result["coin"], 0.05)
        self.assertAlmostEqual(result["alts"], (0.05 + 0.0 - 0.02) / 3)               # VVV, SOL, DOGE equal weight
        title, text = ew.debrief(events[0], "1d")
        self.assertEqual(title, "Burn debrief")
        self.assertTrue(text.startswith("VVV 5% 1d after the Nov "))
        self.assertIsNone(ew.debrief({**events[0], "kind": "alt rally"}, "1d"))       # logged only


if __name__ == "__main__":
    unittest.main()
