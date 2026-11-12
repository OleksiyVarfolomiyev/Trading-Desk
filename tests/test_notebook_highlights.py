import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

import pandas as pd

from desk.hl.backtest import backtest_highlights
from desk.hl.market_research import CHANGE, FUNDING, VOLUME, market_highlights
from desk.notebook_highlights import HighlightsState, render


class HighlightsStateTests(unittest.TestCase):
    def test_compares_numbers_and_items_with_the_last_run(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        first = HighlightsState(path).compare({"Desk value": (1000.0, "usd")}, {"Positions": ["AAPL long", "QNT short"]})
        self.assertIsNone(first.last_run)
        second = HighlightsState(path).compare({"Desk value": (1100.0, "usd"), "Sharpe": (float("nan"), "num")},
                                               {"Positions": ["AAPL long", "IONQ short"]})
        self.assertEqual(second.changes, [("Desk value", "$1,000.00", "$1,100.00", "$100.00")])
        self.assertIn("Positions added: IONQ short.", second.notes)
        self.assertIn("Positions removed: QNT short.", second.notes)

    def test_baseline_is_kept_when_the_same_state_object_runs_again(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        HighlightsState(path).compare({"Value": (1.0, "num")})
        state = HighlightsState(path)
        state.compare({"Value": (2.0, "num")})
        again = state.compare({"Value": (2.0, "num")})
        self.assertEqual(again.changes[0][3], "1.00")

    def test_render_escapes_text_and_lists_both_sections(self):
        block = render([("Good", "a < b")], [], None)
        self.assertIn("a &lt; b", block)
        self.assertIn("NEEDS ATTENTION", block)
        self.assertIn("Nothing to report.", block)


class ReportHighlightTests(unittest.TestCase):
    def test_market_highlights_flag_extreme_funding(self):
        universe = pd.DataFrame({"Ticker": ["A", "B", "C"], CHANGE: [5.0, -3.0, 1.0],
                                 FUNDING: [0.08, -0.001, 0.01], VOLUME: [1e8, 5e7, 1e7]})
        well, attention = market_highlights(universe, "crypto")
        self.assertIn("1 markets; A 0.0800", dict(attention)["Funding beyond ±0.05%/8h"])
        self.assertIn("67% of 3 markets are up", dict(well)["Breadth"])

    def test_backtest_highlights_split_pnl_and_flag_losing_signals(self):
        trades = pd.DataFrame({
            "coin": ["BTC", "BTC", "ETH", "ETH"], "side": ["LONG"] * 4,
            "entry_time": ["2026-01-01", "2026-01-08", "2026-01-01", "2026-01-08"],
            "exit_reason": ["target", "stop", "stop", "stop"],
            "price_pnl": [100.0, -40.0, -30.0, -30.0], "funding_pnl": [-1.0] * 4, "fees": [2.0] * 4,
            "net_pnl": [97.0, -43.0, -33.0, -33.0]})
        well, attention = backtest_highlights(trades, [{"coin": "ETH", "reason": "gap"}], 10_000)
        self.assertIn("Worst signal", dict(attention))
        self.assertIn("ETH long", dict(attention)["Worst signal"])
        self.assertIn("1 entries skipped", dict(attention)["Skipped entries"])
        self.assertIn("stop 75%", dict(well)["Exits"])



class MarketMoverSignTests(unittest.TestCase):
    def universe(self, changes):
        from desk.hl import market_research as research
        return pd.DataFrame({"Ticker": [f"T{i}" for i in range(len(changes))], research.CHANGE: changes,
                             research.FUNDING: float("nan"), research.VOLUME: float("nan")})

    def test_a_falling_market_has_no_gainers(self):
        from desk.hl.market_research import market_highlights

        well, attention = market_highlights(self.universe([-0.35, -1.2, -2.5, -4.0]), "crypto")
        updates = {area: text for area, text in well + attention}
        self.assertNotIn("Biggest gainers", updates)
        self.assertIn("none of 4 markets is up", updates["No gainers"])
        self.assertIn("T0 -0.35", updates["No gainers"])
        self.assertIn("T3 -4.00", updates["Biggest losers"])

    def test_gainers_are_only_rising_markets(self):
        from desk.hl.market_research import market_highlights

        well, attention = market_highlights(self.universe([3.0, 1.0, -0.5]), "crypto")
        updates = {area: text for area, text in well + attention}
        self.assertEqual(updates["Biggest gainers"], "T0 3.00, T1 1.00 (%).")
        self.assertEqual(updates["Biggest losers"], "T2 -0.50 (%).")


if __name__ == "__main__":
    unittest.main()


class InteractiveTableStyleTests(unittest.TestCase):
    def test_bold_total_row_keeps_its_colour(self):
        import re
        from unittest import mock

        import pandas as pd

        import desk.hl.account_analytics as analytics

        frame = pd.DataFrame({"Asset class": ["a", "Total"], "Net": [-5.0, 7.0]})
        shown = []
        with mock.patch.object(analytics, "display", lambda markup: shown.append(markup.data)):
            analytics.display_interactive_table(
                frame, cell_style=lambda i, column, value, row: "color:#188038" if column == "Net" and value > 0
                else "color:#c62828" if column == "Net" else "color:inherit", bold_last=True)
        styles = re.findall(r"style='([^']*)'", shown[0])
        # Without a ';' between them the browser drops "color:#188038font-weight:700;" whole.
        self.assertIn("color:#4ade80;font-weight:700;", styles)  # green, brightened for the black table
        self.assertFalse(any(re.search(r"[0-9a-z]font-weight", style) for style in styles))


class ReloadDeskTests(unittest.TestCase):
    def test_stale_helper_imported_by_another_module_is_replaced(self):
        import inspect

        import desk.hl.account_analytics as analytics
        import desk.overview as overview
        from desk.reloading import reload_desk

        def old_table(frame, formatters=None, cell_style=None, bold_last=False, columns=None,
                      blank_missing_cells=None, html_columns=(), title=None):
            """The table helper as a kernel loaded it before key_columns existed."""

        # A kernel that loaded the overview before the helper changed: both hold the old function.
        analytics.display_interactive_table = old_table
        overview.display_interactive_table = old_table
        reload_desk()
        self.assertIn("key_columns", inspect.signature(overview.display_interactive_table).parameters)
        self.assertIs(overview.display_interactive_table, analytics.display_interactive_table)


class MarketKindSplitTests(unittest.TestCase):
    """Crypto and stocks are never ranked against each other."""

    def test_a_mixed_view_gets_highlights_per_kind(self):
        import pandas as pd
        from desk.hl.market_research import market_highlights

        universe = pd.DataFrame([
            {"Ticker": "ZORA", "Dex": "", "Category": "", CHANGE: 12.0, FUNDING: 0.01, VOLUME: 5e7},
            {"Ticker": "ICP", "Dex": "", "Category": "", CHANGE: -4.0, FUNDING: 0.01, VOLUME: 9e7},
            {"Ticker": "xyz:NVDA", "Dex": "xyz", "Category": "stocks", CHANGE: 3.0, FUNDING: 0.001, VOLUME: 4e8},
            {"Ticker": "xyz:TSLA", "Dex": "xyz", "Category": "stocks", CHANGE: -2.0, FUNDING: 0.001, VOLUME: 2e8}])
        well, attention = market_highlights(universe, "trending")
        labels = dict(well + attention)
        self.assertTrue(labels["Biggest gainers (crypto)"].startswith("ZORA"))
        self.assertTrue(labels["Biggest gainers (stocks)"].startswith("xyz:NVDA"))
        self.assertTrue(labels["Most traded (stocks)"].startswith("xyz:NVDA"))
        self.assertNotIn("xyz", labels["Most traded (crypto)"])

    def test_a_single_kind_view_keeps_plain_labels(self):
        import pandas as pd
        from desk.hl.market_research import market_highlights

        universe = pd.DataFrame([{"Ticker": "ZORA", "Dex": "", "Category": "", CHANGE: 12.0, FUNDING: 0.01, VOLUME: 5e7}])
        self.assertIn("Biggest gainers", dict(market_highlights(universe, "crypto")[0]))

    def test_market_kinds(self):
        from desk.hl.market_research import market_kind

        self.assertEqual(market_kind("ICP", "", ""), "Crypto")
        self.assertEqual(market_kind("HYPE/USDC", None, "spot"), "Crypto")
        self.assertEqual(market_kind("para:TOTAL2", "para", "crypto"), "Crypto")
        self.assertEqual(market_kind("xyz:NVDA", "xyz", "stock"), "Stocks")
        self.assertEqual(market_kind("xyz:GOLD", "xyz", "commodities"), "Commodities & FX")

    def test_hl_highlights_list_crypto_and_stocks_apart(self):
        import pandas as pd
        from desk.hl.highlights import _by_kind

        groups = dict(_by_kind(pd.Series({"ICP": 5.0, "xyz:RELIANCE": 2.0})))
        self.assertEqual(sorted(groups), [" (crypto)", " (stocks)"])
        self.assertEqual(list(dict(_by_kind(pd.Series({"ICP": 5.0}))).keys()), [""])


class MathTypesettingTests(unittest.TestCase):
    """Two dollar signs in one line must not be read as a math formula by the notebook viewer."""

    def test_highlights_and_tables_are_marked_not_math(self):
        from unittest.mock import patch

        from desk import notebook_highlights as nh

        shown = []
        with patch.object(nh, "display", side_effect=lambda obj, **kw: shown.append(obj.data)):
            nh.show([("Largest winners (crypto)", "MELANIA $110.97 (3.9%), ICP $42.18 (4.5%).")], [])
        self.assertIn("tex2jax_ignore", shown[0])
        self.assertIn("MELANIA $110.97 (3.9%), ICP $42.18", shown[0])   # the text itself is untouched
        self.assertTrue(nh.no_math("<b>$1 and $2</b>").startswith("<div class='tex2jax_ignore'>"))


class MissingValueCellTests(unittest.TestCase):
    def test_missing_values_are_empty_cells_not_n_a(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        import pandas as pd
        from desk.hl import account_analytics

        shown = []
        frame = pd.DataFrame({"Coin": ["A", "B", "C"], "Value": [1.0, float("nan"), 3.0], "Note": ["ok", "n/a", None]})
        with patch.object(account_analytics, "display", side_effect=lambda obj, **kw: shown.append(obj.data)), \
                patch.object(account_analytics, "HTML", side_effect=lambda markup: SimpleNamespace(data=markup)):
            account_analytics.display_interactive_table(frame, formatters={"Value": lambda v: f"${v:,.2f}"})
        self.assertNotIn("n/a", shown[0].replace("var ", ""))   # neither the NaN, the "n/a" text nor the None
