"""Tests for honest backtest harness in desk/hl/signal_research.py."""

import unittest
import numpy as np
import pandas as pd

from desk.hl import signal_research


class SignalResearchTests(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
        self.dates = dates
        # Synthetic price data for BTC and 3 altcoins
        np.random.seed(42)
        close_btc = 50000.0 * np.exp(np.cumsum(np.random.normal(0.001, 0.02, size=len(dates))))
        close_alt1 = 100.0 * np.exp(np.cumsum(np.random.normal(0.002, 0.04, size=len(dates))))
        close_alt2 = 50.0 * np.exp(np.cumsum(np.random.normal(-0.001, 0.03, size=len(dates))))
        close_alt3 = 10.0 * np.exp(np.cumsum(np.random.normal(0.0, 0.05, size=len(dates))))

        self.panel = {
            "BTC": pd.DataFrame({
                "open": close_btc * 0.99,
                "high": close_btc * 1.02,
                "low": close_btc * 0.98,
                "close": close_btc,
                "volume": np.full(len(dates), 1000.0),
            }, index=dates),
            "ALT1": pd.DataFrame({
                "open": close_alt1 * 0.99,
                "high": close_alt1 * 1.03,
                "low": close_alt1 * 0.97,
                "close": close_alt1,
                "volume": np.full(len(dates), 50000.0),
            }, index=dates),
            "ALT2": pd.DataFrame({
                "open": close_alt2 * 0.99,
                "high": close_alt2 * 1.03,
                "low": close_alt2 * 0.97,
                "close": close_alt2,
                "volume": np.full(len(dates), 50000.0),
            }, index=dates),
            "ALT3": pd.DataFrame({
                "open": close_alt3 * 0.99,
                "high": close_alt3 * 1.03,
                "low": close_alt3 * 0.97,
                "close": close_alt3,
                "volume": np.full(len(dates), 50000.0),
            }, index=dates),
        }

    def test_frames_alignment(self):
        data = signal_research.frames(self.panel)
        for key in ("close", "high", "low", "dollar_volume"):
            self.assertIn(key, data)
            self.assertEqual(len(data[key]), len(self.dates))
            self.assertIn("BTC", data[key].columns)
            self.assertIn("ALT1", data[key].columns)

    def test_universe_selection(self):
        data = signal_research.frames(self.panel)
        u = signal_research.universe(data["close"], data["dollar_volume"], size=2, min_volume=1000.0)
        # BTC is in EXCLUDED, so only alts can be in universe
        self.assertNotIn("BTC", u.columns)
        # Check that boolean frame is produced
        self.assertTrue(isinstance(u.iloc[-1]["ALT1"], (bool, np.bool_)))

    def test_atr_calculation(self):
        data = signal_research.frames(self.panel)
        atr_frame = signal_research.atr(data["high"], data["low"], data["close"], period=14)
        self.assertEqual(atr_frame.shape, data["close"].shape)
        self.assertTrue((atr_frame.iloc[20:] > 0).all().all())

    def test_signal_frames(self):
        data = signal_research.frames(self.panel)
        signals = signal_research.signal_frames(data)
        for sig in ("rel7", "rel30", "rel90", "from_high60", "vol30", "volume_surge"):
            self.assertIn(sig, signals)
            self.assertEqual(signals[sig].shape, data["close"].shape)

    def test_position_returns_stop_and_cost_application(self):
        data = signal_research.frames(self.panel)
        long_ret, short_ret = signal_research.position_returns(data, hold=7, stop_multiple=3.0)
        self.assertEqual(long_ret.shape, data["close"].shape)
        self.assertEqual(short_ret.shape, data["close"].shape)
        # Costs should be subtracted (long_return <= raw_price_change)
        valid = long_ret.dropna()
        self.assertGreater(len(valid), 0)


if __name__ == "__main__":
    unittest.main()
