"""The screener: scored only on validated signals, sized and stress-checked; ladder candidates are liquid chop."""

import unittest

import pandas as pd

from desk import screener


class ScreenerTests(unittest.TestCase):
    def test_scoring_uses_validated_signals_or_says_it_falls_back(self):
        weights, basis = screener.scoring({"signals": {"rel7": {"validated": True, "direction": 1.0}, "vol30": {"validated": False}}})
        self.assertEqual(weights, {"rel7": 1.0})
        self.assertIn("validated", basis)
        weights, basis = screener.scoring(None)
        self.assertEqual(weights, screener.FALLBACK)
        self.assertIn("desk record", basis)

    def test_short_candidates_are_the_lowest_scores_without_execution_columns(self):
        signals = pd.DataFrame({"rel7": [0.3, -0.2, 0.0, -0.4], "rel30": [0.1, -0.1, 0.0, -0.3],
                                "from_high60": [-0.1] * 4, "atr_pct": [0.05] * 4}, index=["A", "B", "C", "D"])
        live = pd.DataFrame({"funding_hour": [0.00001] * 4, "volume24h": [5e6] * 4}, index=["A", "B", "C", "D"])
        rows = screener.short_candidates(signals, live, ["A", "B", "C", "D"], {"rel7": 1.0}, "validated: 7d", 10_000.0, [], {"B"}, top=2,
                                         funding_history=lambda coin: [])
        self.assertEqual(list(rows["Coin"]), ["D", "B"])                   # weakest first
        self.assertIn("already held", rows.iloc[1]["Notes"])
        for execution in ("Stop (3 ATR)", "Size at budget", "Max by margin"):   # the executor's and the daemon's, not research
            self.assertNotIn(execution, rows.columns)

    def test_market_making_candidates_prefer_chop_and_liquidity(self):
        signals = pd.DataFrame({"efficiency": [0.9, 0.1, 0.2], "move30": [0.5, 0.01, -0.02], "range_pct": [0.05, 0.04, 0.03]},
                               index=["TREND", "CHOP", "QUIET"])
        live = pd.DataFrame({"funding_hour": [0.0] * 3, "volume24h": [5e6, 5e6, 1e6]}, index=["TREND", "CHOP", "QUIET"])
        rows = screener.market_making_candidates(signals, live, ["TREND", "CHOP", "QUIET"], {"CHOP"}, top=2)
        self.assertEqual(rows.iloc[0]["Coin"], "CHOP")
        self.assertEqual(rows.iloc[0]["Notes"], "ladder running")
        self.assertNotIn("TREND", list(rows["Coin"]))


    def test_the_fallback_puts_the_weakest_coin_first(self):
        weights, basis = screener.scoring(None)
        signals = pd.DataFrame({"rel30": [0.28, -0.20, 0.05], "rel7": [0.0] * 3, "from_high60": [-0.1] * 3,
                                "atr_pct": [0.05] * 3}, index=["STRONG", "WEAK", "FLAT"])
        live = pd.DataFrame({"funding_hour": [0.0] * 3, "volume24h": [5e6] * 3}, index=["STRONG", "WEAK", "FLAT"])
        rows = screener.short_candidates(signals, live, ["STRONG", "WEAK", "FLAT"], weights, basis, 10_000.0, [], set(),
                                         funding_history=lambda coin: [])
        self.assertEqual(list(rows["Coin"]), ["WEAK", "FLAT", "STRONG"])

    def test_crowded_shorts_are_flagged_from_a_week_of_funding_without_moving_the_score(self):
        signals = pd.DataFrame({"rel30": [-0.3, -0.2], "atr_pct": [0.05, 0.05]}, index=["CROWD", "CLEAN"])
        live = pd.DataFrame({"funding_hour": [0.0, 0.0], "volume24h": [1e6, 1e6], "open_interest": [2e6, 5e5]}, index=["CROWD", "CLEAN"])
        history = {"CROWD": [-0.00005] * 80 + [0.0000125] * 20, "CLEAN": [0.0000125] * 100}   # shorts pay 80% of hours; base rate
        rows = screener.short_candidates(signals, live, ["CROWD", "CLEAN"], {"rel30": 1.0}, "b", 10_000.0, [], set(),
                                         funding_history=history.get).set_index("Coin")
        self.assertEqual(list(rows.index), ["CROWD", "CLEAN"])                             # ranking unchanged by the flag
        self.assertAlmostEqual(rows.loc["CROWD", "Funding 7d (APR)"], (-0.00005 * 0.8 + 0.0000125 * 0.2) * 8760)   # the short pays
        self.assertEqual(rows.loc["CROWD", "Crowding"], "crowded")
        self.assertAlmostEqual(rows.loc["CLEAN", "Funding 7d (APR)"], 0.0000125 * 8760)    # HL's base rate: longs pay, the short earns ~11%
        self.assertEqual(rows.loc["CLEAN", "Crowding"], "")
        self.assertAlmostEqual(rows.loc["CROWD", "OI / volume"], 2.0)

    def test_no_funding_history_leaves_the_columns_blank(self):
        self.assertTrue(all(value != value for value in screener.crowding([])))

    def test_ladder_candidates_flag_range_edges_and_show_own_edge(self):
        signals = pd.DataFrame({"efficiency": [0.1, 0.15], "move30": [0.0, 0.01], "range_pct": [0.04, 0.04],
                                "range_position": [0.5, 0.95]}, index=["MID", "TOP"])
        live = pd.DataFrame({"funding_hour": [0.0, 0.0], "volume24h": [5e6, 5e6]}, index=["MID", "TOP"])
        rows = screener.market_making_candidates(signals, live, ["MID", "TOP"], set(), edges={"MID": 0.42}).set_index("Coin")
        self.assertIn("breakout risk", rows.loc["TOP", "Notes"])
        self.assertEqual(rows.loc["MID", "Own ladder edge per $1k"], 0.42)
        self.assertTrue(pd.isna(rows.loc["TOP", "Own ladder edge per $1k"]))   # no made-up edge where no ladder has traded


if __name__ == "__main__":
    unittest.main()


class NumbersTests(unittest.TestCase):
    def test_dollars_round_for_reading(self):
        from desk.numbers import usd

        self.assertEqual([usd(v) for v in (0.42, -3.456, 548.9, 1234.5, 12345, 2528475.6, 999950, float("nan"))],
                         ["$0.42", "-$3.46", "$549", "$1.23K", "$12.3K", "$2.53M", "$1M", "n/a"])
