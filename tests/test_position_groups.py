"""Open positions by strategy (desk/position_groups.py) and the tie-out of the groups against the table's total."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import position_groups as pg, reconcile  # noqa: E402

THESES = [
    {"coin": "UNI", "side": "SHORT", "strategy": "directional"},
    {"coin": "GRAM", "side": "SHORT", "strategy": "fade"},
    {"coin": "xyz:MSFT", "side": "SHORT", "strategy": "directional"},
    {"coin": "GOOGL", "side": "LONG", "strategy": "conviction"},
]


def rows():
    return [
        {"Account": "HL 07f2", "Symbol": "UNI", "Side": "SHORT", "Value": -700.0, "uPnL": 20.0, "Weight": 0.25},
        {"Account": "HL 07f2", "Symbol": "GRAM", "Side": "SHORT", "Value": -400.0, "uPnL": -10.0, "Weight": 0.15},
        {"Account": "HL c248", "Symbol": "ETH", "Side": "LONG", "Value": 5.0, "uPnL": 0.0, "Weight": 0.01},
        {"Account": "HL 07f2", "Symbol": "XMR", "Side": "LONG", "Value": 1800.0, "uPnL": -30.0, "Weight": 0.59},
    ]


def spec(rows_):
    return {"columns": [{"key": "Symbol", "fmt": "text"}, {"key": "Value", "fmt": "susd"}, {"key": "uPnL", "fmt": "susd"}],
            "rows": rows_, "total": {"Value": sum(r["Value"] for r in rows_), "uPnL": sum(r["uPnL"] for r in rows_)}}


class LabelTests(unittest.TestCase):
    def test_the_journal_label_decides_and_the_side_must_match(self):
        self.assertEqual(pg.strategy_of(THESES, "UNI", "SHORT"), "directional")
        self.assertEqual(pg.strategy_of(THESES, "GRAM", "SHORT"), "fade")
        self.assertEqual(pg.strategy_of(THESES, "UNI", "LONG"), pg.UNTAGGED)   # a long UNI is not the short's thesis

    def test_a_listed_market_matches_its_bare_name(self):
        self.assertEqual(pg.strategy_of(THESES, "MSFT", "SHORT"), "directional")
        self.assertEqual(pg.strategy_of(THESES, "xyz:MSFT", "SHORT"), "directional")

    def test_untagged_is_never_guessed_and_a_ladder_coin_is_market_making(self):
        self.assertEqual(pg.strategy_of(THESES, "XMR", "LONG"), pg.UNTAGGED)
        self.assertEqual(pg.strategy_of(THESES, "ETH", "LONG", ladder_coins=["ETH", "NEAR"]), pg.MARKET_MAKING)
        self.assertEqual(pg.strategy_of(THESES, "UNI", "SHORT", ladder_coins=["UNI"]), "directional")   # a tag wins over a ladder


class FeedTests(unittest.TestCase):
    def setUp(self):
        pg._theses_cache.update(at=0.0, value=[])

    def test_a_live_research_feed_is_used_as_it_is(self):
        self.assertEqual(pg.current_theses({"theses": THESES}), THESES)

    def test_a_stale_local_feed_reads_the_relay_and_caches_it(self):
        from unittest import mock

        with mock.patch("desk.web_sync.relay_get", return_value={"theses": THESES}) as relay:
            self.assertEqual(pg.current_theses({"strategies": []}, now=1000.0), THESES)
            self.assertEqual(pg.current_theses({}, now=1100.0), THESES)   # inside the ten minutes: no second read
        self.assertEqual(relay.call_count, 1)


class GroupTests(unittest.TestCase):
    def tables(self):
        return {"positions": spec(rows())}

    def test_annotate_adds_strategy_and_risk_as_a_share_of_the_cap(self):
        tables = self.tables()
        risks = {("07f2", "UNI"): 100.0, ("07f2", "GRAM"): 50.0, ("07f2", "XMR"): 200.0}
        pg.annotate(tables, THESES, ["ETH"], risks, 1000.0)
        by = {r["Symbol"]: r for r in tables["positions"]["rows"]}
        self.assertEqual((by["UNI"]["Strategy"], by["XMR"]["Strategy"], by["ETH"]["Strategy"]), ("directional", "untagged", "market making"))
        self.assertAlmostEqual(by["UNI"]["Risk"], 0.10)
        self.assertIsNone(by["ETH"]["Risk"])   # no risk figure: shown as absent, not as zero
        self.assertAlmostEqual(tables["positions"]["total"]["Risk"], 0.35)
        self.assertTrue({"Strategy", "Risk"} <= {c["key"] for c in tables["positions"]["columns"]})

    def test_group_subtotals_add_up_to_the_table_total(self):
        tables = self.tables()
        pg.annotate(tables, THESES, ["ETH"], {("07f2", "UNI"): 100.0}, 1000.0)
        groups = pg.group_totals(tables["positions"]["rows"])
        self.assertEqual(list(groups), ["directional", "fade", "market making", "untagged"])   # a fixed order
        self.assertAlmostEqual(sum(g["Value"] for g in groups.values()), tables["positions"]["total"]["Value"])
        self.assertAlmostEqual(sum(g["uPnL"] for g in groups.values()), tables["positions"]["total"]["uPnL"])
        self.assertEqual(sum(g["positions"] for g in groups.values()), 4)
        self.assertEqual(groups["untagged"]["positions"], 1)   # how many are untagged is visible

    def test_reconcile_passes_when_groups_tie_out(self):
        tables = self.tables()
        pg.annotate(tables, THESES, [], {("07f2", "UNI"): 100.0}, 1000.0)
        self.assertEqual(reconcile.strategy_groups(tables), [])

    def test_reconcile_flags_a_missing_group_and_a_total_that_drifted(self):
        tables = self.tables()
        pg.annotate(tables, THESES, [])
        tables["positions"]["rows"][0]["Strategy"] = ""
        found = reconcile.strategy_groups(tables)
        self.assertTrue(any("no strategy group" in text for _, text in found))
        tables = self.tables()
        pg.annotate(tables, THESES, [])
        tables["positions"]["total"]["uPnL"] += 25.0   # the table total no longer equals its rows
        self.assertTrue(any("uPnL" in text for _, text in reconcile.strategy_groups(tables)))

    def test_a_table_without_the_strategy_column_is_skipped(self):
        self.assertEqual(reconcile.strategy_groups(self.tables()), [])


class MarketMakingExclusionTests(unittest.TestCase):
    def build(self):
        rows_ = [dict(r, **{"Exposure / equity": abs(r["Value"]) / 1000, "uPnL (%)": 0.1, "24h change (%)": -0.02, "Funding (APR)": 0.05,
                            "Asset class": "crypto perp"}) for r in rows()]
        tables = {"positions": spec(rows_), "market_making": {"columns": [{"key": "Coin", "fmt": "text"}],
                                                              "rows": [{"Coin": "ETH"}, {"Coin": "SP500"}], "total": {}},
                  "exposure": {"columns": [], "rows": [], "total": {"Gross": sum(abs(r["Value"]) for r in rows_)}}}
        tables["positions"]["total"].update({"Risk": 0.35, "Weight": None})
        pg.annotate(tables, THESES, ["ETH"], {("07f2", "UNI"): 100.0, ("07f2", "GRAM"): 50.0, ("07f2", "XMR"): 200.0, ("c248", "ETH"): 5.0}, 1000.0)
        return tables

    def test_market_making_rows_leave_the_table_and_the_total_follows(self):
        tables = self.build()
        pg.exclude_market_making(tables)
        spec_ = tables["positions"]
        self.assertEqual([r["Symbol"] for r in spec_["rows"]], ["UNI", "GRAM", "XMR"])
        self.assertAlmostEqual(spec_["total"]["Value"], 700 * -1 + -400 + 1800)
        self.assertAlmostEqual(spec_["total"]["Exposure / equity"], 2.9)
        self.assertEqual(spec_["excluded"]["positions"], 1)
        self.assertAlmostEqual(spec_["excluded"]["gross"], 5.0)
        self.assertIn("Market making tile", spec_["total"]["__hover"])
        self.assertAlmostEqual(sum(r["Weight"] for r in spec_["rows"]), 0.99 / 0.99)   # re-based: the shown weights keep their total

    def test_the_split_still_ties_out_with_the_groups_and_the_exposure_table(self):
        tables = self.build()
        pg.exclude_market_making(tables)
        self.assertEqual(reconcile.strategy_groups(tables), [])
        self.assertEqual(reconcile.market_making_excluded(tables), [])
        tables["exposure"]["total"]["Gross"] += 40.0   # a position that is neither shown nor taken out
        self.assertTrue(any("market making" in text for _, text in reconcile.market_making_excluded(tables)))

    def test_nothing_is_taken_out_without_a_ladder(self):
        tables = self.build()
        for row in tables["positions"]["rows"]:
            row["Strategy"] = "untagged"
        pg.exclude_market_making(tables)
        self.assertNotIn("excluded", tables["positions"])
        self.assertEqual(len(tables["positions"]["rows"]), 4)

    def test_the_market_making_tile_gets_the_inventory(self):
        tables = self.build()
        pg.exclude_market_making(tables)
        pg.add_inventory(tables)
        by = {r["Coin"]: r for r in tables["market_making"]["rows"]}
        self.assertEqual(by["ETH"]["Inventory"], 5.0)
        self.assertIsNone(by["SP500"]["Inventory"])   # a ladder with no inventory: absent, not zero
        self.assertTrue({"Inventory", "Inventory uPnL (%)"} <= {c["key"] for c in tables["market_making"]["columns"]})


if __name__ == "__main__":
    unittest.main()
