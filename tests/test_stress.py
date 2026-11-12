"""Scenario stress: the book's loss in a correlated shock, and the entry check built on it."""

import unittest

from desk import stress

BOOK = [("crypto perp", "ICP", -4000.0), ("crypto perp", "BTC", -1500.0), ("crypto spot", "sVVV (VVV staked, base)", 2000.0),
        ("crypto perp", "VVV", -2000.0), ("equity", "MU", 800.0), ("tradfi perp", "xyz:NVDA", 300.0),
        ("tradfi perp", "xyz:GOLD", 500.0), ("crypto spot", "USDC (base)", 400.0)]


class ScenarioTests(unittest.TestCase):
    def test_positions_are_classified(self):
        kinds = {symbol: stress.classify(cls, symbol) for cls, symbol, _ in BOOK}
        self.assertEqual(kinds["ICP"], "alt")
        self.assertEqual(kinds["BTC"], "major")
        self.assertEqual(kinds["sVVV (VVV staked, base)"], "alt")
        self.assertEqual(kinds["xyz:NVDA"], "equity")
        self.assertEqual(kinds["xyz:GOLD"], "other")  # a commodity is not an equity
        self.assertEqual(kinds["USDC (base)"], "stable")
        self.assertEqual(kinds["MU"], "equity")

    def test_losses_net_the_staked_vvv_against_the_short(self):
        losses = stress.scenario_losses(BOOK)
        # alts: shorts ICP -4000 and VVV -2000 lose 25%; staked VVV +2000 gains 25%; BTC short loses 10%.
        self.assertAlmostEqual(losses["alt rally"], 0.25 * (4000 + 2000 - 2000) + 0.10 * 1500)
        # equities: MU 800 and NVDA 300 lose 10%.
        self.assertAlmostEqual(losses["equity sell-off"], 0.10 * (800 + 300))

    def test_status_levels(self):
        status = lambda equity: {item["scenario"]: item["status"] for item in stress.evaluate(BOOK, equity)}["alt rally"]
        self.assertEqual(status(10_000.0), "ok")     # 1,150 on 10,000 is 11.5%
        self.assertEqual(status(4_000.0), "warn")    # 28.75%: past 25%
        self.assertEqual(status(3_000.0), "block")   # 38%: past 35%

    def test_an_entry_that_adds_to_a_scenario_is_cut_to_fit_the_block_limit(self):
        equity = 5_000.0                                              # limit 1,750; the book loses 1,150 in an alt rally
        fraction, note = stress.fit_entry(BOOK, equity, "crypto perp", "INIT", -4000.0)
        self.assertAlmostEqual(fraction, (0.35 * equity - 1150) / (0.25 * 4000))  # 600 of room for a 1,000 extra loss
        self.assertIn("alt rally stress", note)
        self.assertIn("cut to", note)

    def test_an_entry_that_reduces_the_loss_or_is_unrelated_always_fits(self):
        self.assertEqual(stress.fit_entry(BOOK, 5_000.0, "crypto perp", "INIT", +4000.0)[0], 1.0)   # a long alt hedges the rally
        self.assertEqual(stress.fit_entry(BOOK, 5_000.0, "tradfi perp", "xyz:GOLD", +3000.0)[0], 1.0)  # no shock on gold

    def test_a_full_scenario_refuses_the_entry(self):
        fraction, note = stress.fit_entry(BOOK, 3_000.0, "crypto perp", "INIT", -500.0)  # already past 35%
        self.assertEqual(fraction, 0.0)
        self.assertIn("entry refused", note)

    def test_notices_only_when_warned(self):
        self.assertEqual(stress.notices(BOOK, 10_000.0), [])
        rows = stress.notices(BOOK, 4_000.0)
        self.assertEqual(len(rows), 1)
        self.assertIn("alt rally (warn)", rows[0][0])


if __name__ == "__main__":
    unittest.main()
