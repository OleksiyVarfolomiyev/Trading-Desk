"""Regime signals from daily closes: an alt season and an equity sell-off are read, a quiet market is not."""

import unittest

import numpy as np
import pandas as pd

from desk import market_regime as m

DAYS = pd.date_range("2025-01-01", periods=400, freq="D", tz="UTC")


class AltSeasonTests(unittest.TestCase):
    def frame(self, burst):
        rng = np.random.default_rng(1)
        btc = pd.Series(np.cumprod(1 + rng.normal(0, 0.01, len(DAYS))) * 100, index=DAYS)
        noise = rng.normal(0, 0.02, (len(DAYS), 20))
        daily = noise + btc.pct_change().fillna(0).to_numpy()[:, None]
        if burst:
            daily[-35:] += 0.02  # every alt beats BTC by 2% a day for five weeks
        alts = pd.DataFrame(np.cumprod(1 + daily, axis=0) * 10, index=DAYS, columns=[f"A{i}" for i in range(20)])
        signals = m.alt_signals(alts, btc)
        return signals, m.levels(signals, ["A1", "A2", "A3", "A4"], 3, 5)

    def test_a_broad_lasting_alt_rally_reads_high(self):
        signals, level = self.frame(True)
        self.assertEqual(level.iloc[-1], "high")
        self.assertTrue(signals[["A1", "A2", "A3", "A4"]].iloc[-1].all())

    def test_noise_alone_does_not(self):
        _, level = self.frame(False)
        self.assertNotEqual(level.iloc[-1], "high")


class EquityTests(unittest.TestCase):
    def test_a_sell_off_with_a_vix_spike_reads_high(self):
        days = pd.bdate_range("2025-01-01", periods=300)
        spy = pd.Series(np.linspace(400, 500, 300), index=days)
        spy.iloc[-12:] = np.linspace(495, 440, 12)  # an 12% fall in two weeks
        vix = pd.Series(15.0, index=days)
        vix.iloc[-6:] = [18, 22, 26, 30, 32, 31]
        level = m.levels(m.equity_signals(spy, vix), ["E1", "E2", "E3", "E4"], 2, 3)
        self.assertEqual(level.iloc[-1], "high")
        calm = m.levels(m.equity_signals(pd.Series(np.linspace(400, 500, 300), index=days), pd.Series(14.0, index=days)),
                        ["E1", "E2", "E3", "E4"], 2, 3)
        self.assertEqual(calm.iloc[-1], "none")

    def test_notices_say_what_and_whether_trimming(self):
        state = {"alt_season": {"level": "high", "signals": {"A1": True, "A2": True, "A3": True, "A4": False},
                                "detail": "alts +12%"},
                 "equity_selloff": {"level": "none", "signals": {}, "detail": ""}}
        rows = m.notices(state)
        self.assertEqual(len(rows), 1)
        self.assertIn("alt season (high)", rows[0][0])
        self.assertIn("3 of 4", rows[0][1])


if __name__ == "__main__":
    unittest.main()
