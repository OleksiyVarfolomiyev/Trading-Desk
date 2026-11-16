import unittest

import numpy as np

from desk import return_distribution as dist


def fill(direction, price, size, pnl, coin="BTC", cloid=None):
    return {"dir": direction, "px": str(price), "sz": str(size), "closedPnl": str(pnl), "coin": coin, "time": 1_794_000_000_000,
            "cloid": cloid}


class ClosedReturns(unittest.TestCase):
    def test_return_is_pnl_over_the_cost_of_the_part_closed(self):
        # Long 2 from 100, closed at 110: PnL 20 on cost 200 = 10%. Short 1 from 100, closed at 90: PnL 10 on 100 = 10%.
        frame = dist.closed_returns({"0xabcd": [fill("Close Long", 110, 2, 20), fill("Close Short", 90, 1, 10),
                                                fill("Open Long", 100, 1, 0)]})
        self.assertEqual(len(frame), 2)
        self.assertTrue(np.allclose(frame["return"], 0.10))
        self.assertEqual(list(frame["notional"]), [220.0, 90.0])

    def test_ladder_fills_and_spot_are_left_out(self):
        from desk.market_making import LADDER_CLOID_PREFIX

        frame = dist.closed_returns({"0xabcd": [fill("Close Long", 110, 2, 20, cloid=LADDER_CLOID_PREFIX + "1"),
                                                fill("Close Long", 110, 2, 20, coin="@12")]})
        self.assertTrue(frame.empty)


class Density(unittest.TestCase):
    def test_density_integrates_to_one_and_summary_weights_by_volume(self):
        values, weights = np.array([-0.05, 0.02, 0.03]), np.array([100.0, 300.0, 600.0])
        grid = np.linspace(-0.6, 0.6, 2000)
        area = np.trapezoid(dist.weighted_density(values, weights, grid), grid)
        self.assertAlmostEqual(area, 1.0, places=2)
        import pandas as pd

        stats = dist.summary(pd.DataFrame({"return": values, "notional": weights}))
        self.assertAlmostEqual(stats["in profit"], 0.9)


if __name__ == "__main__":
    unittest.main()


class Metrics(unittest.TestCase):
    def test_payoff_profit_factor_and_expectancy(self):
        import pandas as pd

        frame = pd.DataFrame({"side": ["LONG", "LONG", "SHORT"], "return": [0.10, -0.05, 0.02],
                              "notional": [100.0, 100.0, 50.0], "pnl": [10.0, -5.0, 1.0], "fees": [0.1, 0.1, 0.1],
                              "hold_days": [1.0, 3.0, 2.0]})
        table = dist.metrics_table(frame).set_index("Side")
        self.assertAlmostEqual(table.loc["Long", "Payoff"], 2.0)
        self.assertAlmostEqual(table.loc["Long", "Profit factor"], 2.0)
        self.assertAlmostEqual(table.loc["Long", "Expectancy"], 0.025)
        self.assertEqual(table.loc["All", "Closes"], 3)
        self.assertAlmostEqual(table.loc["All", "Median hold (days)"], 2.0)

    def test_ib_closes_use_realized_pnl_and_fifo_open_time(self):
        import tempfile
        from pathlib import Path

        head = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,"
                "Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\\n".replace("\\n", "\n"))
        text = head + ('Trades,Data,Order,Stocks,USD,MU,"2026-11-02, 10:00:00",2,100,100,-200,-1,200,0,0,O\n'
                       'Trades,Data,Order,Stocks,USD,MU,"2026-11-05, 10:00:00",-1,110,110,110,-1,-100,9,9,C\n')
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "ib_statement_0000_20260101_20261113.csv").write_text(text)
            frame = dist.ib_statement_closes(folder)
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertAlmostEqual(row["return"], 0.09)          # IB's realized 9 over the basis 100
        self.assertAlmostEqual(row["entry"], 100.0)
        self.assertAlmostEqual(row["hold_days"], 3.0)
        self.assertEqual(row["side"], "LONG")


class TradesPerDay(unittest.TestCase):
    def test_counts_book_fills_per_day_and_skips_ladders(self):
        from desk.market_making import LADDER_CLOID_PREFIX

        day = 1_794_000_000_000
        fills = {"0xabcd": [{"coin": "BTC", "time": day, "cloid": None}, {"coin": "ETH", "time": day + 1000, "cloid": None},
                            {"coin": "BTC", "time": day + 2000, "cloid": LADDER_CLOID_PREFIX + "1"},
                            {"coin": "xyz:NVDA", "time": day + 3000, "cloid": None},
                            {"coin": "BTC", "time": day + 90_000_000, "cloid": None}]}
        crypto = dist.trades_per_day(fills, "Crypto")
        self.assertEqual(list(crypto.values), [2, 1])
        self.assertEqual(int(dist.trades_per_day(fills, "Stocks").sum()), 1)
