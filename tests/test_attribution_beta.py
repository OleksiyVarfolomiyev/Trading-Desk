"""The beta-weighted attribution experiment (research/attribution_beta_test.py)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import attribution_beta_test as beta_test  # noqa: E402

DAYS = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")


def synthetic_book(seed=1):
    """One long and one short market over 120 days against an index; the short has beta 1.5, the long 0.5."""
    rng = np.random.default_rng(seed)
    index = pd.Series(rng.normal(0.0005, 0.02, len(DAYS)), index=DAYS)
    closes = 100 * (1 + index).cumprod()
    returns = {"AAA": 0.5 * index + rng.normal(0, 0.01, len(DAYS)), "BBB": 1.5 * index + rng.normal(0, 0.01, len(DAYS))}
    frames, equity = [], pd.Series(1000.0, index=DAYS)
    for asset, side in (("AAA", 1.0), ("BBB", -1.0)):
        value = pd.Series(side * 300.0, index=DAYS)
        price = value.shift(1).fillna(0.0) * returns[asset]
        frames.append(pd.DataFrame({"day": DAYS, "kind": "perp", "asset": asset, "price": price.values, "trading": 0.0,
                                    "fees": -0.01, "funding": 0.0, "value": value.values, "account": "A", "venue": "hl",
                                    "book": "Crypto"}))
    coins = pd.concat(frames, ignore_index=True)
    coins["pnl"] = coins["price"] + coins["trading"] + coins["fees"] + coins["funding"]
    pnl = coins.groupby("day")["pnl"].sum().reindex(DAYS)
    book = SimpleNamespace(coins=coins, days=DAYS, equity=equity, benchmark="BTC", kind="Crypto", benchmark_closes=closes)
    day_returns = (pnl / equity.shift(1)).dropna()
    asset_closes = {name: 100 * (1 + series).cumprod() for name, series in returns.items()}
    return book, day_returns, asset_closes


def betas_of(book, asset_closes, shrink=False):
    return pd.DataFrame({name: beta_test.rolling_beta(closes, book.benchmark_closes, book.days, shrink)
                         for name, closes in asset_closes.items()}, index=book.days)


class BetaAttributionTests(unittest.TestCase):
    def setUp(self):
        self.book, self.returns, self.closes = synthetic_book()
        self.betas = betas_of(self.book, self.closes)

    def test_beta_one_everywhere_is_the_dollar_version(self):
        ones = pd.DataFrame(1.0, index=self.book.days, columns=["AAA", "BBB"])
        dollar = beta_test.attribute(self.book, self.returns, None)["All"]
        same = beta_test.attribute(self.book, self.returns, ones)["All"]
        for part, value in dollar["parts"].items():
            self.assertAlmostEqual(same["parts"][part], value, places=10, msg=part)

    def test_parts_add_up_to_excess_return(self):
        # The telescoping gives parts that sum to the compounded excess return, whatever the weights.
        for betas in (None, self.betas):
            result = beta_test.attribute(self.book, self.returns, betas)["All"]
            self.assertAlmostEqual(sum(result["parts"].values()), result["excess"], places=10)

    def test_unattributed_does_not_depend_on_the_betas(self):
        dollar = beta_test.attribute(self.book, self.returns, None)["All"]["parts"]["Unattributed"]
        weighted = beta_test.attribute(self.book, self.returns, self.betas)["All"]["parts"]["Unattributed"]
        self.assertAlmostEqual(dollar, weighted, places=10)

    def test_beta_weights_move_picks_into_market_exposure(self):
        # Short the beta-1.5 name, long the beta-0.5 name: net dollars are flat but net beta is short, so the beta
        # version sees a market bet that the dollar version leaves in picks.
        dollar = beta_test.attribute(self.book, self.returns, None)["All"]["parts"]
        weighted = beta_test.attribute(self.book, self.returns, self.betas)["All"]["parts"]
        self.assertGreater(abs(weighted["Market exposure"] - dollar["Market exposure"]), 1e-4)
        # What leaves picks and sizing together arrives in exposure and timing: the five attributed lines are conserved.
        keep = ["Picks", "Sizing", "Timing", "Market exposure"]
        self.assertAlmostEqual(sum(dollar[k] for k in keep), sum(weighted[k] for k in keep), places=6)

    def test_beta_estimates_use_only_earlier_returns(self):
        # Changing the last day's asset price cannot change any beta used on or before that day.
        changed = {name: closes.copy() for name, closes in self.closes.items()}
        changed["AAA"].iloc[-1] *= 3
        before = betas_of(self.book, self.closes)["AAA"]
        after = betas_of(self.book, changed)["AAA"]
        pd.testing.assert_series_equal(before.iloc[:-1], after.iloc[:-1])
        self.assertTrue(before.iloc[beta_test.BETA_MIN_OBS + 3:].notna().all())

    def test_betas_recover_the_planted_values(self):
        late = self.betas.iloc[-1]
        self.assertAlmostEqual(late["AAA"], 0.5, delta=0.15)
        self.assertAlmostEqual(late["BBB"], 1.5, delta=0.15)

    def test_a_market_with_too_little_history_has_no_beta(self):
        short = {"AAA": self.closes["AAA"].iloc[:10]}
        beta = beta_test.rolling_beta(short["AAA"], self.book.benchmark_closes, self.book.days)
        self.assertTrue(beta.isna().all())

    def test_halves_cover_the_period(self):
        table = beta_test.halves(self.book, self.returns, self.betas)
        self.assertEqual(set(table["Period"]), {"first half", "second half"})
        self.assertEqual(set(table["Weights"]), {"Dollar", "Beta"})

    def test_beta_picks_match_the_beta_weighted_attribution(self):
        from unittest import mock

        from desk import beta_attribution

        with mock.patch.object(beta_attribution, "asset_closes_for", lambda book, asset, venue, lookback: self.closes[asset]):
            picks = beta_attribution.beta_picks(self.book, self.returns)
        expected = beta_test.attribute(self.book, self.returns, self.betas)["All"]["parts"]["Picks"]
        self.assertAlmostEqual(picks["All"], expected, places=10)
        self.assertNotAlmostEqual(picks["All"], beta_test.attribute(self.book, self.returns, None)["All"]["parts"]["Picks"], places=4)


if __name__ == "__main__":
    unittest.main()
