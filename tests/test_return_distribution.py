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
