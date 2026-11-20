"""Drawdown by period, the tolerance alert and the quarterly report (desk/drawdown.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import drawdown as dd  # noqa: E402
from desk import web_sync  # noqa: E402


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

    def test_short_window_has_no_year_rows_and_names_its_inception(self):
        idx = pd.date_range("2026-10-15", "2026-11-18", freq="D")
        levels = {"desk": pd.Series(1 + 0.001 * pd.Series(range(len(idx))).values, index=idx)}
        labels = [row["Period"] for row in dd.period_table(levels, inception=pd.Timestamp("2026-10-16"))]
        self.assertIn("Since Oct 16 '26", labels)
        self.assertNotIn("2026", labels)


class EraTests(unittest.TestCase):
    def books(self, folder):
        idx = pd.date_range("2025-01-14", "2026-11-18", freq="D")
        # stocks fall through 2026-10 (passive era includes a drawdown), then recover inside the managed era
        stocks = [[d.strftime("%Y-%m-%d"), (-0.0004 * i if d < pd.Timestamp("2026-10-16") else -0.0004 * 640 + 0.001 * (i - 640))]
                  for i, d in enumerate(idx)]
        crypto = [[d.strftime("%Y-%m-%d"), 0.001 * i] for i, d in enumerate(idx) if d >= pd.Timestamp("2025-11-20")]
        desk = [[d.strftime("%Y-%m-%d"), 0.0002 * i] for i, d in enumerate(idx)]
        path = Path(folder) / "books.json"
        path.write_text(json.dumps({"series": {"crypto": crypto, "stocks": stocks}, "desk_series": desk}))
        return path

    def test_managed_since_is_the_repo_start(self):
        self.assertEqual(dd.managed_since(), pd.Timestamp("2026-10-16"))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(dd.managed_since(Path(folder) / "none.json"), pd.Timestamp(dd.MANAGED_SINCE))

    def test_managed_levels_start_at_the_close_before_the_repo_start(self):
        with tempfile.TemporaryDirectory() as folder:
            levels = dd.load_levels(self.books(folder))
        for series in levels.values():
            self.assertEqual(series.index[0], pd.Timestamp("2026-10-15"))
            self.assertAlmostEqual(series.iloc[0], 1.0)

    def test_passive_table_puts_stocks_beside_spy_over_the_same_days(self):
        spy_idx = pd.date_range("2025-01-14", "2026-12-01", freq="D")
        spy = pd.Series(range(100, 100 + len(spy_idx)), index=spy_idx, dtype=float)
        with tempfile.TemporaryDirectory() as folder:
            rows = dd.passive_table(self.books(folder), spy=spy)
        self.assertEqual([r["Held"] for r in rows][:2], ["Stocks (IB, long only)", "SPY, same days"])
        self.assertEqual(rows[0]["To"], rows[1]["To"])
        self.assertEqual(rows[0]["From"], rows[1]["From"])
        self.assertEqual(rows[0]["To"], "Oct 15 '26")   # nothing from the managed era

    def test_alert_uses_the_managed_desk_only(self):
        rows = dd.alerts({"desk": series([1.0, 1.0, 0.84])}, 0.20)
        self.assertEqual(len(rows), 1)
        self.assertIn("Managed desk", rows[0][1])


class ToleranceTests(unittest.TestCase):
    def test_tolerance_parses_and_the_repo_value_is_the_users(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f.json"
            path.write_text(json.dumps({"drawdown_tolerance": None}))
            self.assertIsNone(dd.tolerance(path))
            path.write_text(json.dumps({"drawdown_tolerance": 20}))
            self.assertAlmostEqual(dd.tolerance(path), 0.2)
        self.assertEqual(json.loads(dd.CONFIG.read_text())["drawdown_tolerance"], 0.10)   # set by the user 2026-11-19: the desk's drawdown over the year

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
    def books(self, folder):
        idx = pd.date_range("2025-11-20", "2026-11-18", freq="D")
        payload = {"series": {"crypto": [[d.strftime("%Y-%m-%d"), 0.001 * i] for i, d in enumerate(idx)],
                              "stocks": [[d.strftime("%Y-%m-%d"), -0.0005 * i] for i, d in enumerate(idx)]},
                   "desk_series": [[d.strftime("%Y-%m-%d"), 0.0002 * i] for i, d in enumerate(idx)]}
        books = Path(folder) / "books.json"
        books.write_text(json.dumps(payload))
        return books

    def test_passive_quarter_report_is_labelled_and_has_no_tolerance(self):
        spy_idx = pd.date_range("2025-01-01", "2026-12-01", freq="D")
        spy = pd.Series(range(100, 100 + len(spy_idx)), index=spy_idx, dtype=float)
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            out = Path(folder) / "reports"
            today = pd.Timestamp("2026-11-19")
            self.assertEqual(dd.report_due(today, out), ["Q", "Y"])
            target = dd.write_report("Q", today, books, out, spy=spy)
            self.assertEqual(target.name, "2026Q3.md")
            text = target.read_text()
            self.assertIn("passive era", text)
            self.assertIn("SPY, same days", text)
            self.assertNotIn("tolerance", text.lower().replace("no drawdown tolerance", ""))
            self.assertEqual(dd.report_due(today, out), ["Y"])

    def test_managed_quarter_report_has_the_desk_and_tolerance(self):
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            out = Path(folder) / "reports"
            target = dd.write_report("Q", pd.Timestamp("2027-01-05"), books, out)
            text = target.read_text() if target else ""
        self.assertIn("managed desk", text)
        self.assertIn("| desk |", text)
        self.assertIn("tolerance", text.lower())


    def test_report_carries_all_four_excess_return_columns_when_the_window_matches(self):
        parts = {"Picks": 0.0123, "Sizing": -0.004, "Timing": 0.0, "Market exposure": 0.05, "Fees & funding": -0.0002, "Unattributed": 0.0}
        column = lambda excess: {"parts": parts, "portfolio": 1.0 + excess, "index": 1.0, "start": "2026-10-16", "end": "2026-12-31"}
        regimes = {"Q": {"all": {"sessions": 50, "start": "2026-10-16", "end": "2026-12-31", "comparisons": {
            "crypto": column(0.0583), "stocks": column(-0.02), "desk_btc": column(0.03), "desk_spy": column(0.04)}}}}
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            payload = json.loads(books.read_text())
            payload["regimes"] = regimes
            books.write_text(json.dumps(payload))
            with mock.patch.object(web_sync, "BOOKS_FILE", books):   # the drivers are read from the stored books file
                text = dd.write_report("Q", pd.Timestamp("2027-01-05"), books, Path(folder) / "reports").read_text()
        self.assertIn("## What drove excess return (Q window, 2026-10-16 to 2026-12-31)", text)
        self.assertIn("| Driver | Crypto vs BTC | Stocks vs SPY | Desk vs BTC | Desk vs SPY |", text)
        self.assertIn("| Picks | 1.2 pp | 1.2 pp | 1.2 pp | 1.2 pp |", text)
        self.assertIn("| Timing |  |  |  |  |", text)   # a zero is blank, not 0.0
        self.assertIn("| Excess return | 5.8 pp | -2.0 pp | 3.0 pp | 4.0 pp |", text)

    def test_report_leaves_out_drivers_from_a_window_that_does_not_match_the_period(self):
        regimes = {"Q": {"all": {"start": "2026-07-01", "end": "2026-09-30", "comparisons": {}}}}
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            payload = json.loads(books.read_text())
            payload["regimes"] = regimes
            books.write_text(json.dumps(payload))
            with mock.patch.object(web_sync, "BOOKS_FILE", books):
                text = dd.write_report("Q", pd.Timestamp("2027-01-05"), books, Path(folder) / "reports").read_text()
        self.assertIn("does not match this period", text)
        self.assertNotIn("| Driver |", text)


class EraTests(unittest.TestCase):
    """The page's two analytics choices: since active management and since inception."""

    def books(self, folder):
        idx = pd.date_range("2025-11-20", "2026-11-18", freq="D")
        payload = {"series": {"crypto": [[d.strftime("%Y-%m-%d"), 0.001 * i] for i, d in enumerate(idx)],
                              "stocks": [[d.strftime("%Y-%m-%d"), -0.0005 * i] for i, d in enumerate(idx)]},
                   "desk_series": [[d.strftime("%Y-%m-%d"), 0.0002 * i] for i, d in enumerate(idx)]}
        books = Path(folder) / "books.json"
        books.write_text(json.dumps(payload))
        return books

    def test_active_window_counts_days_since_the_managed_start(self):
        windows = {"W": 7, "M": 30, "All": None}
        out = dd.with_active_window(windows, pd.Timestamp("2026-11-18", tz="UTC"))
        self.assertEqual(out["Active"], 34)   # Oct 16 .. Nov 18 inclusive
        self.assertEqual({k: v for k, v in out.items() if k != "Active"}, windows)
        self.assertNotIn("Active", dd.with_active_window(windows, pd.Timestamp("2026-10-16")))   # not yet two days long

    def test_inception_levels_start_with_the_passive_months(self):
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            everything = dd.load_levels(books, era="all")
            managed = dd.load_levels(books)
        self.assertEqual(everything["desk"].index[0], pd.Timestamp("2025-11-20"))
        self.assertEqual(managed["desk"].index[0], pd.Timestamp("2026-10-15"))
        self.assertGreater(len(everything["desk"]), len(managed["desk"]))

    def test_alert_rows_carry_the_era_and_say_the_desk_includes_passive_stocks(self):
        from desk import tile_feed

        levels = {"desk": series([1.0, 1.2, 1.05])}   # 12.5% under its high, past a 10% tolerance
        rows = dd.alerts(levels, 0.10, "Desk since inception")
        self.assertEqual(rows[0][0], "Drawdown past tolerance")
        self.assertIn("passive IB stocks", rows[0][1])
        self.assertIn("Desk since inception", rows[0][1])
        with tempfile.TemporaryDirectory() as folder:
            tile_feed.publish(pd.DataFrame([{"Period": "x", "Return": 0.1}]), "t", key="k", push=False, directory=Path(folder),
                              attention=rows, era="inception")
            tile = json.loads((Path(folder) / "k.json").read_text())
        self.assertEqual(tile["era"], "inception")
        self.assertEqual(tile["attention"][0][2], "inception")

    def test_underwater_chart_marks_the_tolerance(self):
        figure = dd.underwater_figure({"desk": series([1.0, 1.1, 1.0])}, 0.10)
        self.assertAlmostEqual(figure.layout.shapes[0].y0, -0.10)
        self.assertEqual(len(dd.underwater_figure({"desk": series([1.0, 1.1, 1.0])}).layout.shapes), 0)

    def test_publish_makes_both_eras_tiles_with_their_own_alert_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            books = self.books(folder)
            tiles = Path(folder) / "tiles"
            tiles.mkdir()
            keys = dd.publish(push=False, directory=tiles, path=books, spy=pd.Series(dtype=float))
            eras = {json.loads(f.read_text()).get("era") for f in tiles.glob("drawdown-*.json")}
        self.assertTrue({"drawdown-periods", "drawdown-underwater", "drawdown-periods-all", "drawdown-underwater-all"} <= set(keys))
        self.assertEqual(eras, {"managed", "inception"})


class MonitorTests(unittest.TestCase):
    def test_tolerance_alert_is_monitoring_not_a_rule(self):
        from desk import forward_monitor as fm

        item = next(i for i in fm.REGISTRY if i.key == "dd-tolerance")
        rows = fm.evaluate(fm.Sources(), [item])
        self.assertEqual(rows[0]["Status"], "monitoring, alert only")
        self.assertEqual(fm.attention(rows), [])


if __name__ == "__main__":
    unittest.main()
