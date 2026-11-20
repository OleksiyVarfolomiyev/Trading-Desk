"""The crypto book's statistical drawdown monitor (desk/drawdown_monitor.py) and the stocks-vs-SPY view (desk/drawdown.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import drawdown as dd  # noqa: E402
from desk import drawdown_monitor as dm  # noqa: E402


def series(values, start="2026-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"), dtype=float)


FITTED = {"sigma_daily": 0.0127, "quarter": 63, "assumed_sharpe": 1.0,
          "levels": {"watch": {"sigma_units": 0.82, "false_alarm_per_quarter": 0.35},
                     "re-evaluate": {"sigma_units": 1.38, "false_alarm_per_quarter": 0.08}}}


class SimulationTests(unittest.TestCase):
    def test_max_drawdown_of_known_paths(self):
        paths = np.array([[0.1, -0.5, 0.2], [0.1, 0.1, 0.1], [-0.1, -0.1, 0.0]])
        out = dm.max_drawdown(paths)
        self.assertAlmostEqual(out[0], 0.5)           # 1.1 then halved
        self.assertAlmostEqual(out[1], 0.0)           # never below a high
        self.assertAlmostEqual(out[2], 1 - 0.9 * 0.9)  # starts from the high at 1

    def test_block_bootstrap_shape_and_values_come_from_the_series(self):
        rng = np.random.default_rng(1)
        data = np.arange(1, 21) / 100
        out = dm.block_bootstrap(data, 15, 50, rng, 4)
        self.assertEqual(out.shape, (50, 15))
        self.assertTrue(np.isin(out, data).all())

    def test_block_length_grows_with_autocorrelation(self):
        rng = np.random.default_rng(2)
        noise = rng.normal(size=600)
        trending = np.convolve(noise, np.ones(12) / 12, mode="same")
        self.assertGreater(dm.block_length(trending), dm.block_length(noise))
        self.assertGreaterEqual(dm.block_length(noise), 3)

    def test_student_paths_have_the_requested_volatility(self):
        rng = np.random.default_rng(3)
        paths = dm.student_paths(5, 0.02, 200, 400, rng)
        self.assertAlmostEqual(float(paths.std()), 0.02, delta=0.003)

    def test_deeper_percentile_is_deeper_and_longer_is_deeper(self):
        rng = np.random.default_rng(4)
        r = rng.normal(0.0005, 0.02, 500)
        short = dm.max_drawdown(dm.block_bootstrap(r, 30, 1500, rng, 5))
        long = dm.max_drawdown(dm.block_bootstrap(r, 90, 1500, rng, 5))
        self.assertLess(np.percentile(short, 50), np.percentile(short, 95))
        self.assertLess(np.percentile(short, 90), np.percentile(long, 90))


class ReplayTests(unittest.TestCase):
    def test_replay_weights_and_stand_in_for_a_short_history(self):
        idx = pd.date_range("2026-01-01", periods=200, freq="D")
        rng = np.random.default_rng(5)
        returns = pd.DataFrame({"AAA": rng.normal(0, 0.02, 200), "BBB": rng.normal(0, 0.03, 200)}, index=idx)
        returns["NEW"] = np.nan
        returns.loc[idx[-10:], "NEW"] = 0.05           # listed 10 days ago: replayed by the basket before that
        total, info = dm.replay_returns({"AAA": 0.5, "BBB": -0.25, "NEW": 0.1}, returns)
        self.assertEqual(info["stand_in"], ["NEW"])
        self.assertAlmostEqual(info["net"], 0.35)
        self.assertAlmostEqual(info["gross"], 0.85)
        day = idx[50]
        basket = returns.loc[day, ["AAA", "BBB"]].median()
        self.assertAlmostEqual(total[day], 0.5 * returns.loc[day, "AAA"] - 0.25 * returns.loc[day, "BBB"] + 0.1 * basket)


class MonitorTests(unittest.TestCase):
    def test_levels_scale_with_the_books_volatility(self):
        weights = {"AAA": 0.5}
        idx = pd.date_range("2026-01-01", periods=300, freq="D")
        rng = np.random.default_rng(6)
        calm = pd.DataFrame({"AAA": rng.normal(0, 0.02, 300)}, index=idx)
        wild = calm * 2
        calls = iter([calm, wild])
        original = dm.panel_returns
        dm.panel_returns = lambda path=None: next(calls)
        try:
            small = dm.levels_now(FITTED, weights)
            big = dm.levels_now(FITTED, weights)
        finally:
            dm.panel_returns = original
        self.assertAlmostEqual(big["watch"] / small["watch"], 2.0, places=3)
        self.assertGreater(small["re-evaluate"], small["watch"])
        self.assertEqual(small["source"], "today's positions")

    def test_level_reached(self):
        levels = {"watch": 0.08, "re-evaluate": 0.14}
        self.assertIsNone(dm.level_reached(-0.05, levels))
        self.assertEqual(dm.level_reached(-0.09, levels), "watch")
        self.assertEqual(dm.level_reached(-0.14, levels), "re-evaluate")
        self.assertIsNone(dm.level_reached(None, levels))
        self.assertIsNone(dm.level_reached(-0.5, None))

    def test_alert_rows_carry_the_evidence_and_are_blank_below_the_levels(self):
        levels = {"watch": 0.08, "re-evaluate": 0.14}
        self.assertEqual(dm.alerts(-0.03, levels, FITTED), [])
        rows = dm.alerts(-0.15, levels, FITTED)
        self.assertEqual(rows[0][0], "Crypto drawdown: re-evaluate models")
        self.assertIn("8%", rows[0][1])               # about 8% of quarters for a book whose model is right
        self.assertEqual(dm.alerts(-0.09, levels, FITTED)[0][0], "Crypto drawdown: watch")

    def test_push_once_per_level_and_again_after_it_clears(self):
        levels = {"watch": 0.08, "re-evaluate": 0.14}
        sent = []
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "s.json"
            send = lambda title, text: sent.append(title)
            self.assertEqual(dm.notify(-0.09, levels, FITTED, state, send), "watch")
            self.assertIsNone(dm.notify(-0.10, levels, FITTED, state, send))      # same level: no repeat
            self.assertEqual(dm.notify(-0.15, levels, FITTED, state, send), "re-evaluate")
            self.assertIsNone(dm.notify(-0.01, levels, FITTED, state, send))      # cleared: nothing sent, state reset
            self.assertEqual(dm.notify(-0.09, levels, FITTED, state, send), "watch")
        self.assertEqual(len(sent), 3)

    def test_the_committed_fit_orders_its_levels_and_states_its_false_alarm_rates(self):
        fitted = dm.load_fit()
        self.assertIsNotNone(fitted)
        watch, reeval = fitted["levels"]["watch"], fitted["levels"]["re-evaluate"]
        self.assertLess(watch["drawdown"], reeval["drawdown"])
        self.assertAlmostEqual(fitted["frequency"]["watch"], 0.35, delta=0.02)
        self.assertAlmostEqual(fitted["frequency"]["re-evaluate"], 0.08, delta=0.02)
        for level in (watch, reeval):
            self.assertLessEqual(level["ci90"][0], level["drawdown"])
            self.assertLessEqual(level["drawdown"], level["ci90"][1])
        # a model with no edge or more volatility crosses the levels more often than one that is right
        right, worse = fitted["power"]["model right"]["quarter"], fitted["power"]["vol +50%"]["quarter"]
        self.assertGreater(worse["re-evaluate"], right["re-evaluate"])

    def test_the_monitor_is_registered_as_monitoring_not_a_rule(self):
        from desk import forward_monitor

        item = next(i for i in forward_monitor.REGISTRY if i.key == "dd-crypto-models")
        self.assertTrue(item.monitor.startswith("monitoring"))


class StocksViewTests(unittest.TestCase):
    def test_alert_text_names_the_passive_stocks_and_never_uses_them_for_a_tolerance(self):
        levels = {"desk": series([1.0, 1.0, 0.92]), "crypto": series([1.0, 1.0, 0.99]), "stocks": series([1.0, 1.0, 0.80])}
        rows = dd.alerts(levels, 0.10)
        self.assertEqual(rows[0][0], "Drawdown nearing tolerance")            # the desk's own drawdown decides
        self.assertIn("stocks, passive", rows[0][1])
        self.assertIsNone(dd.alert_level(float(dd.drawdowns(levels["desk"]).iloc[-1]), None))
        only_stocks_deep = {"desk": series([1.0, 1.0, 0.99]), "stocks": series([1.0, 1.0, 0.70])}
        self.assertEqual(dd.alerts(only_stocks_deep, 0.10), [])               # a deep passive book alone raises nothing

    def test_period_table_can_carry_spy_beside_the_stocks(self):
        levels = {"desk": series([1.0, 1.1, 1.0, 1.05]), "stocks": series([1.0, 1.05, 0.9, 0.95]), "spy": series([1.0, 1.02, 0.95, 0.98])}
        rows = dd.period_table(levels, names=dd.NAMES + ("spy",))
        self.assertIn("spy", {row["Book"] for row in rows})
        self.assertNotIn("spy", {row["Book"] for row in dd.period_table(levels)})

    def test_underwater_figure_dashes_the_passive_stocks_and_marks_the_crypto_levels(self):
        levels = {"desk": series([1.0, 1.0, 0.95]), "crypto": series([1.0, 0.98, 0.97]), "stocks": series([1.0, 0.9, 0.85])}
        spy = series([1.0, 0.95, 0.9])
        figure = dd.underwater_figure(levels, 0.10, spy, {"watch": 0.08, "re-evaluate": 0.14})
        names = [trace.name for trace in figure.data]
        self.assertEqual(names, ["desk", "crypto", "stocks (passive)", "SPY"])
        self.assertEqual(figure.data[2].line.dash, "dash")
        texts = [a.text for a in figure.layout.annotations]
        self.assertTrue(any(t.startswith("crypto watch") for t in texts))
        self.assertTrue(any(t.startswith("crypto re-evaluate") for t in texts))


if __name__ == "__main__":
    unittest.main()
