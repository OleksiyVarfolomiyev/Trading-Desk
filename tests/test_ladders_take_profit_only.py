"""Take-profit ladders never cut losses (desk/tp_ladder.py, execution/hl_daemon.py no_loss_reduce) and the adaptive
market-making re-evaluation (desk/mm_tuning.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))

from desk import mm_tuning, tp_ladder  # noqa: E402

LADDER = {"levels_atr": [0.5, 0.8, 1.5], "shares": [0.333333, 0.333333, 0.333334], "basis": "test"}


class TakeProfitLadderTests(unittest.TestCase):
    def test_long_rungs_are_above_entry_in_order_and_sum_to_the_position(self):
        rungs = tp_ladder.rungs(100.0, 1, 4.0, 3.0, 2, 0.01, 10.0, LADDER, 12.0)
        self.assertEqual([price for price, _ in rungs], [102.0, 103.2, 106.0])
        self.assertAlmostEqual(sum(size for _, size in rungs), 3.0)
        self.assertTrue(all(price > 100.0 for price, _ in rungs))

    def test_short_rungs_are_below_entry(self):
        rungs = tp_ladder.rungs(100.0, -1, 4.0, 3.0, 2, 0.01, 10.0, LADDER, 12.0)
        self.assertTrue(all(price < 100.0 for price, _ in rungs))
        self.assertEqual([price for price, _ in rungs], sorted([price for price, _ in rungs], reverse=True))

    def test_no_rung_at_or_past_the_entry_even_with_zero_atr_distance(self):
        rungs = tp_ladder.rungs(100.0, 1, 0.0, 3.0, 2, 0.01, 10.0, {"levels_atr": [0.0001], "shares": [1.0]}, 0.0)
        self.assertTrue(all(price >= 100.01 for price, _ in rungs))

    def test_small_position_merges_rungs_to_meet_the_minimum(self):
        rungs = tp_ladder.rungs(100.0, 1, 4.0, 0.25, 2, 0.01, 10.0, LADDER, 12.0)   # $25: at most two $10 rungs
        self.assertTrue(all(price * size >= 10.0 for price, size in rungs))
        self.assertAlmostEqual(sum(size for _, size in rungs), 0.25)

    def test_without_a_ladder_it_is_the_single_target(self):
        self.assertEqual(tp_ladder.rungs(100.0, 1, 4.0, 3.0, 2, 0.01, 10.0, None, 12.0), [(112.0, 3.0)])

    def test_load_rejects_unordered_or_unbalanced_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "l.json"
            path.write_text(json.dumps({"levels_atr": [1.0, 0.5], "shares": [0.5, 0.5]}))
            self.assertIsNone(tp_ladder.load(path))
            path.write_text(json.dumps({"levels_atr": [0.5, 1.0], "shares": [0.5, 0.4]}))
            self.assertIsNone(tp_ladder.load(path))
            path.write_text(json.dumps(LADDER))
            self.assertEqual(tp_ladder.load(path)["levels_atr"], [0.5, 0.8, 1.5])


class NoLossReduceTests(unittest.TestCase):
    def setUp(self):
        import hl_daemon

        self.f = hl_daemon.no_loss_reduce

    def test_long_in_loss_keeps_adds_and_drops_trims_below_entry(self):
        quotes = [{"is_buy": True, "price": 90.0}, {"is_buy": False, "price": 95.0}, {"is_buy": False, "price": 101.0}]
        kept = self.f(quotes, 5.0, 100.0)
        self.assertEqual(kept, [{"is_buy": True, "price": 90.0}, {"is_buy": False, "price": 101.0}])

    def test_short_in_loss_drops_buys_above_entry(self):
        quotes = [{"is_buy": True, "price": 105.0}, {"is_buy": True, "price": 99.0}, {"is_buy": False, "price": 110.0}]
        kept = self.f(quotes, -5.0, 100.0)
        self.assertEqual(kept, [{"is_buy": True, "price": 99.0}, {"is_buy": False, "price": 110.0}])

    def test_at_entry_is_not_a_profit(self):
        self.assertEqual(self.f([{"is_buy": False, "price": 100.0}], 1.0, 100.0), [])

    def test_flat_or_unknown_entry_keeps_everything(self):
        quotes = [{"is_buy": False, "price": 1.0}]
        self.assertEqual(self.f(quotes, 0.0, 100.0), quotes)
        self.assertEqual(self.f(quotes, 1.0, None), quotes)


def fills(n, side_pattern="BA", px=100.0, drift=0.0):
    out = []
    for i in range(n):
        side = side_pattern[i % len(side_pattern)]
        out.append({"time": 1_000_000 + i * 120_000, "side": side, "px": str(px + (0.05 if side == "A" else -0.05)), "sz": "1",
                    "fee": "0.001"})
    return out


class MMTuningTests(unittest.TestCase):
    CONFIG = {"spacing_atr": 0.10, "add_level_pct": 0.05, "trim_level_pct": 0.03, "add_first": 0.5}

    def test_too_few_fills_changes_nothing(self):
        result = mm_tuning.evaluate("ETH", fills(10), [], [], self.CONFIG, 1.0, 300.0, 0.1, 1.0)
        self.assertEqual(result["changes"], {})
        self.assertIn("too few fills", result["status"])

    def test_picked_off_fills_widen_the_spacing_one_step(self):
        sample = fills(60)
        times = [1_000_000 + i * 60_000 for i in range(200)]
        # after every buy the price falls, after every sell it rises: fills are picked off
        closes = []
        for t in times:
            last = max((f for f in sample if f["time"] <= t), key=lambda f: f["time"], default=None)
            closes.append(99.0 if last and last["side"] == "B" else 101.0 if last else 100.0)
        result = mm_tuning.evaluate("ETH", sample, times, closes, self.CONFIG, 1.0, 300.0, 0.1, 1.0)
        self.assertAlmostEqual(result["changes"]["spacing_atr"], 0.125)
        self.assertTrue(any("picked off" in reason for reason in result["reasons"]))

    def test_a_change_past_the_bounds_is_a_notice_not_applied(self):
        config = dict(self.CONFIG, spacing_atr=0.28)
        result = mm_tuning.evaluate("ETH", fills(60), [], [], config, spread_bps=100.0, atr_bps=300.0, funding_apr=0.1, base_side=1.0)
        self.assertNotIn("spacing_atr", result["changes"])
        self.assertTrue(any("outside" in notice for notice in result["notices"]))

    def test_apply_writes_the_coin_entry_and_the_log(self):
        with tempfile.TemporaryDirectory() as folder:
            config, log, state = Path(folder) / "ladder.json", Path(folder) / "log.jsonl", Path(folder) / "state.json"
            config.write_text(json.dumps({"levels": 5, "coins": {"BTC": {"spacing_atr": 0.2}}}))
            mm_tuning.apply([{"coin": "ETH", "changes": {"spacing_atr": 0.125}, "notices": [], "status": "adjusted", "stats": {}}],
                            config, log, state)
            saved = json.loads(config.read_text())
            self.assertEqual(saved["coins"]["ETH"], {"spacing_atr": 0.125})
            self.assertEqual(saved["coins"]["BTC"], {"spacing_atr": 0.2})
            self.assertEqual(saved["levels"], 5)
            self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_edge_per_fill_is_net_of_fees(self):
        edge = mm_tuning.edge_per_fill([{"time": 1, "side": "B", "px": "100", "sz": "1", "fee": "0.02"},
                                        {"time": 2, "side": "A", "px": "100.1", "sz": "1", "fee": "0.02"}])
        self.assertAlmostEqual(edge[0], (0.1 - 0.04) / 200.1 * 1000)


if __name__ == "__main__":
    unittest.main()
