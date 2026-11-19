"""Order-book wall/spoof signals and their forward test (desk/book_signals.py)."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import book_signals  # noqa: E402


def book(mid=100.0, bid_sizes=None, asks_sizes=None, step=0.05):
    bid_sizes = bid_sizes or [10.0] * 30
    asks_sizes = asks_sizes or [10.0] * 30
    return {"levels": [
        [{"px": str(round(mid - step * (i + 1), 4)), "sz": str(s), "n": 1} for i, s in enumerate(bid_sizes)],
        [{"px": str(round(mid + step * (i + 1), 4)), "sz": str(s), "n": 1} for i, s in enumerate(asks_sizes)],
    ]}


class SignalTests(unittest.TestCase):
    def test_balanced_book_has_no_imbalance_or_walls(self):
        values = book_signals.signals(*book_signals.parse_book(book()))
        self.assertAlmostEqual(values["imbalance_100bp"], 0.0, places=1)
        self.assertEqual(values["bid_wall_usd"], 0.0)
        self.assertAlmostEqual(values["mid"], 100.0)

    def test_heavy_bids_read_positive(self):
        values = book_signals.signals(*book_signals.parse_book(book(bid_sizes=[30.0] * 30)))
        self.assertGreater(values["imbalance_100bp"], 0.4)

    def test_a_wall_is_found_and_skews_the_signal(self):
        sizes = [10.0] * 30
        sizes[9] = 400.0
        values = book_signals.signals(*book_signals.parse_book(book(bid_sizes=sizes)))
        self.assertGreater(values["bid_wall_usd"], 30000)
        self.assertGreater(values["wall_skew"], 0)
        self.assertEqual(values["ask_wall_usd"], 0.0)

    def test_a_wall_pulled_while_price_stays_away_is_a_spoof(self):
        sizes = [10.0] * 30
        sizes[19] = 400.0   # ~100 bps from the mid, far from price
        first = book_signals.signals(*book_signals.parse_book(book(bid_sizes=sizes)))
        walls = first.pop("_walls")
        self.assertTrue(walls["bid"])
        second = book_signals.signals(*book_signals.parse_book(book()), {"bid": walls["bid"], "ask": []})
        self.assertEqual(second["bid_walls_pulled"], 1)
        self.assertEqual(second["spoof_skew"], -1)

    def test_a_wall_eaten_near_price_is_not_a_spoof(self):
        near = [(99.95, 40000.0)]
        gone = book_signals.spoofed(near, [(99.95, 100.0)], mid=100.0)   # 5 bps away: price reached it
        self.assertEqual(gone, 0)

    def test_empty_book_gives_nothing(self):
        self.assertIsNone(book_signals.signals([], []))


class PassTests(unittest.TestCase):
    def test_pass_logs_rows_and_skips_a_failed_coin(self):
        calls = []

        def fetch(payload):
            calls.append(payload["coin"])
            if payload["coin"] == "ETH":
                raise RuntimeError("HL info request failed (HTTP 429).")
            return book()
        state = {}
        rows = book_signals.snapshot_pass(["BTC", "ETH", "SOL"], state, fetch=fetch, sleep=lambda s: None,
                                          now=datetime(2026, 11, 18, tzinfo=timezone.utc))
        self.assertEqual([r["coin"] for r in rows], ["BTC", "SOL"])
        self.assertNotIn("_walls", rows[0])
        self.assertIn("BTC", state)
        self.assertEqual(calls, ["BTC", "ETH", "SOL"])


def synthetic_log(predictive: bool, n=400, seed=1):
    rng = np.random.default_rng(seed)
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = []
    for coin in ("AAA", "BBB", "CCC"):
        mid = 100.0
        signal = rng.normal(size=n)
        shocks = rng.normal(scale=0.0004, size=n)
        for i in range(n):
            rows.append({"time": start + timedelta(minutes=5 * i), "coin": coin, "mid": mid, "imbalance_10bp": signal[i]})
            drift = 0.0015 * np.sign(signal[i]) if predictive else 0.0
            mid *= 1 + shocks[i] + drift
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


class ForwardTestTests(unittest.TestCase):
    def test_predictive_signal_survives_net_of_costs(self):
        result = book_signals.evaluate(synthetic_log(True), ["imbalance_10bp"], horizon_min=5)
        self.assertEqual(result.iloc[0]["Verdict"], "survives")
        self.assertGreater(result.iloc[0]["t first half"], 2)

    def test_noise_is_dropped(self):
        result = book_signals.evaluate(synthetic_log(False), ["imbalance_10bp"], horizon_min=5)
        self.assertTrue(result.iloc[0]["Verdict"].startswith("dropped"))

    def test_costs_can_kill_a_real_but_small_edge(self):
        frame = book_signals.forward_returns(synthetic_log(True), 5)
        cheap = book_signals.signal_series(frame, "imbalance_10bp", cost_bps=0.0).mean()
        costly = book_signals.signal_series(frame, "imbalance_10bp", cost_bps=50.0).mean()
        self.assertGreater(cheap, costly)
        self.assertLess(costly, 0)

    def test_forward_return_uses_only_later_snapshots(self):
        frame = synthetic_log(False, n=10)
        out = book_signals.forward_returns(frame, 5)
        first = out[out["coin"] == "AAA"].sort_values("time").iloc[0]
        later = frame[(frame["coin"] == "AAA")].sort_values("time").iloc[1]["mid"]
        self.assertAlmostEqual(first["fwd"], later / first["mid"] - 1)
        self.assertTrue(out[out["coin"] == "AAA"].sort_values("time")["fwd"].iloc[-1:].isna().all())  # last row has no future

    def test_short_log_is_not_judged(self):
        result = book_signals.evaluate(synthetic_log(True, n=10), ["imbalance_10bp"], horizon_min=5)
        self.assertEqual(result.iloc[0]["Verdict"], "not enough snapshots yet")

    def test_single_snapshot_per_coin_is_not_judged(self):
        frame = synthetic_log(True, n=10)
        one = frame.groupby("coin").head(1)
        result = book_signals.evaluate(one, ["imbalance_10bp"], horizon_min=60)
        self.assertEqual(result.iloc[0]["Verdict"], "not enough snapshots yet")


if __name__ == "__main__":
    unittest.main()
