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


class ModelsAndAttribution(unittest.TestCase):
    def data(self):
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(3)
        index = pd.date_range("2026-01-01", periods=200, tz="UTC")
        factors = pd.DataFrame({"BTC": 0.02 * rng.standard_normal(200), "Alts - BTC": 0.01 * rng.standard_normal(200)}, index=index)
        book = 0.8 * factors["BTC"] - 0.5 * factors["Alts - BTC"] + 0.001 + 0.002 * rng.standard_normal(200)
        return book, factors

    def test_attribution_adds_up_and_comparison_lists_each_model(self):
        from desk import book_factors

        book, factors = self.data()
        parts = book_factors.attribution(book, factors, 365)
        self.assertTrue(((parts[["BTC", "Alts - BTC", "Specific"]].sum(axis=1) - parts["Total"]).abs() < 1e-12).all())
        cumulative = book_factors.cumulative_attribution(parts)
        self.assertAlmostEqual(cumulative["Factors"].iloc[-1] + cumulative["Specific"].iloc[-1], cumulative["Total"].iloc[-1])
        table = book_factors.model_comparison(book, factors, 365, {"Market": ["BTC"], "Market + alts": ["BTC", "Alts - BTC"]})
        self.assertEqual(list(table["Model"]), ["Market", "Market + alts"])
        self.assertGreater(table["R²"].iloc[1], table["R²"].iloc[0])   # the alt factor explains more of this book
