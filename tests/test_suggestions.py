"""The suggestions log: what counts as followed, how a call is scored, and when a hit rate is too thin to read."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import suggestions as s  # noqa: E402


class SuggestionTests(unittest.TestCase):
    def test_followed_means_an_opening_fill_in_that_direction_within_three_days(self):
        at = "2026-11-20T14:00:00Z"
        start = s._ms(at)
        fills = [
            {"coin": "xyz:SHAZ", "side": "A", "dir": "Open Short", "time": start + 3600_000},
            {"coin": "xyz:SHAZ", "side": "B", "dir": "Close Short", "time": start + 7200_000},
        ]
        self.assertIsNotNone(s.followed("xyz:SHAZ", "short", at, fills))
        self.assertIsNone(s.followed("xyz:SHAZ", "long", at, fills))                       # the wrong direction is not following it
        self.assertIsNone(s.followed("xyz:SHAZ", "short", at, [{**fills[0], "time": start + 5 * 86400_000}]))   # too late
        self.assertIsNone(s.followed("xyz:QNT", "short", at, fills))

    def test_a_directional_call_is_scored_in_its_own_direction_and_avoid_is_inverted(self):
        orig = s.price_moves
        try:
            s.price_moves = lambda coin, at, now_ms=None: {1: -0.10, 3: None, 7: None}
            short = s.score([{"at": "2026-11-20T14:00:00Z", "coin": "X", "side": "short", "kind": "trade", "text": "t"}], fills=[])[0]
            self.assertAlmostEqual(short["moves"][1], 0.10)          # price fell 10%: a short call gained 10%
            avoid = s.score([{"at": "2026-11-20T14:00:00Z", "coin": "X", "side": "long", "kind": "avoid", "text": "t"}], fills=[])[0]
            self.assertAlmostEqual(avoid["moves"][1], 0.10)          # price fell: avoiding the long was right
            self.assertIsNone(short["moves"][3])                     # a horizon in the future is not scored
            skipped = s.score([{"at": "2026-11-20T14:00:00Z", "coin": None, "side": None, "kind": "info", "text": "t"}], fills=[])
            self.assertEqual(skipped, [])                            # analysis that is not a call is never scored
        finally:
            s.price_moves = orig

    def test_a_thin_hit_rate_says_too_few(self):
        rows = [{"moves": {1: 0.02, 3: -0.01, 7: None}, "followed": True}] * 3
        out = s.summary(rows)
        self.assertEqual(out["horizons"][1]["hit"], 1.0)
        self.assertEqual(out["horizons"][7]["n"], 0)
        self.assertIn("too few", out["verdict"])

    def test_add_appends_and_never_edits(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            s.add("X", "long", "first", path=path)
            s.add("Y", "short", "second", path=path)
            self.assertEqual([row["text"] for row in s.load(path)], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
