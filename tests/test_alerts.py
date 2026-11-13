"""Daemon alerts reach the dashboards; a silent daemon is an alert of its own."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "execution")]

import hl_daemon  # noqa: E402
from desk import alerts  # noqa: E402


class AlertRowTests(unittest.TestCase):
    def test_current_alerts_become_rows_and_expired_ones_do_not(self):
        state = {"heartbeat": 1000.0, "alerts": [
            {"title": "ladder out of band: rebalance (...c248 BTC)", "text": "Position -0.04", "since": "2026-11-11T22:00:00"},
            {"title": "ladder stopped", "text": "closed", "since": "2026-11-11T20:00:00", "expires": 900.0}]}
        rows = alerts.rows(state, now=1100.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Ladder out of band: rebalance (...c248 BTC)")
        self.assertIn("since 2026-11-11 17:00 ET", rows[0][1])  # the daemon stamps UTC; the desk reads Eastern

    def test_a_silent_daemon_is_flagged(self):
        rows = alerts.rows({"heartbeat": 0.0, "alerts": []}, now=20 * 60.0)
        self.assertEqual(rows[0][0], "Trading daemon not reporting")
        self.assertEqual(alerts.rows(None), [])


class AlertRegistryTests(unittest.TestCase):
    def test_set_clear_and_masking(self):
        registry = hl_daemon.Alerts(path=None)
        registry.set("x", "ladder failing", "wallet 0x" + "ab" * 18 + "c248 error")
        state = registry.state()
        self.assertEqual(len(state["alerts"]), 1)
        self.assertNotIn("ab" * 18, state["alerts"][0]["text"])  # only the last four characters
        since = state["alerts"][0]["since"]
        registry.set("x", "ladder failing", "wallet 0x" + "ab" * 18 + "c248 error")
        self.assertEqual(registry.state()["alerts"][0]["since"], since)  # a repeat keeps its start
        registry.clear("x")
        self.assertEqual(registry.state()["alerts"], [])
        registry.set("y", "short", "lived", ttl=-1)
        self.assertEqual(registry.state()["alerts"], [])  # expired


class SectionWarningTests(unittest.TestCase):
    def test_a_section_says_which_accounts_are_missing(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        from desk.overview import DeskOverview

        overview = object.__new__(DeskOverview)
        overview.snapshots = [SimpleNamespace()]
        overview.problems = ["HL 07f2: TimeoutError"]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertTrue(overview._require())
        self.assertIn("NOT SHOWN", out.getvalue())
        self.assertIn("HL 07f2: TimeoutError", out.getvalue())
        overview.problems = []
        out = io.StringIO()
        with redirect_stdout(out):
            overview._require()
        self.assertEqual(out.getvalue(), "")


class EasternTimeTests(unittest.TestCase):
    def test_ladder_start_is_shown_in_eastern_time(self):
        from desk.market_making import eastern

        self.assertEqual(eastern("2026-11-11 22:24"), "2026-11-11 17:24 ET")   # standard time
        self.assertEqual(eastern("2026-07-01 22:24"), "2026-07-01 18:24 ET")   # daylight time
        self.assertEqual(eastern(""), "")


if __name__ == "__main__":
    unittest.main()
