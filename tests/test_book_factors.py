import unittest

import numpy as np
import pandas as pd

from desk import book_factors


class BookFactorTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        days = pd.date_range("2026-01-01", periods=300, freq="D", tz="UTC")
        self.factors = pd.DataFrame({"BTC": rng.normal(0, 0.03, 300), "Alts - BTC": rng.normal(0, 0.02, 300)},
                                    index=days)
        noise = pd.Series(rng.normal(0, 0.002, 300), index=days)
        self.days = days
        self.noise = noise

    def test_recovers_betas_and_a_real_alpha_out_of_sample(self):
        book = 0.003 + 0.5 * self.factors["BTC"] - 1.0 * self.factors["Alts - BTC"] + self.noise
        betas, check, rolling = book_factors.model_tables(book, self.factors, 365)
        self.assertAlmostEqual(betas.loc["BTC", "Beta"], 0.5, delta=0.02)
        self.assertAlmostEqual(betas.loc["Alts - BTC", "Beta"], -1.0, delta=0.03)
        self.assertAlmostEqual(betas.loc["Alpha (/yr)", "Beta"], 0.003 * 365, delta=0.1)
        self.assertEqual(check.iloc[-1]["Verdict"], "alpha established")
        self.assertFalse(rolling.empty)

    def test_no_alpha_is_not_called_alpha(self):
        book = 0.5 * self.factors["BTC"] + self.noise
        _, check, _ = book_factors.model_tables(book, self.factors, 365)
        self.assertNotEqual(check.iloc[-1]["Verdict"], "alpha established")


if __name__ == "__main__":
    unittest.main()
