"""The thesis review's arithmetic (desk/thesis_review.py): episodes, hold-through, status tests, stop distances, the tile."""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import thesis_review as tr  # noqa: E402


def closed_frame():
    rows = [
        ("2026-11-04 15:00:00+00:00", "HL", "07f2", "xyz:GOOGL", "LONG", 1.0, 100.0, 80.0, -20.0, "2026-10-16 01:00:00+00:00"),
        ("2026-11-04 15:00:00+00:00", "HL", "07f2", "xyz:GOOGL", "LONG", 3.0, 100.0, 90.0, -30.0, "2026-10-16 01:00:00+00:00"),
        ("2026-11-05 12:00:00+00:00", "HL", "07f2", "xyz:SHAZ", "SHORT", 2.0, 20.0, 22.0, -4.0, "2026-11-03 20:00:00+00:00"),
        ("2026-08-01 12:00:00+00:00", "IB", "8440", "OLD", "LONG", 1.0, 10.0, 9.0, -1.0, "2026-07-01 12:00:00+00:00"),
    ]
    return pd.DataFrame(rows, columns=["time", "venue", "account", "coin", "side", "size", "entry", "exit", "pnl", "opened"])


class EpisodeTests(unittest.TestCase):
    def test_fills_of_one_position_are_one_episode_at_the_size_weighted_prices(self):
        eps = tr.episodes(closed_frame())
        self.assertEqual(len(eps), 2)   # the August close is before the window
        googl = eps[eps["name"] == "GOOGL"].iloc[0]
        self.assertAlmostEqual(googl["size"], 4.0)
        self.assertAlmostEqual(googl["exit"], (80 * 1 + 90 * 3) / 4)
        self.assertAlmostEqual(googl["move"], 87.5 / 100 - 1)
        self.assertAlmostEqual(googl["pnl"], -50.0)

    def test_a_short_that_lost_has_a_negative_move_on_entry(self):
        shaz = tr.episodes(closed_frame()).query("name == 'SHAZ'").iloc[0]
        self.assertAlmostEqual(shaz["move"], -0.10)


class HoldTests(unittest.TestCase):
    def prices(self, values):
        return pd.Series(values, index=pd.date_range("2026-11-05", periods=len(values), freq="D", tz="UTC"))

    def test_a_long_that_kept_falling_after_the_exit_shows_the_exit_saved_money(self):
        stats = tr.hold_stats(self.prices([90, 80, 70, 60, 55, 50]), pd.Timestamp("2026-11-05 12:00", tz="UTC"), 90.0, 1)
        self.assertAlmostEqual(stats["since_exit"], 50 / 90 - 1)
        self.assertLess(stats["since_exit"], 0)   # negative: holding would have lost more
        self.assertEqual(stats["days"], 5)

    def test_a_short_whose_price_fell_after_the_exit_shows_the_exit_cost_money(self):
        stats = tr.hold_stats(self.prices([100, 90, 80, 70, 60, 50, 40]), pd.Timestamp("2026-11-05 12:00", tz="UTC"), 100.0, -1)
        self.assertGreater(stats["since_exit"], 0)
        self.assertAlmostEqual(stats["d5"], -(50 / 100 - 1))

    def test_path_is_a_move_on_the_entry_price_adjusted_for_side(self):
        path = tr.path_after(self.prices([90, 80]), pd.Timestamp("2026-11-05 12:00", tz="UTC"), 100.0, -1)
        self.assertAlmostEqual(path.iloc[-1], 0.20)


class StatusTests(unittest.TestCase):
    def test_neutral_band_keeps_a_coin_flip_from_flipping_the_status(self):
        self.assertEqual(tr.classify(1, 0.10, -0.001, False), "intact")
        self.assertEqual(tr.classify(1, 0.027, -0.16, True), "weakened")

    def test_broken_needs_both_against_and_the_price_at_its_extreme(self):
        self.assertEqual(tr.classify(1, -0.10, -0.20, False), "weakened")
        self.assertEqual(tr.classify(1, -0.10, -0.20, True), "broken")
        self.assertEqual(tr.classify(-1, 0.10, 0.20, True), "broken")   # a short is against when the name outruns SPY

    def test_no_history_cannot_be_tested(self):
        self.assertEqual(tr.classify(1, np.nan, 0.1, False), "cannot test")

    def test_relative_strength_is_the_name_less_spy_over_the_window(self):
        index = pd.date_range("2026-09-01", periods=80, freq="D", tz="UTC")
        name = pd.Series(np.linspace(100, 120, 80), index=index)
        spy = pd.Series(np.linspace(100, 110, 80), index=index)
        self.assertGreater(tr.relative_strength(name, spy, 60), 0)
        self.assertLess(tr.relative_strength(spy, name, 60), 0)

    def test_extreme_against_a_long_is_the_sixty_day_low(self):
        index = pd.date_range("2026-09-01", periods=70, freq="D", tz="UTC")
        falling = pd.Series(np.linspace(100, 60, 70), index=index)
        self.assertTrue(tr.at_extreme_against(falling, 1))
        self.assertFalse(tr.at_extreme_against(falling, -1))


class StatsTests(unittest.TestCase):
    def episode(self, name, day, since, side="LONG"):
        return {"name": name, "side": side, "exit_time": pd.Timestamp(f"2026-11-{day:02d}", tz="UTC"), "since_exit": since, "d5": since,
                "stop_effect": -since * 100, "pnl": -10.0, "verified_stop": False}

    def test_a_name_stopped_twice_counts_once(self):
        stats = tr.stop_statistics([self.episode("A", 3, 0.10), self.episode("A", 4, 0.30), self.episode("B", 5, -0.10),
                                    self.episode("C", 6, 0.02)])
        self.assertEqual(stats["names"][2], 3)
        self.assertEqual(stats["episodes"][2], 4)
        self.assertAlmostEqual(stats["names"][0], (0.20 - 0.10 + 0.02) / 3)

    def test_helped_and_hurt_follow_the_sign_of_the_move_after_the_exit(self):
        stats = tr.stop_statistics([self.episode("A", 3, 0.10), self.episode("B", 4, -0.10), self.episode("C", 5, -0.2)])
        self.assertEqual((stats["hurt"], stats["helped"]), (1, 2))

    def test_t_stat_needs_three_values(self):
        self.assertTrue(np.isnan(tr.t_stat([0.1, 0.2])[1]))


class PlacementTests(unittest.TestCase):
    def test_stop_distance_is_against_the_position_for_longs_and_shorts(self):
        eps = [
            {"account": "HL 07f2", "coin": "xyz:GOOGL", "name": "GOOGL", "side": "LONG", "dir": 1, "entry": 343.0,
             "opened": pd.Timestamp("2026-10-16", tz="UTC"), "exit_time": pd.Timestamp("2026-11-04", tz="UTC")},
            {"account": "HL 07f2", "coin": "xyz:SHAZ", "name": "SHAZ", "side": "SHORT", "dir": -1, "entry": 23.8,
             "opened": pd.Timestamp("2026-11-03", tz="UTC"), "exit_time": pd.Timestamp("2026-11-04", tz="UTC")},
        ]
        orders = pd.DataFrame([
            {"wallet": "07f2", "coin": "xyz:GOOGL", "trigger": 275.0, "status": "triggered", "time": pd.Timestamp("2026-11-02 17:29", tz="UTC")},
            {"wallet": "07f2", "coin": "xyz:SHAZ", "trigger": 25.0, "status": "triggered", "time": pd.Timestamp("2026-11-03 20:02", tz="UTC")},
            {"wallet": "07f2", "coin": "xyz:SHAZ", "trigger": 27.0, "status": "canceled", "time": pd.Timestamp("2026-11-03 20:02", tz="UTC")},
        ])
        with mock.patch.object(tr, "mark_at", side_effect=lambda coin, when: {"xyz:GOOGL": 282.5, "xyz:SHAZ": 23.8}[coin]):
            placements = tr.stop_placements(eps, orders)
        googl, shaz = placements
        self.assertAlmostEqual(googl["from_mark"][0], 1 - 275.0 / 282.5)   # below the mark: against a long
        self.assertGreater(googl["from_mark"][0], 0)
        self.assertAlmostEqual(shaz["from_mark"][0], 25.0 / 23.8 - 1)      # above the mark: against a short
        self.assertEqual(len(shaz["levels"]), 1)                            # the replaced order is not a rung
        self.assertAlmostEqual(googl["lost_before"], 282.5 / 343.0 - 1)


class TileTests(unittest.TestCase):
    def test_tile_has_no_zero_or_nan_cells(self):
        names = pd.DataFrame([
            {"name": "NET", "side": "LONG", "held": True, "thesis": "t", "stop_move": -0.11, "since_exit": 0.09, "status": "intact"},
            {"name": "MSFT", "side": "SHORT", "held": True, "thesis": "t", "stop_move": np.nan, "since_exit": np.nan, "status": "weakened"},
            {"name": "IONQ", "side": "SHORT", "held": False, "thesis": "t", "stop_move": -0.1, "since_exit": 0.36, "status": "closed"},
        ])
        frame = tr.tile_frame(names)
        self.assertEqual(list(frame.columns), ["Position", "Thesis", "Stop realised", "Since exit", "Status"])
        from desk import tile_feed

        rows = tile_feed._table(frame, {"Stop realised": "spct", "Since exit": "spct"})["rows"]
        self.assertIsNone(rows[1]["Stop realised"])   # absent is absent on the page: null, never 0 or nan
        self.assertEqual(frame.loc[2, "Status"], "closed")
        self.assertEqual(frame.loc[0, "Position"], "NET long")

    def test_excluded_symbolic_holdings_have_no_thesis(self):
        self.assertEqual(tr.EXCLUDED, {"KYIV", "SWMR"})
        self.assertEqual(tr.thesis_of("KYIV")[1], "none")


if __name__ == "__main__":
    unittest.main()
