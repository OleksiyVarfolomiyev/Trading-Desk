"""Drawdown by period, the tolerance alert and the quarterly report (desk/drawdown.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import drawdown as dd  # noqa: E402


def series(values, start="2026-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"), dtype=float)


class MeasureTests(unittest.TestCase):
    def test_desk_return_weights_books_by_equity(self):
        days = pd.date_range("2026-01-01", periods=2, freq="D")
        returns = {"crypto": pd.Series([0.10, 0.0], index=days), "stocks": pd.Series([-0.10, 0.0], index=days)}
        equity = {"crypto": pd.Series([300.0, 300.0], index=days), "stocks": pd.Series([100.0, 100.0], index=days)}
        out = dd.desk_returns(returns, equity)
        self.assertAlmostEqual(out.iloc[0], (300 * 0.10 - 100 * 0.10) / 400)

    def test_common_window_cuts_and_rebases(self):
        levels = {"desk": series([1, 1.1, 1.2, 1.3]), "crypto": series([2, 2.2, 2.4], start="2026-01-02")}
        out = dd.common_window(levels)
        self.assertEqual(out["desk"].index[0], pd.Timestamp("2026-01-02"))
        self.assertEqual(out["desk"].index[-1], pd.Timestamp("2026-01-04"))
        self.assertAlmostEqual(out["crypto"].iloc[0], 1.0)

    def test_period_stats_peak_to_trough_from_close_before(self):
        levels = series([1.0, 1.2, 0.9, 1.0, 1.1])
        stats = dd.period_stats(levels, pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-05"))
        self.assertAlmostEqual(stats["Return"], 0.1)          # from the close before (1.0) to 1.1
        self.assertAlmostEqual(stats["Max DD"], 0.9 / 1.2 - 1)
        self.assertAlmostEqual(stats["DD at end"], 1.1 / 1.2 - 1)
        self.assertTrue(stats["Status"].startswith("under water since"))

    def test_no_zero_drawdown_figures(self):
        stats = dd.period_stats(series([1.0, 1.1, 1.2]), pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03"))
        self.assertIsNone(stats["Max DD"])
        self.assertIsNone(stats["DD at end"])
        self.assertIsNone(stats["Return / max DD"])
        self.assertEqual(stats["Status"], "at high")

    def test_period_table_has_desk_and_books_for_the_same_windows(self):
        idx = pd.date_range("2025-11-20", "2026-02-15", freq="D")
        levels = {name: pd.Series(1 + 0.001 * pd.Series(range(len(idx))).values * k, index=idx)
                  for name, k in (("desk", 1), ("crypto", 2), ("stocks", -1))}
        rows = dd.period_table(levels)
        by_period = {}
        for row in rows:
            by_period.setdefault(row["Period"], set()).add(row["Book"])
        self.assertTrue(all(books == {"desk", "crypto", "stocks"} for books in by_period.values()))
        self.assertIn("Since Nov 20 '25", by_period)


class ToleranceTests(unittest.TestCase):
    def test_tolerance_empty_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f.json"
            path.write_text(json.dumps({"drawdown_tolerance": None}))
            self.assertIsNone(dd.tolerance(path))
            path.write_text(json.dumps({"drawdown_tolerance": 20}))
            self.assertAlmostEqual(dd.tolerance(path), 0.2)
        self.assertIsNone(json.loads(dd.CONFIG.read_text())["drawdown_tolerance"])   # no number is invented

    def test_alert_levels(self):
        self.assertIsNone(dd.alert_level(-0.10, None))
        self.assertIsNone(dd.alert_level(-0.10, 0.20))
        self.assertEqual(dd.alert_level(-0.15, 0.20), "warn")
        self.assertEqual(dd.alert_level(-0.21, 0.20), "breach")

    def test_alerts_and_push_once_per_level(self):
        levels = {"desk": series([1.0, 1.0, 0.84])}
        rows = dd.alerts(levels, 0.20)
        self.assertEqual(rows[0][0], "Drawdown nearing tolerance")
        sent = []
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "s.json"
            self.assertEqual(dd.notify(levels, 0.20, state, send=lambda t, x: sent.append(t)), "warn")
            self.assertIsNone(dd.notify(levels, 0.20, state, send=lambda t, x: sent.append(t)))   # same level: no repeat
        self.assertEqual(len(sent), 1)
        self.assertEqual(dd.alerts(levels, None), [])


class ReportTests(unittest.TestCase):
    def test_quarter_report_and_due(self):
        idx = pd.date_range("2025-11-20", "2026-11-18", freq="D")
        payload = {"series": {"crypto": [[d.strftime("%Y-%m-%d"), 0.001 * i] for i, d in enumerate(idx)],
                              "stocks": [[d.strftime("%Y-%m-%d"), -0.0005 * i] for i, d in enumerate(idx)]},
                   "desk_series": [[d.strftime("%Y-%m-%d"), 0.0002 * i] for i, d in enumerate(idx)]}
        with tempfile.TemporaryDirectory() as folder:
            books = Path(folder) / "books.json"
            books.write_text(json.dumps(payload))
            out = Path(folder) / "reports"
            today = pd.Timestamp("2026-11-19")
            self.assertEqual(dd.report_due(today, out), ["Q", "Y"])
            target = dd.write_report("Q", today, books, out)
            self.assertEqual(target.name, "2026Q3.md")
            text = target.read_text()
            self.assertIn("| desk |", text)
            self.assertIn("tolerance", text.lower())
            self.assertEqual(dd.report_due(today, out), ["Y"])


class MonitorTests(unittest.TestCase):
    def test_tolerance_alert_is_monitoring_not_a_rule(self):
        from desk import forward_monitor as fm

        item = next(i for i in fm.REGISTRY if i.key == "dd-tolerance")
        rows = fm.evaluate(fm.Sources(), [item])
        self.assertEqual(rows[0]["Status"], "monitoring, alert only")
        self.assertEqual(fm.attention(rows), [])


if __name__ == "__main__":
    unittest.main()
