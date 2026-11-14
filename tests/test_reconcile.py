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
