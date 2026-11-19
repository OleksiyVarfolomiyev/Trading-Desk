"""The commit-to-risk/reward model (desk/commit_risk_reward.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import build_history, commit_risk_reward as crr  # noqa: E402


def synthetic(post_effect=0.0, short_after=True, n=60, seed=1):
    rng = np.random.default_rng(seed)
    days = pd.date_range("2026-09-01", periods=n)
    bench = pd.Series(rng.normal(0, 0.02, n), index=days)
    net = pd.Series(0.0, index=days)
    if short_after:
        net[days >= "2026-10-15"] = -1.0   # the book goes net short from the split
    ret = net.shift(1).fillna(0) * bench + rng.normal(0, 0.002, n)
    ret[days >= "2026-10-15"] += post_effect
    return pd.DataFrame({"ret": ret, "bench": bench, "net": net, "gross": net.abs(), "fees": 0.0, "funding": 0.0})


class RegimeTests(unittest.TestCase):
    def test_exposure_is_not_credited_to_the_change(self):
        panel = synthetic(post_effect=0.0)
        out = crr.regime_test(panel, "2026-10-15")
        self.assertLess(abs(out["t_gamma"]), 2)                    # a net short earning the market's fall is not alpha
        self.assertAlmostEqual(out["beta_matched"], 1.0, delta=0.15)

    def test_a_real_extra_return_is_found(self):
        out = crr.regime_test(synthetic(post_effect=0.01), "2026-10-15")
        self.assertGreater(out["t_gamma"], 2)
        self.assertAlmostEqual(out["gamma"], 0.01, delta=0.003)
        self.assertGreater(out["gamma_first_half"], 0)
        self.assertGreater(out["gamma_second_half"], 0)

    def test_too_few_days_is_not_tested(self):
        self.assertEqual(crr.regime_test(synthetic(), "2026-09-04"), {})

    def test_placebo_ranks_the_real_split(self):
        plc = crr.placebo(synthetic(post_effect=0.02), "2026-10-15")
        self.assertGreater(plc["splits"], 10)
        self.assertLess(plc["share_as_large"], 0.2)

    def test_ols_hac_recovers_coefficients(self):
        rng = np.random.default_rng(0)
        x = np.column_stack([np.ones(200), rng.normal(size=200)])
        y = 1.5 + 2.0 * x[:, 1] + rng.normal(0, 0.1, 200)
        beta, t = crr.ols_hac(y, x)
        np.testing.assert_allclose(beta, [1.5, 2.0], atol=0.05)
        self.assertGreater(t[1], 10)


class VerdictTests(unittest.TestCase):
    base = {"t_gamma": 2.5, "gamma": 0.01, "gamma_first_half": 0.012, "gamma_second_half": 0.008}

    def test_established_needs_t_halves_and_placebo(self):
        self.assertEqual(crr.verdict(self.base, {"share_as_large": 0.05})[0], "established")

    def test_each_condition_can_fail(self):
        self.assertEqual(crr.verdict({**self.base, "t_gamma": 1.9}, {"share_as_large": 0.05})[0], "not established")
        self.assertEqual(crr.verdict({**self.base, "gamma_second_half": -0.001}, {"share_as_large": 0.05})[0], "not established")
        self.assertEqual(crr.verdict(self.base, {"share_as_large": 0.5})[0], "not established")
        self.assertEqual(crr.verdict({}, {})[0], "not testable")


class MapTests(unittest.TestCase):
    rows = [{"hash": str(i), "area": area, "added": "10", "removed": "5"} for i, area in enumerate(["ui", "ui", "daemon", "signals"])]

    def test_effort_shares_sum_to_one(self):
        table = crr.effort_by_area(self.rows)
        self.assertAlmostEqual(table["Commits %"].sum(), 1.0)
        self.assertAlmostEqual(table["Churn %"].sum(), 1.0)
        self.assertEqual(table.iloc[0]["Area"], "ui")

    def test_outcome_shares_sum_to_one_and_ui_gets_none(self):
        share = crr.outcome_by_area({"Picks": 0.04, "Sizing": 0.03, "Timing": 0.1, "Market exposure": 0.3, "Fees & funding": -0.02}, 2.0)
        self.assertAlmostEqual(sum(share.values()), 1.0)
        self.assertNotIn("ui", share)

    def test_events_sit_on_real_commit_days(self):
        days = {row["date_et"][:10] for row in build_history.read_csv()}
        for date, area, _ in crr.EVENTS:
            self.assertIn(date, days)
            self.assertIn(area, build_history.AREAS)

    def test_every_event_is_confounded_by_a_neighbour(self):
        self.assertTrue(all(row["others_within"] >= 1 for row in crr.confounding()))


class TileTests(unittest.TestCase):
    def test_publish_writes_a_table_and_a_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            keys = crr.publish_cached(directory=Path(folder), push=False)
            self.assertEqual(keys, ["build-risk-reward-table", "build-risk-reward-chart"])
            table = json.loads((Path(folder) / "build-risk-reward-table.json").read_text())
            chart = json.loads((Path(folder) / "build-risk-reward-chart.json").read_text())
            self.assertEqual(table["kind"], "table")
            self.assertEqual(chart["kind"], "chart")
            self.assertEqual(len(chart["data"]), 2)


if __name__ == "__main__":
    unittest.main()
