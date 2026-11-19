import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from desk import stock_risk as sk

SAMPLE = """Header text
more text

,Mkt-RF,SMB,HML,RMW,CMA,RF
20240102,   -0.67,    0.50,   -0.10,    0.20,    0.30,    0.01
20240103,    1.00,   -0.20,    0.40,    0.00,   -0.10,    0.01

Copyright
"""


def simulated(days=300, names=10, seed=1):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-01", periods=days)
    factors = pd.DataFrame(rng.normal(0, 0.01, (days, len(sk.FACTORS))), index=index, columns=sk.FACTORS)
    true = rng.normal(0.5, 0.5, (names, len(sk.FACTORS)))
    noise = rng.normal(0, 0.01, (days, names))
    excess = pd.DataFrame(factors.to_numpy() @ true.T + noise, index=index, columns=[f"S{i}" for i in range(names)])
    return excess, factors, true


class StockRiskTests(unittest.TestCase):
    def test_french_file_parses_to_decimals(self):
        frame = sk.parse_french(SAMPLE)
        self.assertEqual(list(frame.columns), ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])
        self.assertEqual(len(frame), 2)
        self.assertAlmostEqual(frame.loc["2024-01-02", "Mkt-RF"], -0.0067)

    def test_cached_files_load_with_every_factor(self):
        factors = sk.load_factors()
        self.assertTrue(set(sk.FACTORS) <= set(factors.columns))
        self.assertGreater(len(factors), 5000)

    def test_exposures_recover_the_planted_betas(self):
        excess, factors, true = simulated()
        betas, spec = sk.exposures(excess, factors)
        self.assertLess(np.abs(betas.to_numpy() - true).max(), 0.2)
        self.assertAlmostEqual(float(spec.mean()), 0.01**2, delta=0.00003)

    def test_model_uses_only_rows_up_to_the_position(self):
        excess, factors, _ = simulated()
        a = sk.model_at(excess, factors, 200)
        changed = excess.copy()
        changed.iloc[201:] = 9.0
        b = sk.model_at(changed, factors, 200)
        pd.testing.assert_frame_equal(a[0], b[0])
        pd.testing.assert_series_equal(a[2], b[2])
        self.assertIsNone(sk.model_at(excess, factors, 50))

    def test_portfolio_risk_splits_factor_and_specific(self):
        excess, factors, _ = simulated()
        betas, cov, spec = sk.model_at(excess, factors, 280)
        w = pd.Series(1.0 / len(betas), index=betas.index)
        risk = sk.portfolio_risk(w, betas, cov, spec)
        self.assertAlmostEqual(risk["vol"] ** 2, risk["factor_variance"] + risk["specific_variance"], places=12)
        self.assertGreater(risk["factor_share"], 0.5)
        self.assertAlmostEqual(risk["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
