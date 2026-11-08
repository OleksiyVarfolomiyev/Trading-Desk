import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

# Modules live in the repository root and its execution/ and research/ folders.
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "execution"), str(ROOT / "research")]
# Keep tests off real accounts: no IB or wallet in accounts.json is ever contacted.
os.environ["DESK_ACCOUNTS_FILE"] = str(ROOT / "tests" / "no_accounts.json")
# IB records from tests go to a throwaway folder, never the real one next to the repository.
os.environ["DESK_IB_DATA_DIR"] = __import__("tempfile").mkdtemp(prefix="desk-ib-")

from factor_model import (
    alpha_verdict,
    btc_momentum_factor,
    btc_realized_vol,
    fit_factor_regression,
    fit_regime_interaction,
    independent_factors,
    rolling_betas,
    split_in_half,
)
from desk.hl import book_exposure


class FactorModelTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.date_range("2026-01-01", periods=200, freq="D", tz="UTC")
        self.rng = np.random.default_rng(7)

    def test_regression_recovers_known_alpha_and_beta(self):
        market = pd.Series(self.rng.normal(0, 0.03, 200), index=self.dates)
        portfolio = 0.002 - 0.8 * market
        fit = fit_factor_regression(portfolio, market.to_frame("BTC"))
        self.assertAlmostEqual(fit.alpha, 0.002)
        self.assertAlmostEqual(fit.betas["BTC"], -0.8)
        self.assertEqual(fit.observations, 200)

    def test_state_factor_keeps_alpha_at_its_average_level(self):
        state = pd.Series(self.rng.normal(50, 10, 200), index=self.dates)
        portfolio = 0.002 + 0.0001 * (state - state.mean())
        fit = fit_factor_regression(portfolio, state.to_frame("Vol"), state_factors=("Vol",))
        self.assertAlmostEqual(fit.alpha, 0.002)
        self.assertAlmostEqual(fit.betas["Vol"], 0.0001 * state.std())

    def test_momentum_and_vol_do_not_use_the_same_day_return(self):
        returns = pd.Series(self.rng.normal(0, 0.02, 200), index=self.dates)
        changed = returns.copy()
        changed.iloc[100] = -0.5
        for build in (lambda r: btc_momentum_factor(r, 0.0, 30) / r, lambda r: btc_realized_vol(r, 30)):
            pd.testing.assert_series_equal(build(returns).iloc[:101], build(changed).iloc[:101], check_names=False)

    def test_momentum_follows_the_trailing_sign(self):
        returns = pd.Series(0.01, index=self.dates)
        returns.iloc[100:] = -0.01
        factor = btc_momentum_factor(returns, 0.0, 30)
        self.assertTrue(np.isnan(factor.iloc[29]))
        self.assertAlmostEqual(factor.iloc[50], 0.01)
        self.assertAlmostEqual(factor.iloc[100], -0.01)
        self.assertAlmostEqual(factor.iloc[150], 0.01)

    def test_independent_factors_skip_a_duplicate(self):
        market = pd.Series(self.rng.normal(0, 0.03, 200), index=self.dates)
        other = pd.Series(self.rng.normal(0, 0.03, 200), index=self.dates)
        frame = pd.DataFrame({"BTC": market, "Momentum": market, "Other": other})
        self.assertEqual(independent_factors(frame), ["BTC", "Other"])

    def test_split_keeps_the_earlier_half_first(self):
        first, second = split_in_half(self.dates[::-1])
        self.assertLess(first.max(), second.min())
        self.assertEqual(len(first) + len(second), 200)

    def test_verdict_needs_positive_and_significant_alpha(self):
        self.assertTrue(alpha_verdict(-0.1, 3.0).startswith("does not survive"))
        self.assertTrue(alpha_verdict(0.1, 1.0).startswith("not established"))
        self.assertTrue(alpha_verdict(0.1, 2.5).startswith("survives"))


class RegimeBetaTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.dates = pd.date_range("2026-01-04", periods=120, freq="W-SUN", tz="UTC")
        self.factor = pd.DataFrame({"BTC": rng.normal(0, 0.05, 120), "Alt": rng.normal(0, 0.04, 120)}, index=self.dates)
        self.regimes = pd.Series(["Long"] * 70 + ["Short"] * 50, index=self.dates)
        betas = np.where((self.regimes == "Long").to_numpy()[:, None], [0.8, 0.5], [-0.6, -0.4])
        self.book = pd.Series((self.factor.to_numpy() * betas).sum(axis=1) + rng.normal(0, 0.002, 120),
                              index=self.dates)

    def test_interaction_recovers_each_regimes_betas_and_flags_the_change(self):
        table, observations = fit_regime_interaction(self.book, self.factor, self.regimes)
        self.assertEqual(observations, 120)
        estimate = table.set_index(["Regime", "Term"])["Estimate"]
        self.assertAlmostEqual(estimate[("Long", "BTC")], 0.8, delta=0.03)
        self.assertAlmostEqual(estimate[("Short", "BTC")], -0.6, delta=0.03)
        self.assertAlmostEqual(estimate[("Short", "Alt")], -0.4, delta=0.03)
        change = table.set_index(["Regime", "Term"])["Difference t-stat"]
        self.assertTrue(np.isnan(change[("Long", "BTC")]))
        self.assertLess(change[("Short", "BTC")], -10)
        # The pooled beta describes neither regime.
        pooled = fit_factor_regression(self.book, self.factor).betas["BTC"]
        self.assertTrue(-0.6 + 0.2 < pooled < 0.8 - 0.2)

    def test_rolling_betas_show_the_sign_flip(self):
        rolling = rolling_betas(self.book, self.factor, window=12, min_periods=8)
        self.assertGreater(rolling.loc[self.dates[60], "BTC"], 0.7)
        self.assertLess(rolling.iloc[-1]["BTC"], -0.5)
        self.assertNotIn(self.dates[3], rolling.index)


class BookExposureTests(unittest.TestCase):
    wallet = "0xAbC0000000000000000000000000000000000001"

    def ms(self, day):
        return int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)

    def test_perp_path_counts_a_position_opened_before_the_fills(self):
        fills = [
            {"coin": "ETH", "side": "A", "sz": "1", "startPosition": "2", "time": self.ms("2026-01-03 12:00")},
            {"coin": "ETH", "side": "A", "sz": "3", "startPosition": "1", "time": self.ms("2026-01-05 12:00")},
        ]
        path = book_exposure.perp_position_path(fills, self.wallet)
        days = pd.date_range("2026-01-01", periods=6, freq="D", tz="UTC")
        held = book_exposure.daily_holdings(path, days)[("perp", "ETH")]
        self.assertEqual(held.tolist(), [2.0, 2.0, 1.0, 1.0, -2.0, -2.0])

    def test_spot_balances_are_rebuilt_backward_from_today(self):
        me = self.wallet.lower()
        fills = [{"coin": "@107", "side": "B", "sz": "4", "fee": "0.01", "feeToken": "HYPE",
                  "time": self.ms("2026-01-04 12:00")}]
        ledger = [
            {"time": self.ms("2026-01-02 12:00"),
             "delta": {"type": "spotTransfer", "token": "HYPE", "amount": "1", "user": me, "destination": "0x2"}},
            {"time": self.ms("2026-01-05 12:00"), "delta": {"type": "cStakingTransfer", "token": "HYPE", "amount": "3"}},
            {"time": self.ms("2026-01-05 12:00"),
             "delta": {"type": "send", "token": "USDC", "amount": "50", "user": "0x9", "destination": me}},
        ]
        path = book_exposure.spot_balance_path(self.wallet, fills, ledger, {"HYPE": 5.0, "USDC": 70.0},
                                               lambda coin: "HYPE")
        days = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
        held = book_exposure.daily_holdings(path, days)[("spot", "HYPE")]
        self.assertEqual([round(v, 2) for v in held], [2.01, 1.01, 1.01, 5.0, 5.0])
        self.assertNotIn("USDC", set(path["asset"]))

    def test_exposure_splits_legs_and_scales_by_equity(self):
        days = pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC")
        holdings = pd.DataFrame({("perp", "BTC"): [0.01, 0.01], ("perp", "SOL"): [-2.0, -2.0],
                                 ("perp", "xyz:AAPL"): [1.0, 1.0], ("spot", "UETH"): [0.1, 0.1]}, index=days)
        holdings.columns = pd.MultiIndex.from_tuples(holdings.columns)
        closes = pd.DataFrame({"BTC": 100_000.0, "SOL": 150.0, "xyz:AAPL": 200.0, "ETH": 3_000.0}, index=days)
        group = lambda name: "Perp TradFi" if name.startswith("xyz:") else "Perp Crypto"
        out = book_exposure.exposure_by_leg(holdings, closes, pd.Series(1_000.0, index=days), group)
        row = out.iloc[0]
        self.assertAlmostEqual(row["Net BTC"], 1.0)
        self.assertAlmostEqual(row["Net Alts"], (-300 + 300) / 1_000)
        self.assertAlmostEqual(row["Gross Alts"], 0.6)
        self.assertAlmostEqual(row["Net TradFi"], 0.2)

    def test_regimes_are_contiguous_and_short_detours_fold_in(self):
        weeks = pd.date_range("2026-01-04", periods=16, freq="W-SUN", tz="UTC")
        alts = [0.4] * 7 + [1.3] + [0.4] * 2 + [0.0, 0.2, -0.6, -1.1, -1.2, -1.3]
        weekly = pd.DataFrame({"Net BTC": 0.0, "Net Alts": alts, "Net TradFi": 0.0}, index=weeks)
        weekly["Gross total"] = weekly["Net Alts"].abs()
        regimes = book_exposure.label_regimes(weekly, min_run=6)
        self.assertEqual(regimes.iloc[:12].nunique(), 1)  # through the last week back at long alts
        self.assertTrue(regimes.iloc[0].startswith("Long alts ("))
        self.assertTrue(regimes.iloc[-1].startswith("Short alts, levered ("))
        self.assertEqual(regimes.iloc[12:].nunique(), 1)
        # Named from the regime's average gross (1.05x), not from its latest week alone.
        weekly.loc[weeks[-1], ["Net Alts", "Gross total"]] = [-0.5, 0.5]
        self.assertTrue(book_exposure.label_regimes(weekly, min_run=6).iloc[-1].startswith("Short alts ("))


if __name__ == "__main__":
    unittest.main()
