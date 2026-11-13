"""The ladders' table splits their result into spread and inventory drift, and measures markouts."""

import unittest

import pandas as pd

from desk import market_making


class LadderMetricsTests(unittest.TestCase):
    def test_ladder_pnl_is_spread_plus_inventory_drift_less_fees(self):
        # Sold 1 at 102, bought 1 at 100 (a 2 round trip), then bought 2 more at 99; the mark is 97.
        fills = [{"side": "A", "px": "102", "sz": "1", "fee": "0.1"}, {"side": "B", "px": "100", "sz": "1", "fee": "0.1"},
                 {"side": "B", "px": "99", "sz": "2", "fee": "0.1"}]
        row = market_making.metrics(fills, mark=97.0)
        self.assertAlmostEqual(row["Ladder PnL"], 102 - 100 - 198 + 2 * 97 - 0.3)   # 2 still held net, at 97
        self.assertAlmostEqual(row["Spread captured"] + row["Inventory drift"] + row["Fees"], row["Ladder PnL"])
        self.assertAlmostEqual(row["Fees"], -0.3)  # paid: negative
        self.assertLess(row["Inventory drift"], 0)  # bought into a fall

    def test_markouts_are_signed_for_the_fill(self):
        minute = 60_000
        closes = pd.Series({index * minute: 100.0 - index for index in range(30)})  # falls 1 a minute
        buy = {"side": "B", "px": "100", "time": 0}
        sell = {"side": "A", "px": "100", "time": 0}
        late = {"side": "B", "px": "80", "time": 29 * minute}                      # no candle 15 minutes on yet
        moves = market_making.markouts([buy], closes)
        self.assertAlmostEqual(moves["Markout 1m (bps)"], -100.0)    # bought, the price fell 1%
        self.assertAlmostEqual(moves["Markout 15m (bps)"], -1500.0)
        self.assertAlmostEqual(market_making.markouts([sell], closes)["Markout 1m (bps)"], 100.0)
        self.assertAlmostEqual(market_making.markouts([buy, late], closes)["Markout 15m (bps)"], -1500.0)
        self.assertTrue(market_making.markouts([buy], pd.Series(dtype=float))["Markout 1m (bps)"] !=
                        market_making.markouts([buy], pd.Series(dtype=float))["Markout 1m (bps)"])  # nothing known: NaN

    def test_the_total_weights_markouts_by_fills(self):
        frame = pd.DataFrame([{"Fills": 3, "Volume": 100.0, "Spread captured": 1.0, "Inventory drift": -2.0, "Ladder PnL": -1.1,
                               "Fees": 0.1, "Funding": 0.5, "Markout 1m (bps)": -2.0, "Markout 15m (bps)": float("nan")},
                              {"Fills": 1, "Volume": 100.0, "Spread captured": 0.0, "Inventory drift": 0.0, "Ladder PnL": 0.0,
                               "Fees": 0.0, "Funding": 0.0, "Markout 1m (bps)": 2.0, "Markout 15m (bps)": 4.0}])
        total = market_making.total_row(frame)
        self.assertAlmostEqual(total["Markout 1m (bps)"], -1.0)
        self.assertAlmostEqual(total["Markout 15m (bps)"], 4.0)
        self.assertAlmostEqual(total["Inventory drift"], -2.0)

    def test_ladder_funding_is_its_share_of_the_position_at_each_payment(self):
        # Short 10; the ladder sells 2 more (inventory -2), then buys 1 back (inventory -1).
        fills = [{"side": "A", "sz": "2", "time": 1_000}, {"side": "B", "sz": "1", "time": 3_000}]
        events = [{"time": 500, "delta": {"coin": "X", "szi": "-10", "usdc": "1.0"}},     # before any fill: none is the ladder's
                  {"time": 2_000, "delta": {"coin": "X", "szi": "-12", "usdc": "1.2"}},   # inventory -2 of -12: a sixth
                  {"time": 4_000, "delta": {"coin": "X", "szi": "-11", "usdc": "1.1"}}]   # inventory -1 of -11: an eleventh
        parts = [amount for _, amount in market_making.ladder_funding_parts(fills, events)]
        self.assertEqual(parts[0], 0.0)
        self.assertAlmostEqual(parts[1], 0.2)
        self.assertAlmostEqual(parts[2], 0.1)


if __name__ == "__main__":
    unittest.main()


class LadderFrameTests(unittest.TestCase):
    def test_a_running_ladder_builds_its_row(self):
        # 2026-11-13 the row failed to build (a local variable hid the date helper): the whole table went missing.
        from types import SimpleNamespace
        from unittest.mock import patch

        wallet = "0x" + "ab" * 20
        ladder = {"wallet": wallet, "coin": "BTC", "base": -0.02, "started": "2026-11-12 10:00"}
        fill = {"coin": "BTC", "side": "B", "px": "69000", "sz": "0.001", "fee": "0.01", "time": 1_763_000_000_000}
        state = {"assetPositions": [{"position": {"coin": "BTC", "szi": "-0.021", "positionValue": "1449"}}]}
        with patch.object(market_making, "running_ladders", return_value=[ladder]), \
                patch("desk.accounts.load_accounts", return_value=[SimpleNamespace(id=wallet, venue="hl", name="HL abab")]), \
                patch("desk.hl.account_analytics.post", return_value=state), \
                patch.object(market_making, "ladder_fill_rows", return_value=[fill]), \
                patch.object(market_making, "funding_events", return_value=[]), \
                patch.object(market_making, "minute_closes", return_value=pd.Series(dtype=float)):
            frame = market_making.market_making_frame()
        self.assertEqual(list(frame["Coin"]), ["BTC"])
        self.assertTrue(frame.iloc[0]["Since"])          # "Nov 12" (or "Nov'26" once old)
        self.assertEqual(frame.iloc[0]["Status"], "quoting")
