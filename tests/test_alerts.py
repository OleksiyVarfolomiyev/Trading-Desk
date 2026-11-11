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
        self.assertEqual(rows[0][0], "Desk alert: ladder out of band: rebalance (...c248 BTC)")
        self.assertIn("since 2026-11-11 22:00 UTC", rows[0][1])

    def test_a_silent_daemon_is_flagged(self):
        rows = alerts.rows({"heartbeat": 0.0, "alerts": []}, now=20 * 60.0)
        self.assertEqual(rows[0][0], "Desk alert: trading daemon not reporting")
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


if __name__ == "__main__":
    unittest.main()
