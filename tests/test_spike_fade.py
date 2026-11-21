"""Paper-only entry sizing, marked budget drawdown, and position lifecycle."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research import spike_fade as fade


class PaperBudgetTests(unittest.TestCase):
    def test_notice_fires_once_per_breach_and_rearms_on_recovery(self):
        state = {"budget": 250, "realized": 0, "below_notice": False}
        self.assertIsNone(fade.budget_notice(state, -24.99))
        notice = fade.budget_notice(state, -25)
        self.assertAlmostEqual(notice["return"], -0.10)
        self.assertIsNone(fade.budget_notice(state, -26))
        self.assertIsNone(fade.budget_notice(state, -24))
        self.assertIsNotNone(fade.budget_notice(state, -25))

    def test_paper_uses_fraction_of_budget_and_marks_open_losses(self):
        now = 2_000_000_000

        def book(bid, ask):
            return {"time": int(fade.time.time() * 1000), "levels": [
                [{"px": str(bid), "sz": "1000"}], [{"px": str(ask), "sz": "1000"}],
            ]}

        signal = {"coin": "DOGE", "spike": True, "bar": now * 1000 - 900_000}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.json"
            with patch.object(fade.time, "time", return_value=now), \
                 patch.object(fade, "recent_signals", return_value=[signal]), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(1, 1.001)):
                opened = fade.paper_step(path, 250, 0.10)
            self.assertEqual(opened["position"]["quantity"], 25)
            with patch.object(fade.time, "time", return_value=now + 60), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(2, 2.001)):
                marked = fade.paper_step(path, 250, 0.10)
                self.assertLess(marked["notice"]["return"], -0.10)
                self.assertNotIn("notice", fade.paper_step(path, 250, 0.10))
            state = json.loads(path.read_text())
            self.assertEqual(state["realized"], 0)
            self.assertTrue(state["below_notice"])
            self.assertIsNotNone(state["notice"])
            with patch.object(fade.time, "time", return_value=now + 14401), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(2, 2.001)):
                covered = fade.paper_step(path, 250, 0.10)
            self.assertEqual(covered["status"], "covered")
            self.assertEqual(json.loads(path.read_text())["realized"], covered["net_usd"])

    def test_state_budget_and_fraction_cannot_be_changed_silently(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(fade, "recent_signals", return_value=[]):
            path = Path(folder) / "paper.json"
            fade.paper_step(path, 250, 0.20)
            with self.assertRaises(ValueError):
                fade.paper_step(path, 250, 0.10)
            with self.assertRaises(ValueError):
                fade.paper_step(path, 1000, 0.20)
            with self.assertRaises(ValueError):
                fade.paper_step(path, 250, 0.25)


if __name__ == "__main__":
    unittest.main()
