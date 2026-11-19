"""The take-profit ladder replay (research/tp_ladder_replay.py): exits at a level only when a full held day reached it."""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research import tp_ladder_replay as replay  # noqa: E402


class ReplayTests(unittest.TestCase):
    def test_levels_hit_exit_there_and_unreached_ones_exit_as_the_trade_did(self):
        part = pd.DataFrame([{"move": -0.05, "atr": 0.10, "best": 0.08}])   # a loser that ran 0.8 ATR first
        diff = replay.evaluate(part, [0.5, 1.0, 2.0])
        ladder = ((0.05 - replay.MAKER_FEE) + 2 * (-0.05 - replay.TAKER_FEE)) / 3
        single = -0.05 - replay.TAKER_FEE                       # 3 ATR never reached
        self.assertAlmostEqual(diff.iloc[0], ladder - single)

    def test_levels_come_from_winners_only_of_the_fitting_half(self):
        part = pd.DataFrame({"move": [0.1, 0.2, -0.3], "atr": [0.1, 0.1, 0.1]})
        levels = replay.fit_levels(part)
        self.assertTrue(all(1.0 <= level <= 2.0 for level in levels))


if __name__ == "__main__":
    unittest.main()
