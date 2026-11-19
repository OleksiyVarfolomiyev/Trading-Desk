import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "execution"), str(ROOT / "research")]
os.environ["DESK_ACCOUNTS_FILE"] = str(ROOT / "tests" / "no_accounts.json")
os.environ["DESK_IB_DATA_DIR"] = __import__("tempfile").mkdtemp(prefix="desk-ib-")

from desk import factor_risk as fr


def simulated(days=400, names=40, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", periods=days, freq="D")
    cols = [f"C{i}" for i in range(names)]
    caps = pd.Series(np.exp(rng.normal(20, 1, names)), index=cols)
    market = pd.Series(rng.normal(0, 0.03, days), index=index)
    beta = pd.Series(rng.uniform(0.5, 1.5, names), index=cols)
    noise = pd.DataFrame(rng.normal(0, 0.02, (days, names)), index=index, columns=cols)
    returns = noise + np.outer(market, beta)
    return returns, caps, market


class FactorRiskTests(unittest.TestCase):
    def test_point_in_time_size_matches_static_when_it_never_changes(self):
        returns, caps, market = simulated(days=200)
        frame = pd.DataFrame([caps.to_numpy()] * len(returns), index=returns.index, columns=caps.index)
        static = fr.estimate(returns, caps, market)
        moving = fr.estimate(returns, frame, market)
        pd.testing.assert_frame_equal(static["factors"], moving["factors"])

    def test_point_in_time_size_skips_names_without_a_size_that_day(self):
        returns, caps, market = simulated(days=200)
        frame = pd.DataFrame([caps.to_numpy()] * len(returns), index=returns.index, columns=caps.index)
        frame.loc[frame.index[150]:, "C0"] = np.nan
        fit = fr.estimate(returns, frame, market)
        self.assertNotIn("C0", fit["designs"][returns.index[160]].index)
        self.assertIn("C0", fit["designs"][returns.index[100]].index)

    def test_standardize_is_cap_weighted_mean_zero_and_unit_sd(self):
        rng = np.random.default_rng(0)
        values = pd.Series(rng.normal(5, 2, 30)); weights = pd.Series(rng.uniform(1, 9, 30))
        z = fr.standardize(values, weights)
        self.assertAlmostEqual(float((z * weights / weights.sum()).sum()), 0, places=9)
        self.assertAlmostEqual(float(z.std(ddof=0)), 1, places=9)
        self.assertEqual(float(fr.standardize(pd.Series([1.0, 1.0, 1.0, 1.0]), pd.Series([1, 2, 3, 4.0])).abs().sum()), 0)

    def test_exposures_never_use_the_day_they_explain(self):
        returns, caps, market = simulated(days=120)
        date = returns.index[100]
        before = fr.raw_exposures(returns, caps, market)
        changed = returns.copy(); changed.loc[date] += 0.5
        after = fr.raw_exposures(changed, caps, market.where(market.index != date, market + 0.5))
        for name in ("Momentum", "Reversal", "Volatility", "Beta"):
            pd.testing.assert_series_equal(before[name].loc[date], after[name].loc[date])

    def test_market_factor_return_tracks_the_common_move(self):
        returns, caps, market = simulated()
        fit = fr.estimate(returns, caps, market)
        joined = pd.concat([fit["factors"]["Market"], market], axis=1, sort=True).dropna()
        self.assertGreater(joined.corr().iloc[0, 1], 0.9)
        self.assertGreaterEqual(fit["factors"].shape[0], 300)

    def test_factor_covariance_is_symmetric_positive_definite(self):
        returns, caps, market = simulated()
        cov = fr.factor_covariance(fr.estimate(returns, caps, market)["factors"])
        self.assertTrue(np.allclose(cov, cov.T))
        self.assertGreater(np.linalg.eigvalsh(cov.to_numpy()).min(), 0)

    def test_portfolio_variance_splits_into_factors_and_specific(self):
        returns, caps, market = simulated()
        fit = fr.estimate(returns, caps, market)
        cov, spec = fr.model_at(fit, len(fit["factors"]) - 2)
        date = fit["factors"].index[-1]
        w = pd.Series(0.05, index=returns.columns[:20])
        risk = fr.portfolio_risk(w, fit["designs"][date], cov, spec)
        self.assertAlmostEqual(risk["variance"], float(risk["factor_variance"].sum()) + risk["specific_variance"], places=12)
        self.assertAlmostEqual(risk["vol"], np.sqrt(risk["variance"]), places=12)
        self.assertGreater(risk["vol"], 0)

    def test_specific_variance_is_shrunk_floored_and_needs_history(self):
        rng = np.random.default_rng(1)
        spec = pd.DataFrame(rng.normal(0, 0.02, (200, 9)), columns=list("abcdefghi"))
        spec["a"] *= 20
        spec.loc[:150, "i"] = np.nan   # 49 observations: enough
        spec.loc[:190, "h"] = np.nan   # 9 observations: dropped
        caps = pd.Series(np.arange(1, 10) * 1e6, index=list("abcdefghi"))
        out = fr.specific_variance(spec, caps)
        self.assertNotIn("h", out.index)
        own_a = float((spec["a"] ** 2).mean())
        self.assertLess(out["a"], own_a)   # pulled toward its bucket's median


class SectorBlockTests(unittest.TestCase):
    def sectors(self, names=40):
        return pd.Series([("A", "B", "C")[i % 3] for i in range(names)], index=[f"C{i}" for i in range(names)])

    def test_sector_slopes_are_cap_weighted_to_zero_each_day(self):
        returns, caps, market = simulated(days=200)
        sectors = self.sectors()
        fit = fr.estimate(returns, caps, market, sectors=sectors)
        for date in fit["factors"].index[::20]:
            design = fit["designs"][date]
            weight = {c: (design[c] * caps.reindex(design.index)).sum() for c in design if c.startswith(fr.SECTOR_PREFIX)}
            slopes = fit["factors"].loc[date]
            self.assertAlmostEqual(sum(weight[c] * slopes[c] for c in weight) / sum(weight.values()), 0.0, places=9)

    def test_sector_model_recovers_a_planted_sector_move(self):
        returns, caps, market = simulated(days=200)
        sectors = self.sectors()
        move = pd.Series(np.random.default_rng(5).normal(0, 0.05, len(returns)), index=returns.index)
        returns = returns.copy()
        for name in sectors.index[sectors == "A"]:
            returns[name] += move
        fit = fr.estimate(returns, caps, market, styles=(), sectors=sectors)   # styles would pick up the planted move's momentum
        gap = fit["factors"][fr.SECTOR_PREFIX + "A"] - fit["factors"][fr.SECTOR_PREFIX + "B"]   # the market column absorbs part of A's own move
        self.assertGreater(gap.corr(move), 0.95)

    def test_thin_sector_has_no_exposure_and_the_fit_still_works(self):
        returns, caps, market = simulated(days=120)
        sectors = self.sectors()
        sectors.iloc[:2] = "Tiny"   # two names: below MIN_SECTOR
        fit = fr.estimate(returns, caps, market, sectors=sectors)
        design = fit["designs"][fit["factors"].index[-1]]
        self.assertEqual(float(design[fr.SECTOR_PREFIX + "Tiny"].sum()), 0.0)
        self.assertFalse(fit["factors"].drop(columns=fr.SECTOR_PREFIX + "Tiny").isna().any().any())

    def test_sector_file_covers_the_panel_coins_with_known_sectors(self):
        sectors = fr.load_sectors()
        self.assertIn("BTC", sectors.index)
        self.assertTrue(sectors.notna().all())
        self.assertGreaterEqual(sectors.value_counts().min(), fr.MIN_SECTOR)


if __name__ == "__main__":
    unittest.main()
