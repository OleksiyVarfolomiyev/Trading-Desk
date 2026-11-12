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
        self.assertAlmostEqual(row["Spread captured"] + row["Inventory drift"] - row["Fees"], row["Ladder PnL"])
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
                               "Fees": 0.1, "Markout 1m (bps)": -2.0, "Markout 15m (bps)": float("nan")},
                              {"Fills": 1, "Volume": 100.0, "Spread captured": 0.0, "Inventory drift": 0.0, "Ladder PnL": 0.0,
                               "Fees": 0.0, "Markout 1m (bps)": 2.0, "Markout 15m (bps)": 4.0}])
        total = market_making.total_row(frame)
        self.assertAlmostEqual(total["Markout 1m (bps)"], -1.0)
        self.assertAlmostEqual(total["Markout 15m (bps)"], 4.0)
        self.assertAlmostEqual(total["Inventory drift"], -2.0)


if __name__ == "__main__":
    unittest.main()
