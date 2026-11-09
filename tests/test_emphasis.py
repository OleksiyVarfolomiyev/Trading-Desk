import re
import unittest
from unittest import mock

import pandas as pd

from desk import emphasis


class EmphasisTests(unittest.TestCase):
    def test_standout_modes_skip_text_nan_and_total(self):
        values = [3.0, -9.0, "n/a", float("nan"), 5.0, 100.0]
        self.assertEqual(emphasis.standout_position(values, "abs", skip={5}), 1)
        self.assertEqual(emphasis.standout_position(values, "max", skip={5}), 4)
        self.assertEqual(emphasis.standout_position(values, "min", skip={5}), 1)
        self.assertIsNone(emphasis.standout_position(values, None))
        self.assertIsNone(emphasis.standout_position(["a", None], "abs"))

    def test_normalize_accepts_lists_and_rejects_unknown_modes(self):
        self.assertEqual(emphasis.normalize(["A", "B"]), {"A": "abs", "B": "abs"})
        self.assertEqual(emphasis.normalize("A"), {"A": "abs"})
        with self.assertRaises(ValueError):
            emphasis.normalize({"A": "largest"})

    def test_interactive_table_tints_key_column_standout_and_headline_rows(self):
        import desk.hl.account_analytics as analytics

        frame = pd.DataFrame({"Account": ["A", "B", "Total"], "Equity": [10.0, 30.0, 40.0]})
        shown = []
        with mock.patch.object(analytics, "display", lambda markup: shown.append(markup.data)):
            analytics.display_interactive_table(frame, bold_last=True, key_columns={"Equity": "max"})
            analytics.display_interactive_table(pd.DataFrame({"Metric": ["Net PnL", "Fees"], "Value": [1, 2]}),
                                                key_rows={"Net PnL"})
        rows = re.findall(r"<tr>(.*?)</tr>", shown[0])[1:]
        equity = [re.findall(r"style='([^']*)'", row)[1] for row in rows]
        self.assertIn(emphasis.TOP_TINT, equity[1])       # B is the standout (largest)
        self.assertIn(emphasis.KEY_TINT, equity[0])
        self.assertNotIn(emphasis.TOP_TINT, equity[2])    # the total row never is
        self.assertIn(emphasis.KEY_HEADER, shown[0])
        metric_rows = re.findall(r"<tr>(.*?)</tr>", shown[1])[1:]
        self.assertIn(emphasis.KEY_TINT, metric_rows[0])
        self.assertNotIn(emphasis.KEY_TINT, metric_rows[1])

    def test_styler_tables_get_the_same_emphasis(self):
        frame = pd.DataFrame({"Beta": [0.2, -1.4, 0.9], "t": [1.0, 3.0, 0.5]}, index=["X", "Y", "Z"])
        html = emphasis.style_frame(frame.style, frame, {"Beta": "abs"}, key_rows={"Z"}).to_html()
        self.assertIn("inset 0 0 0 2px", html)   # the standout outline (Y, |beta| 1.4)
        self.assertRegex(html, r"0\.32")   # key tint

    def test_dashboard_tables_carry_the_key_columns(self):
        from desk.dashboard import table

        spec = table(pd.DataFrame({"Account": ["A"], "Equity": [1.0]}), {"Equity": "usd"}, key={"Equity": "max"})
        self.assertEqual(spec["columns"][1]["emphasis"], "max")
        self.assertNotIn("emphasis", spec["columns"][0])
        spec = table(pd.DataFrame({"VaR 95%": [1.0]}), {}, key={"VaR 95%": None})
        self.assertEqual(spec["columns"][0]["emphasis"], "none")


if __name__ == "__main__":
    unittest.main()
