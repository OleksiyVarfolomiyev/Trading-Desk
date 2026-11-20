"""What drove excess return: the four columns over all matched sessions (ALL) and over each market direction
(desk/benchmark_regimes.py)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import benchmark_regimes  # noqa: E402

DAYS = pd.date_range("2026-09-01", "2026-11-18", freq="D", tz="UTC")
COLUMNS = ("crypto", "stocks", "desk_btc", "desk_spy")


def synthetic_book(kind, benchmark, seed):
    """One long and one short market against a benchmark, each 300 of 1,000 equity, with small fees."""
    rng = np.random.default_rng(seed)
    index = pd.Series(rng.normal(0.0005, 0.02, len(DAYS)), index=DAYS)
    closes = 100 * (1 + index).cumprod()
    frames, equity = [], pd.Series(1000.0, index=DAYS)
    for asset, side, beta in (("AAA", 1.0, 0.6), ("BBB", -1.0, 1.4)):
        value = pd.Series(side * 300.0, index=DAYS)
        price = value.shift(1).fillna(0.0) * (beta * index + rng.normal(0, 0.01, len(DAYS)))
        frames.append(pd.DataFrame({"day": DAYS, "kind": "perp", "asset": asset, "price": price.values, "trading": 0.0,
                                    "fees": -0.02, "funding": 0.01, "value": value.values, "account": "A", "venue": "hl",
                                    "book": kind}))
    coins = pd.concat(frames, ignore_index=True)
    coins["pnl"] = coins["price"] + coins["trading"] + coins["fees"] + coins["funding"]
    pnl = coins.groupby("day")["pnl"].sum().reindex(DAYS).fillna(0.0)
    returns = (pnl / equity.shift(1)).dropna()
    return SimpleNamespace(coins=coins, days=DAYS, equity=equity, benchmark=benchmark, kind=kind, benchmark_closes=closes,
                           pnl=pnl, returns=returns)


def synthetic_books():
    crypto, stocks = synthetic_book("Crypto", "BTC", 1), synthetic_book("Stocks", "SPY", 2)
    desk_return = (crypto.pnl + stocks.pnl) / (crypto.equity + stocks.equity).shift(1)
    stocks.desk = SimpleNamespace(index=1000 * (1 + desk_return.fillna(0.0)).cumprod())
    return {"crypto": crypto, "stocks": stocks}


class RegimeAttributionTests(unittest.TestCase):
    def setUp(self):
        self.result, self.dates = benchmark_regimes.benchmark_regimes(synthetic_books())

    def test_every_window_has_all_four_columns_for_all_sessions(self):
        self.assertTrue(self.result)
        for window, groups in self.result.items():
            self.assertIn("all", groups, window)
            self.assertEqual(set(groups["all"]["comparisons"]), set(COLUMNS), window)

    def test_each_column_ties_out_over_all_sessions_and_each_direction(self):
        # The parts, with Unattributed, add up to the compounded excess return, for ALL and for every direction.
        for window, groups in self.result.items():
            for regime, entry in groups.items():
                for key, column in entry["comparisons"].items():
                    excess = column["portfolio"] - column["index"]
                    self.assertAlmostEqual(sum(column["parts"].values()), excess, places=10, msg=f"{window} {regime} {key}")

    def test_all_covers_the_sessions_of_the_four_directions(self):
        for window, groups in self.result.items():
            directions = sum(entry["sessions"] for regime, entry in groups.items() if regime != "all")
            self.assertEqual(groups["all"]["sessions"], directions, window)
            union = set().union(*(days for regime, days in self.dates[window].items() if regime != "all"))
            self.assertEqual(self.dates[window]["all"], union, window)

    def test_the_four_columns_of_a_window_share_start_and_end(self):
        for window, groups in self.result.items():
            entry = groups["all"]
            self.assertLessEqual(entry["start"], entry["end"], window)
            for key, column in entry["comparisons"].items():
                self.assertEqual((column["start"], column["end"]), (entry["start"], entry["end"]), f"{window} {key}")

    def test_a_replaced_mark_function_changes_only_the_crypto_column(self):
        books = synthetic_books()

        def doubled_price(book):   # a stand-in for the beta-weighted marks: the crypto Picks inputs differ
            frame = benchmark_regimes.decision_marks(book).copy()
            frame["equal_price"] = frame["equal_price"] * 2
            return frame

        other, _ = benchmark_regimes.benchmark_regimes(books, mark_functions={"crypto": doubled_price})
        for window, groups in self.result.items():
            plain, changed = groups["all"]["comparisons"], other[window]["all"]["comparisons"]
            self.assertNotAlmostEqual(plain["crypto"]["parts"]["Picks"], changed["crypto"]["parts"]["Picks"], places=8)
            self.assertAlmostEqual(plain["stocks"]["parts"]["Picks"], changed["stocks"]["parts"]["Picks"], places=12)
            # The excess return and Unattributed absorb nothing else: the column still ties out.
            self.assertAlmostEqual(sum(changed["crypto"]["parts"].values()),
                                   changed["crypto"]["portfolio"] - changed["crypto"]["index"], places=10)


if __name__ == "__main__":
    unittest.main()
