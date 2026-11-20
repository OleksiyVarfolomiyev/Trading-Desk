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


class FitAndComparisonTests(unittest.TestCase):
    NOW = s._ms("2026-11-25T00:00:00Z")

    def series(self, coin, at):   # +10% a day for a week from 100
        start = s._ms(at)
        return [(start + h * 3600_000, 100 * 1.1 ** (h / 24)) for h in range(-1, 24 * 8)]

    def test_old_rows_default_to_unknown_fit_and_a_bad_fit_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            path.write_text('{"at": "2026-11-20T00:00:00Z", "coin": "X", "side": "long", "kind": "trade", "confidence": "low", "text": "old"}\n')
            self.assertEqual(s.load(path)[0]["fit"], "unknown")
            with self.assertRaises(ValueError):
                s.add("X", "long", "t", fit="whatever", path=path)

    def test_reduce_and_avoid_are_scored_in_their_own_direction(self):
        rows = [{"at": "2026-11-20T00:00:00Z", "coin": "X", "side": "long", "kind": "reduce", "fit": "strategy", "confidence": "low", "text": "r"},
                {"at": "2026-11-20T00:00:00Z", "coin": "Y", "side": "long", "kind": "avoid", "fit": "additional", "confidence": "low", "text": "a"},
                {"at": "2026-11-20T00:00:00Z", "coin": "Z", "side": "short", "kind": "trade", "fit": "strategy", "confidence": "low", "text": "t"}]
        out = s.score(rows, fills=[], now_ms=self.NOW, series_for=self.series)
        by = {r["coin"]: r for r in out}
        self.assertLess(by["X"]["moves"][1], 0)    # reduce a long: the price rose 10%, so the cut was wrong
        self.assertLess(by["Y"]["moves"][1], 0)    # avoid a long: the price rose, so avoiding it was wrong
        self.assertLess(by["Z"]["moves"][1], 0)    # short: the price rose, wrong
        self.assertAlmostEqual(by["X"]["moves"][1], -0.10, places=2)

    def test_avoid_is_followed_when_no_long_was_opened_and_reduce_by_a_closing_fill(self):
        at = "2026-11-20T00:00:00Z"
        start = s._ms(at)
        rows = [{"at": at, "coin": "Y", "side": "long", "kind": "avoid", "fit": "additional", "confidence": "low", "text": "a"},
                {"at": at, "coin": "X", "side": "long", "kind": "reduce", "fit": "strategy", "confidence": "low", "text": "r"}]
        opened = [{"coin": "Y", "side": "B", "dir": "Open Long", "time": start + 3600_000}]
        closed = [{"coin": "X", "side": "A", "dir": "Close Long", "time": start + 3600_000}]
        out = {r["coin"]: r for r in s.score(rows, fills=opened + closed, now_ms=self.NOW, series_for=self.series)}
        self.assertFalse(out["Y"]["followed"])     # a long was opened: the avoid was not followed
        self.assertTrue(out["X"]["followed"])
        unverified = s.score(rows, fills=[], now_ms=self.NOW, series_for=self.series)
        self.assertTrue(all(r["followed"] is None for r in unverified))
        none = {r["coin"]: r for r in s.score(rows, fills=[], now_ms=self.NOW, series_for=self.series,
                                              fill_coverage=(start, self.NOW))}
        self.assertTrue(none["Y"]["followed"])
        self.assertFalse(none["X"]["followed"])

    def test_chosen_label_comes_from_the_journal_entry_opened_near_the_fill(self):
        entries = [{"coin": "xyz:SHAZ", "side": "SHORT", "strategy": "directional", "opened": "2026-11-20T17:42:00"},
                   {"coin": "xyz:SHAZ", "side": "SHORT", "strategy": "fade", "opened": "2026-11-10T10:00:00"}]
        fill_ms = s._ms("2026-11-20T17:42:30Z")
        self.assertEqual(s.chosen_label("xyz:SHAZ", "short", fill_ms, entries), "directional")
        self.assertIsNone(s.chosen_label("xyz:QNT", "short", fill_ms, entries))

    def test_an_approximate_row_is_dropped_when_an_exact_one_exists(self):
        rows = [{"at": "2026-11-20T14:00:00Z", "coin": "X", "side": "short", "kind": "trade", "text": "x. Time approximate."},
                {"at": "2026-11-20T01:49:04Z", "coin": "X", "side": "short", "kind": "trade", "text": "x exact"}]
        self.assertEqual(len(s._without_approximate_duplicates(rows)), 1)
        self.assertEqual(len(s._without_approximate_duplicates(rows[:1])), 1)   # alone it stays

    def test_weekly_lines_are_cumulative_per_fit_and_comparison_has_three_groups(self):
        results = [{"at": "2026-11-16T10:00:00Z", "fit": "strategy", "kind": "trade", "followed": True, "moves": {3: 0.05, "now": 0.05}},
                   {"at": "2026-11-23T10:00:00Z", "fit": "strategy", "kind": "trade", "followed": False, "moves": {3: -0.02, "now": -0.02}},
                   {"at": "2026-11-17T10:00:00Z", "fit": "additional", "kind": "trade", "followed": False, "moves": {3: 0.01, "now": 0.01}}]
        weeks = s.weekly(results)
        self.assertEqual([n for _, n, _ in weeks["strategy"]], [1, 2])     # cumulative
        self.assertEqual(weeks["strategy"][1][2], 0.5)
        out = s.comparison(results, [{"coin": "Q", "side": "long", "at": "2026-11-18T00:00:00Z", "moves": {3: 0.02, "now": 0.02}}])
        self.assertEqual((out["suggested_followed"]["n"], out["suggested_not_followed"]["n"], out["own_not_suggested"]["n"]), (1, 2, 1))

    def test_own_entries_leave_out_suggested_coins_and_market_making_noise(self):
        start = s._ms("2026-11-18T00:00:00Z")
        fills = [{"coin": "A", "side": "A", "dir": "Open Short", "time": start}, {"coin": "B", "side": "B", "dir": "Open Long", "time": start}]
        fills += [{"coin": "ETH", "side": "B", "dir": "Open Long", "time": start + i * 60_000} for i in range(10)]   # a ladder: noisy
        suggested = [{"coin": "B", "side": "long"}]
        out = s.own_entries(fills, suggested, now_ms=self.NOW, series_for=self.series)
        self.assertEqual([e["coin"] for e in out], ["A"])                 # B was suggested, ETH is a ladder
        self.assertLess(out[0]["moves"][1], 0)                            # a short while the price rose 10%

    def test_truncated_prices_do_not_fill_later_horizons(self):
        at = "2026-11-20T00:00:00Z"
        start = s._ms(at)
        moves = s.moves_from_series([(start, 100), (start + 86_400_000, 110)], at, self.NOW)
        self.assertAlmostEqual(moves[1], 0.1)
        self.assertIsNone(moves[3])
        self.assertIsNone(moves["now"])

    def test_own_entries_keep_separate_opening_clusters(self):
        start = s._ms("2026-11-20T00:00:00Z")
        fill = {"coin": "BTC", "side": "B", "dir": "Open Long"}
        fills = [{**fill, "time": start + offset} for offset in (0, 1000, 172_800_000)]
        self.assertEqual(len(s.own_entries(fills, [], self.NOW, self.series)), 2)

    def test_the_hit_stat_carries_n_and_a_z(self):
        out = s.hit_stats([0.1, 0.2, -0.1, 0.3])
        self.assertEqual(out["n"], 4)
        self.assertEqual(out["hit"], 0.75)
        self.assertAlmostEqual(out["z"], 1.0)


class LogAggregateTests(unittest.TestCase):
    def test_counts_by_kind_trigger_month_and_scoreable(self):
        rows = [{"at": "2026-11-05T10:00:00Z", "kind": "trade", "coin": "X", "side": "short", "fit": "strategy", "trigger": "asked", "text": "a"},
                {"at": "2026-11-06T10:00:00Z", "kind": "design", "coin": None, "side": None, "fit": "process", "trigger": "unprompted", "text": "b"},
                {"at": "2026-12-01T10:00:00Z", "kind": "avoid", "coin": "Y", "side": "long", "fit": "additional", "trigger": "asked", "text": "c"}]
        out = s.counts(rows)
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["scoreable"], 2)
        self.assertEqual(out["by_kind"]["trade"], 1)
        self.assertEqual(out["by_trigger"], {"asked": 2, "unprompted": 1, "unknown": 0})
        self.assertEqual(out["by_month"], {"2026-11": 2, "2026-12": 1})

    def test_log_uses_shares_and_keeps_the_sample_count(self):
        rows = [{"at": "2026-11-20", "kind": kind, "trigger": trigger}
                for kind, trigger in (("trade", "asked"), ("trade", "unknown"), ("process", "unprompted"))]
        frame = s.log_frame({"rows": rows, "counts": s.counts(rows),
                             "results": [{"kind": "trade", "moves": {1: 0.0}}],
                             "followups": [{"kind": "process", "done": True}]})
        trade = frame.set_index("Kind").loc["trade"]
        self.assertEqual(trade["n"], 2)
        self.assertAlmostEqual(trade["Share of log"], 2 / 3)
        self.assertEqual((trade["Asked"], trade["Unprompted"], trade["Scored on price"]), (0.5, 0.0, 0.5))
        self.assertEqual(frame.set_index("Kind").loc["process", "Linked commit"], 1.0)

    def test_voided_rows_are_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            s.add("X", None, "The most you could short is 6,761 HEMI, about $31", kind="info", at="2026-11-05T10:00:00Z", path=path)
            s.add("Y", None, "kept", kind="info", at="2026-11-05T11:00:00Z", path=path)
            self.assertEqual([r["text"] for r in s.load(path)], ["kept"])
            self.assertEqual(len(s.load(path, include_voided=True)), 2)

    def test_the_verdict_reads_a_horizon_only_with_twenty_outcomes(self):
        few = [{"moves": {1: 0.01, 3: 0.01, 7: None, "now": 0.01}, "followed": True} for _ in range(19)]
        many = [{"moves": {1: 0.01, 3: 0.01, 7: None, "now": 0.01}, "followed": True} for _ in range(20)]
        self.assertIn("too few", s.summary(few)["verdict"])
        self.assertNotIn("too few", s.summary(many)["verdict"])


class FunnelTests(unittest.TestCase):
    def test_usage_requires_an_explicit_link_and_exact_post_deploy_events(self):
        from desk import suggestion_funnel as funnel

        suggestion = {"kind": "process", "text": "Trade confirmation", "at": "2026-11-20T12:00:00Z"}
        links = {funnel.suggestion_id(suggestion): {"component": "desk", "commit": "example", "target": "orders", "action": "open"}}
        deploy = {"component": "desk", "commit": "example", "at": "2026-11-20T15:00:00Z"}
        event = {"target": "orders", "action": "open"}
        events = [
            {**event, "received": "2026-11-20T14:59:00Z"},
            {**event, "received": "2026-11-20T15:01:00Z"},
            {**event, "received": "2026-11-20T15:02:00Z", "target": "orders archive"},
            {**event, "received": "2026-11-20T15:03:00Z", "action": "close"},
        ]
        unlinked, = funnel.funnel([suggestion], {}, [deploy], events)
        self.assertFalse(unlinked["shipped"])
        linked, = funnel.funnel([suggestion], links, [deploy], events)
        self.assertTrue(linked["shipped"])
        self.assertFalse(linked["used"])
        self.assertEqual(linked["usage"]["events_before"], 1)
        self.assertEqual(linked["usage"]["events_after"], 1)


if __name__ == "__main__":
    unittest.main()
