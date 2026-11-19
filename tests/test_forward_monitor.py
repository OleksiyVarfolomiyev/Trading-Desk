"""Forward monitor (desk/forward_monitor.py): drift tests, statuses and attention rows."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import forward_monitor as fm  # noqa: E402

EXPECTED = fm.Expectation("x", 0.01, 0.02, 300, True, "test")


def item(live, expected=EXPECTED, **kw):
    return fm.Item("k", "Rule", "risk", "2026-11-15", (lambda s: expected) if expected is not None else None,
                   (lambda s: live) if live is not None else None, **kw)


class DriftTests(unittest.TestCase):
    def test_few_live_outcomes_get_no_verdict(self):
        row = fm.evaluate(fm.Sources(), [item([-0.5] * 5)])[0]
        self.assertEqual(row["Status"], "too few live (5)")
        self.assertEqual(fm.attention([row]), [])

    def test_live_far_below_history_is_drift(self):
        live = [-0.02 + 0.001 * (i % 3) for i in range(30)]
        row = fm.evaluate(fm.Sources(), [item(live)])[0]
        self.assertEqual(row["Status"], "live drift")
        self.assertTrue(fm.attention([row])[0][0].startswith("Live drift: Rule"))

    def test_live_in_line_is_not_flagged(self):
        live = [0.01 + (0.02 if i % 2 else -0.02) for i in range(40)]
        self.assertEqual(fm.evaluate(fm.Sources(), [item(live)])[0]["Status"], "in line")

    def test_cusum_catches_a_run_of_losses(self):
        self.assertTrue(fm.cusum_alarm([-0.03] * 12, 0.01, 0.02))
        self.assertFalse(fm.cusum_alarm([0.01, -0.01] * 10, 0.01, 0.02))

    def test_missing_history_and_forward_tests_are_flags(self):
        rows = fm.evaluate(fm.Sources(), [item([], expected=None), item(None)])
        self.assertEqual(rows[0]["Status"], "no historical test")
        titles = [title for title, _ in fm.attention(rows)]
        self.assertIn("No historical test", titles)
        self.assertIn("No forward test", titles)

    def test_switched_off_rule_is_neither_drift_nor_missing(self):
        row = fm.evaluate(fm.Sources(), [item([-1.0] * 50, off="off: failed")])[0]
        self.assertEqual(row["Status"], "off: failed")
        self.assertEqual(fm.attention([row]), [])

    def test_forward_only_item_needs_its_own_edge(self):
        row = fm.evaluate(fm.Sources(), [item([0.001, -0.002] * 20, expected=None, forward_only="by design")])[0]
        self.assertTrue(row["Status"].startswith("no edge live so far"))


class LiveStreamTests(unittest.TestCase):
    def test_trim_outcome_is_the_trimmed_coins_next_day_move_net_of_cost(self):
        log = ("2026-11-10 12:00:00 UTC auto trimmed shorts: BTC -4% in D: trim shorts. Trimmed shorts by 33%: AAA; BBB\n"
               "2026-11-10 12:00:00 UTC ALERT Auto: trimmed shorts: same event\n"
               "2026-11-12 12:00:00 UTC auto trimmed shorts: SP500 2% down. Trimmed 33% of the shorts: xyz:MSFT 0.04\n")
        start = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        prices = {("AAA", start): 10, ("AAA", start + timedelta(hours=24)): 11, ("BBB", start): 10,
                  ("BBB", start + timedelta(hours=24)): 10}
        later = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        prices.update({("xyz:MSFT", later): 100, ("xyz:MSFT", later + timedelta(hours=24)): 98})
        src = fm.Sources(log_text=log, price_at=lambda coin, at: prices.get((coin, at)), now=later + timedelta(days=2))
        outcomes = fm._forward_after_trims(r"^auto trimmed shorts")(src)
        self.assertEqual(len(outcomes), 2)
        self.assertAlmostEqual(outcomes[0], 0.05 - fm.TRADE_COST)
        self.assertAlmostEqual(outcomes[1], -0.02 - fm.TRADE_COST)

    def test_closed_trades_split_at_the_change_date(self):
        frame = pd.DataFrame({"time": ["2026-11-10 00:00:00+00:00", "2026-11-18 00:00:00+00:00"], "account": ["07f2", "07f2"],
                              "return": [0.1, -0.2]})
        src = fm.Sources(closed_crypto=frame)
        self.assertEqual(fm._closed_returns(lambda s: s.closed_crypto, "2026-11-17", True, "07f2")(src), [0.1])
        self.assertEqual(fm._closed_returns(lambda s: s.closed_crypto, "2026-11-17", False, "07f2")(src), [-0.2])


class PublishTests(unittest.TestCase):
    def test_tile_carries_attention_rows(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            result = {"items": fm.evaluate(fm.Sources(), [item([], expected=None)]), "attention": [["No historical test", "Rule."]]}
            fm.publish(result, push=False, directory=Path(folder))
            tile = json.loads((Path(folder) / "forward-monitor.json").read_text())
            self.assertEqual(tile["attention"], [["No historical test", "Rule."]])
            self.assertEqual(tile["title"], "Forward monitor")


if __name__ == "__main__":
    unittest.main()
