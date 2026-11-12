"""The signal harness: point-in-time universe, stops, costs, and validation that only passes real signals."""

import unittest

import numpy as np
import pandas as pd

from desk.hl import signal_research as research
from desk import screener

DAYS = pd.date_range("2025-01-01", periods=330, freq="D", tz="UTC")


def panel(n=30, predictive=True, seed=3):
    """Coins whose next-week return follows their last week's (when ``predictive``), around a random BTC."""
    rng = np.random.default_rng(seed)
    btc = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(DAYS)))
    out = {"BTC": pd.DataFrame({"open": btc, "high": btc * 1.005, "low": btc * 0.995, "close": btc, "volume": 1e6}, index=DAYS)}
    for i in range(n):
        drift = np.zeros(len(DAYS))
        noise = rng.normal(0, 0.01, len(DAYS))
        if predictive:  # persistent regimes: each coin trends up or down for 20 days at a time
            regime = np.repeat(rng.choice([-0.004, 0.004], len(DAYS) // 20 + 1), 20)[: len(DAYS)]
            drift = regime
        price = 10 * np.cumprod(1 + drift + noise) * btc / 100
        out[f"C{i}"] = pd.DataFrame({"open": price, "high": price * 1.004, "low": price * 0.996, "close": price,
                                     "volume": 5e6 / price}, index=DAYS)
    return out


class UniverseTests(unittest.TestCase):
    def test_the_universe_is_the_most_liquid_with_history_and_never_btc(self):
        data = research.frames(panel(10, predictive=False))
        data["dollar_volume"]["C0"] = 1.0                  # too illiquid
        tradable = research.universe(data["close"], data["dollar_volume"], size=5)
        self.assertNotIn("BTC", tradable.columns)            # the benchmark is never a candidate
        self.assertFalse(tradable["C0"].any())
        self.assertFalse(tradable.iloc[:research.MIN_HISTORY_DAYS - 1].any().any())   # no names before 60 days of history
        self.assertTrue((tradable.iloc[-1].sum()) <= 5)


class PositionReturnTests(unittest.TestCase):
    def test_a_short_is_stopped_at_three_atr_and_pays_costs(self):
        days = pd.date_range("2025-01-01", periods=40, freq="D", tz="UTC")
        close = pd.Series(100.0, index=days)
        high, low = close * 1.01, close * 0.99
        high.iloc[30] = 110.0                               # a spike past the stop on the day after entry
        data = {"close": close.to_frame("X"), "high": high.to_frame("X"), "low": low.to_frame("X")}
        long_return, short_return = research.position_returns(data, hold=7)
        stop = 3 * research.atr(data["high"], data["low"], data["close"]).loc[days[29], "X"] / 100
        self.assertAlmostEqual(short_return.loc[days[29], "X"], -stop - 2 * research.COST_PER_SIDE)
        self.assertAlmostEqual(long_return.loc[days[20], "X"], -2 * research.COST_PER_SIDE)   # flat week: costs only
        self.assertTrue(np.isnan(long_return.loc[days[5], "X"]))   # before 14 days there is no ATR, so no stop: no trade


class ValidationTests(unittest.TestCase):
    def run_signal(self, predictive):
        data = research.frames(panel(30, predictive))
        tradable = research.universe(data["close"], data["dollar_volume"], size=30)
        long_return, short_return = research.position_returns(data)
        btc_forward = data["close"]["BTC"].shift(-7) / data["close"]["BTC"] - 1
        return research.test_signal(research.signal_frames(data)["rel7"], tradable, long_return, short_return, btc_forward)

    def test_a_real_signal_validates(self):
        result = self.run_signal(True)
        self.assertTrue(result["validated"], result)
        self.assertGreater(result["spread_week"], 0)

    def test_noise_does_not(self):
        for seed in range(3):
            data = research.frames(panel(30, False, seed=seed))
            tradable = research.universe(data["close"], data["dollar_volume"], size=30)
            lr, sr = research.position_returns(data)
            btc_forward = data["close"]["BTC"].shift(-7) / data["close"]["BTC"] - 1
            result = research.test_signal(research.signal_frames(data)["rel7"], tradable, lr, sr, btc_forward)
            self.assertFalse(result["validated"], (seed, result))


if __name__ == "__main__":
    unittest.main()
