"""Adverse funding: the highlights row and the sustained-rate test (desk/funding_watch.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import funding_watch  # noqa: E402


class AttentionRowTests(unittest.TestCase):
    def test_flags_payers_past_the_threshold_worst_first(self):
        title, text = funding_watch.attention_row([
            ("MELANIA short (HL c248)", -0.678, -1233.0),
            ("ICP short (HL 07f2)", 0.11, -1779.0),           # earns
            ("XYZ long (HL 07f2)", -1.0, 2000.0),            # pays more a day
            ("INTC long (HL 07f2)", -0.06, 246.0),           # under the threshold
        ])
        self.assertEqual(title, "Adverse funding")
        self.assertEqual(text, "XYZ long (HL 07f2) -100% APR, MELANIA short (HL c248) -68% APR.")
        self.assertNotIn("ICP", text)
        self.assertNotIn("INTC", text)

    def test_small_positions_are_not_flagged(self):
        self.assertIsNone(funding_watch.attention_row([("TINY long (HL 07f2)", -0.8, 50.0)]))  # $0.11/day

    def test_nan_funding_is_ignored(self):
        self.assertIsNone(funding_watch.attention_row([("X long (HL 07f2)", float("nan"), 5000.0)]))


class SustainedTests(unittest.TestCase):
    def test_needs_six_adverse_hours(self):
        self.assertIsNone(funding_watch.sustained([0.0001] * 5, 1))
        self.assertAlmostEqual(funding_watch.sustained([0.0001] * 6, 1), -0.876)
        self.assertIsNone(funding_watch.sustained([0.0001] * 6, -1))  # a short earns it


if __name__ == "__main__":
    unittest.main()


class ConsolidateTests(unittest.TestCase):
    DAEMON_BERA = ("Funding", "...07f2 BERA short -70.7% avg APR over 6 hrs (since 2026-11-13 10:00 ET)")
    DAEMON_MELANIA = ("Funding", "...c248 MELANIA short -119.7% avg APR over 6 hrs (since 2026-11-13 09:00 ET)")

    def test_daemon_alerts_and_the_current_rate_row_become_one_row(self):
        rows = [("Margin", "m"), self.DAEMON_BERA, self.DAEMON_MELANIA, ("Concentration", "c"),
                (funding_watch.TITLE, "BERA short (07f2) -57.0% APR, xyz:TSLA long (07f2) -40.0% APR.")]
        self.assertEqual(funding_watch.consolidate(rows), [
            ("Margin", "m"),
            ("Funding", "MELANIA short (c248) -120%, BERA short (07f2) -71% avg APR over 6 hrs; TSLA long (07f2) -40% APR now."),
            ("Concentration", "c")])

    def test_current_rate_only_and_nothing_to_merge(self):
        self.assertEqual(funding_watch.consolidate([(funding_watch.TITLE, "BERA short (HL 07f2) -57.0% APR.")]),
                         [("Funding", "BERA short (07f2) -57% APR now.")])
        rows = [("Concentration", "c"), ("Funding", "some other text")]
        self.assertEqual(funding_watch.consolidate(rows), rows)   # an unknown shape is left alone
