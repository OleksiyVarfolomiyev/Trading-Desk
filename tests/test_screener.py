"""Tests for candidate screener and ladder coin selection in desk/screener.py."""

import unittest
from unittest.mock import patch
import pandas as pd
import numpy as np

from desk import screener


class ScreenerTests(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range("2026-01-01", periods=65, freq="D", tz="UTC")
        self.mock_btc = pd.DataFrame({
            "time": dates,
            "open": np.linspace(50000, 60000, 65),
            "high": np.linspace(51000, 61000, 65),
            "low": np.linspace(49000, 59000, 65),
            "close": np.linspace(50000, 60000, 65),
            "volume": np.full(65, 1000.0),
        })
        self.mock_alt = pd.DataFrame({
            "time": dates,
            "open": np.linspace(100, 80, 65),  # declining vs BTC
            "high": np.linspace(102, 82, 65),
            "low": np.linspace(98, 78, 65),
            "close": np.linspace(100, 80, 65),
            "volume": np.full(65, 50000.0),
        })

    def test_calculate_metrics(self):
        with patch("desk.hl.trade_analytics.fetch_daily_candles", return_value=self.mock_alt):
            m = screener._calculate_metrics("SOL", 80.0, self.mock_btc)
            self.assertIsNotNone(m)
            self.assertIn("rel7", m)
            self.assertIn("rel30", m)
            self.assertIn("from_high60", m)
            self.assertIn("atr14", m)
            # Alt declined while BTC rose, so relative return must be negative
            self.assertLess(m["rel7"], 0)
            self.assertLess(m["rel30"], 0)
            self.assertLess(m["from_high60"], 0)
            self.assertGreater(m["atr14"], 0)

    def test_screen_candidates_produces_sized_trades(self):
        liquid_mock = [{
            "name": "SOL",
            "sz_decimals": 2,
            "max_leverage": 20,
            "mark": 80.0,
            "prev_day_px": 82.0,
            "funding_hourly": 0.00001,
            "volume_24h": 50_000_000.0,
            "open_interest": 10_000_000.0,
        }]
        with patch.object(screener, "fetch_liquid_perps", return_value=liquid_mock), \
             patch("desk.hl.trade_analytics.fetch_daily_candles", side_effect=lambda coin, **kw: self.mock_btc if coin == "BTC" else self.mock_alt):
            candidates = screener.screen_candidates(desk_equity=10000.0, current_exposures=[])
            self.assertEqual(len(candidates), 1)
            c = candidates[0]
            self.assertEqual(c.coin, "SOL")
            self.assertEqual(c.side, "SHORT")
            self.assertGreater(c.planned_size, 0)
            self.assertGreater(c.stop_price, c.mark)
            self.assertIn("execution/hl_trade.py", c.command)

    def test_ladder_candidates_selection(self):
        # Range-bound mock: oscillates around 100
        dates = pd.date_range("2026-01-01", periods=65, freq="D", tz="UTC")
        oscillating = pd.DataFrame({
            "time": dates,
            "open": np.full(65, 100.0),
            "high": np.full(65, 105.0),
            "low": np.full(65, 95.0),
            "close": np.full(65, 100.0),
            "volume": np.full(65, 10000.0),
        })
        liquid_mock = [{
            "name": "RANGE",
            "sz_decimals": 2,
            "max_leverage": 20,
            "mark": 100.0,
            "prev_day_px": 100.0,
            "funding_hourly": 0.0,
            "volume_24h": 30_000_000.0,
            "open_interest": 5_000_000.0,
        }]
        with patch.object(screener, "fetch_liquid_perps", return_value=liquid_mock), \
             patch("desk.hl.trade_analytics.fetch_daily_candles", side_effect=lambda coin, **kw: self.mock_btc if coin == "BTC" else oscillating):
            ladders = screener.ladder_candidates(min_volume=20_000_000.0)
            self.assertEqual(len(ladders), 1)
            cand = ladders[0]
            self.assertEqual(cand.coin, "RANGE")
            self.assertAlmostEqual(cand.range_pos60, 0.5, places=1)
            self.assertGreater(cand.mm_score, 0.5)


if __name__ == "__main__":
    unittest.main()
