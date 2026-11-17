import unittest

import numpy as np
import pandas as pd

from desk import kelly


def prices(days=400, coins=("AAA", "BBB", "CCC", "BTC")):
    rng = np.random.default_rng(1)
    index = pd.date_range("2025-01-01", periods=days, tz="UTC")
    drift = {"AAA": 0.002, "BBB": -0.002, "CCC": 0.0, "BTC": 0.0005}
    return pd.DataFrame({c: 100 * np.cumprod(1 + drift[c] + 0.02 * rng.standard_normal(days)) for c in coins}, index=index)


class KellyComparison(unittest.TestCase):
    def test_the_optimizer_is_shared_and_respects_the_bounds(self):
        returns = prices().pct_change().dropna()[["AAA", "BBB", "CCC"]]
        weights = kelly.fit_weights(returns)
        self.assertTrue((weights.abs() <= kelly.BOUND + 1e-9).all())
        self.assertGreater(weights["AAA"], weights["BBB"])   # the rising coin is held long, the falling one short

    def test_many_coins_keep_the_total_near_the_gross_limit(self):
        rng = np.random.default_rng(2)
        returns = pd.DataFrame(0.001 * np.sign(rng.standard_normal(40)) + 0.02 * rng.standard_normal((300, 40)))
        self.assertLessEqual(kelly.fit_weights(returns).abs().sum(), kelly.MAX_GROSS + 1e-6)

    def test_walk_forward_uses_only_earlier_days_and_the_comparison_lines_up(self):
        closes = prices()
        returns = closes.pct_change(fill_method=None).iloc[1:]
        oos = kelly.walk_forward(returns[["AAA", "BBB", "CCC"]])
        self.assertEqual(oos.index[0], returns.index[kelly.MIN_FIT_DAYS])   # nothing before the first fit's days
        actual = pd.Series(0.001, index=returns.index[150:])
        result = kelly.compare_with_book(actual, closes, ["AAA", "BBB", "CCC"])
        self.assertEqual(list(result["lines"].columns), ["Desk (actual)", "Kelly (walk-forward)", "Kelly (in-sample)", "BTC"])
        self.assertEqual(list(result["table"]["Line"]), list(result["lines"].columns))


if __name__ == "__main__":
    unittest.main()
