"""Paper-only entry sizing, marked budget drawdown, and position lifecycle."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from execution.hl_trade import Market
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

    def test_paper_uses_desk_risk_and_marks_open_losses(self):
        now = 2_000_000_000

        def book(bid, ask):
            return {"time": int(fade.time.time() * 1000), "levels": [
                [{"px": str(bid), "sz": "1000"}], [{"px": str(ask), "sz": "1000"}],
            ]}

        signal = {"coin": "DOGE", "spike": True, "bar": now * 1000 - 900_000}
        daily = pd.DataFrame({"time": pd.date_range("2025-01-01", periods=30, freq="D", tz="UTC"),
                              "open": 1., "high": 1.05, "low": 0.95, "close": 1.})
        funding = pd.DataFrame({"time": pd.date_range("2025-01-01", periods=240, freq="h", tz="UTC"),
                                "fundingRate": 0.})
        market = Market("DOGE", "", 1, 1, 0, 4)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.json"
            with patch.object(fade.time, "time", return_value=now), \
                 patch.object(fade, "recent_signals", return_value=[signal]), \
                 patch.object(fade, "fetch_daily_candles", return_value=daily), \
                 patch.object(fade, "fetch_funding_history", return_value=funding), \
                 patch.object(fade, "fetch_markets", return_value={"DOGE": market}), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(1, 1.001)):
                opened = fade.paper_step(path, 250)
            self.assertEqual(opened["position"]["quantity"], 33)
            self.assertAlmostEqual(opened["position"]["risk_usd"], 33 * (0.30 + 2 * fade.FEE))
            state = json.loads(path.read_text())
            state["fraction"] = 0.10  # An existing position opened by the previous runner still exits normally.
            path.write_text(json.dumps(state))
            with patch.object(fade.time, "time", return_value=now + 60), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(2, 2.001)):
                marked = fade.paper_step(path, 250)
                self.assertLess(marked["notice"]["return"], -0.10)
                self.assertNotIn("notice", fade.paper_step(path, 250))
            state = json.loads(path.read_text())
            self.assertEqual(state["realized"], 0)
            self.assertTrue(state["below_notice"])
            self.assertIsNotNone(state["notice"])
            with patch.object(fade.time, "time", return_value=now + 14401), \
                 patch.object(fade, "post_info", side_effect=lambda *a, **k: book(2, 2.001)):
                covered = fade.paper_step(path, 250)
            self.assertEqual(covered["status"], "covered")
            self.assertEqual(json.loads(path.read_text())["realized"], covered["net_usd"])

    def test_state_budget_cannot_be_changed_silently(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(fade, "recent_signals", return_value=[]):
            path = Path(folder) / "paper.json"
            fade.paper_step(path, 250)
            with self.assertRaises(ValueError):
                fade.paper_step(path, 1000)
            with self.assertRaises(ValueError):
                fade.paper_step(path, 0)


if __name__ == "__main__":
    unittest.main()
