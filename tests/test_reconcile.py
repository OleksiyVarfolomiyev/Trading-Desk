"""The dashboard's tables tie out (desk/reconcile.py) and name the same thing the same way everywhere."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import reconcile  # noqa: E402


def table(columns, rows, total=None):
    return {"columns": [{"key": column} for column in columns], "rows": rows, "total": total}


class TieOutTests(unittest.TestCase):
    def tables(self, strategy_upnl=-0.03, account_upnl=-0.028, shares=(0.6, 0.4), weights=((0.5, 0.3), (0.2,))):
        return {
            "strategies": table(["Strategy", "uPnL"], [{"Strategy": "untagged", "uPnL": strategy_upnl}],
                                {"Strategy": "Total", "uPnL": strategy_upnl}),
            "accounts": table(["Account", "% of desk", "uPnL / equity"],
                              [{"Account": f"A{i}", "% of desk": share} for i, share in enumerate(shares)],
                              {"Account": "Total", "% of desk": 1, "uPnL / equity": account_upnl}),
            "positions": table(["Symbol", "Weight"], [{"Symbol": "X", "Weight": w} for w in weights[0]]),
            "positions_stocks": table(["Symbol", "Weight"], [{"Symbol": "Y", "Weight": w} for w in weights[1]]),
        }

    def test_tables_that_tie_out_raise_nothing(self):
        self.assertEqual(reconcile.checks(self.tables()), [])   # 0.2 points apart: read moments apart

    def test_a_book_missing_from_one_table_is_flagged(self):
        # The strategy table once left out IB: +1.2% there against -2.8% in the accounts table.
        rows = reconcile.checks(self.tables(strategy_upnl=0.012, account_upnl=-0.028))
        self.assertEqual(rows, [("Tables disagree", "uPnL: strategies 1.2%, accounts -2.8%")])

    def test_shares_of_a_whole_add_up_to_it(self):
        rows = reconcile.checks(self.tables(shares=(0.6, 0.3), weights=((0.5, 0.3), (0.1,))))
        self.assertEqual([text.split(":")[0] for _, text in rows], ["desk shares", "position weights"])

    def test_a_missing_table_is_not_a_break(self):
        self.assertEqual(reconcile.checks({"accounts": self.tables()["accounts"]}), [])


class WindowTests(unittest.TestCase):
    """Figures compared in one table or chart share their start, end and base."""

    def line(self, name, first, last):
        return {"name": name, "x": [first, last], "y": [0, 1]}

    def test_books_a_day_behind_the_desk_are_in_line(self):
        twr = [self.line("Desk (HL + IB)", "2025-01-01", "2026-11-19"), self.line("crypto", "2025-11-20", "2026-11-18"),
               self.line("stocks", "2025-01-14", "2026-11-18"), self.line("SPY", "2025-01-01", "2026-11-14")]
        self.assertEqual(reconcile.windows({"twr": twr}, {}), [])   # the chart cuts to Nov 18; SPY does not set the end

    def test_a_stale_book_is_flagged(self):
        twr = [self.line("Desk (HL + IB)", "2025-01-01", "2026-11-19"), self.line("crypto", "2025-11-20", "2026-11-15")]
        self.assertEqual(reconcile.windows({"twr": twr}, {}), [("Windows differ", "return chart: crypto ends Nov 15, desk Nov 19")])

    def test_lines_without_a_common_day_are_flagged(self):
        twr = [self.line("Desk (HL + IB)", "2026-11-10", "2026-11-19"), self.line("stocks", "2025-01-01", "2025-02-01")]
        texts = [text for _, text in reconcile.windows({"twr": twr}, {})]
        self.assertIn("return chart: the desk and book lines share no days", texts)

    def test_attribution_windows_that_differ_are_flagged(self):
        comparison = {"attribution": {"crypto": {"All": {"start": "2025-11-20", "end": "2026-11-18"}},
                                      "stocks": {"All": {"start": "2025-01-14", "end": "2026-11-18"}}}}
        self.assertEqual(reconcile.windows({}, comparison),
                         [("Windows differ", "excess return All: crypto Nov 20 '25 to Nov 18, stocks Jan 14 '25 to Nov 18")])

    def test_a_weekend_gap_from_spy_sessions_is_allowed(self):
        # Sunday: crypto ends Saturday's close, stocks Friday's (2026-11-20 is a Friday).
        comparison = {"attribution": {"crypto": {"W": {"start": "2026-11-14", "end": "2026-11-21"}},
                                      "stocks": {"W": {"start": "2026-11-13", "end": "2026-11-20"}}}}
        self.assertEqual(reconcile.windows({}, comparison), [])

    def test_a_weekday_gap_is_not_a_weekend_gap(self):
        comparison = {"attribution": {"crypto": {"W": {"start": "2026-11-11", "end": "2026-11-18"}},
                                      "stocks": {"W": {"start": "2026-11-11", "end": "2026-11-17"}}}}
        self.assertEqual(len(reconcile.windows({}, comparison)), 1)

    def test_books_start_side_by_side_comparisons_together(self):
        import pandas as pd

        from desk.web_sync import common_start

        crypto = pd.Series([0.01], index=pd.to_datetime(["2025-11-20"], utc=True))
        stocks = pd.Series([0.01, 0.0], index=pd.to_datetime(["2025-01-14", "2025-11-20"], utc=True))
        self.assertEqual(common_start({"crypto": crypto, "stocks": stocks}), pd.Timestamp("2025-11-20", tz="UTC"))
        self.assertIsNone(common_start({}))


# One name per thing on every client surface. Each banned label maps to the name the desk uses for it; add a
# pair whenever two tables are found calling the same thing by different names.
SAME_THING = {
    "Unrealized": "uPnL",
    "Unrealized PnL %": "uPnL",
    "Return 7d": "Return 7D",
}
SURFACES = ["web/live.js", "desk/dashboard_static/app.js", "web/index.html", "desk/dashboard_static/index.html"]


class LabelTests(unittest.TestCase):
    def test_the_same_thing_has_the_same_name_on_every_surface(self):
        from desk import journal

        shown = set(journal.VIEW_COLUMNS)
        for path in SURFACES:
            shown |= set(re.findall(r'"([^"\n]{1,40})"', (ROOT / path).read_text()))
        for banned, name in SAME_THING.items():
            self.assertNotIn(banned, shown, f'"{banned}" is shown somewhere; the desk calls it "{name}"')

    def test_reconcile_checks_the_columns_the_tables_have(self):
        from desk import journal

        self.assertIn("uPnL", journal.VIEW_COLUMNS)       # the strategy total reconcile reads
        live = (ROOT / "web/live.js").read_text()
        self.assertIn('"uPnL / equity"', live)
        self.assertIn('"% of desk"', live)
        self.assertIn("reconcile(tables)", live)          # the browser runs the same checks


if __name__ == "__main__":
    unittest.main()
