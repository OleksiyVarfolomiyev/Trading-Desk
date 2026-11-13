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
        self.assertEqual(text, "XYZ long (HL 07f2) -100.0% APR, MELANIA short (HL c248) -67.8% APR.")
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
