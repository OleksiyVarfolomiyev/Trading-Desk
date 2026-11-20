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


if __name__ == "__main__":
    unittest.main()
