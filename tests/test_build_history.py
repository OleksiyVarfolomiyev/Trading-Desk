"""The build history: purpose labels, the committed csv and the dashboard tiles (desk/build_history.py)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import build_history as bh  # noqa: E402


def row(day, effect, purpose, files="desk/a.py", subject="s", added=1, removed=0, confidence="high", why=""):
    return {"hash": "h" + day + effect + purpose, "date_et": f"2026-11-{day} 10:00", "effect": effect, "purpose": purpose,
            "confidence": confidence, "why": why, "subject": subject, "files": files, "added": added, "removed": removed}


class ClassifyTests(unittest.TestCase):
    def test_a_settled_label_wins(self):
        labels = {"x0": {"effect": "fix", "purpose": "risk", "confidence": "high", "why": "betas from the wrong dex"}}
        self.assertEqual(bh.classify("x0", "Colour tweak", ["desk/app_static/app.css"], labels),
                         ("fix", "risk", "high", "betas from the wrong dex"))

    def test_a_labelled_subject_keeps_its_label(self):
        self.assertEqual(bh.classify("x1", "improve(execution): maker-first reductions", ["execution/hl_trade.py"], {})[:3],
                         ("improve", "execution", "high"))

    def test_rules_read_purpose_not_tool(self):
        self.assertEqual(bh.classify("x2", "Margin guard on account-wide liquidation risk", ["execution/hl_daemon.py"], {})[1], "risk")
        self.assertEqual(bh.classify("x3", "Short candidates flag crowded funding", ["desk/screener.py"], {})[1], "strategy")
        self.assertEqual(bh.classify("x4", "Pump and dump replay net of costs", ["research/pump.py"], {})[1], "research")
        self.assertEqual(bh.classify("x5", "Batch execution with plan and confirm", ["desk/app_static/app.js"], {})[1], "execution")

    def test_presentation_changes_are_overhead(self):
        self.assertEqual(bh.classify("x6", "Softer candle colours", ["desk/app_static/app.css"], {})[1], "overhead")
        self.assertEqual(bh.classify("x7", "Update notes", ["README.md"], {})[1], "overhead")

    def test_effect_from_the_subject(self):
        self.assertEqual(bh.classify("x8", "Fix chart crash on bar traces", ["desk/charts.py"], {})[0], "fix")
        self.assertEqual(bh.classify("x9", "Add a stress tile", ["desk/stress.py"], {})[0], "new")
        self.assertEqual(bh.classify("xa", "Stress tile reads the room left", ["desk/stress.py"], {})[0], "improve")

    def test_rule_labels_are_never_high_confidence(self):
        self.assertIn(bh.classify("xb", "Something else entirely", ["desk/a.py"], {})[2], ("low", "medium"))


class FileTests(unittest.TestCase):
    def test_every_commit_up_to_the_relabel_is_settled_by_hand_with_a_reason(self):
        labels = bh.load_labels()
        self.assertGreaterEqual(len(labels), 515)
        for entry in labels.values():
            self.assertIn(entry["effect"], bh.EFFECTS)
            self.assertIn(entry["purpose"], bh.PURPOSES)
            self.assertIn(entry["confidence"], ("high", "medium", "low"))
            self.assertTrue(entry["why"].strip())

    def test_committed_csv_rows_are_valid_and_unique(self):
        rows = bh.read_csv()
        self.assertGreaterEqual(len(rows), 515)
        self.assertEqual(len({r["hash"] for r in rows}), len(rows))
        for r in rows:
            self.assertIn(r["effect"], bh.EFFECTS)
            self.assertIn(r["purpose"], bh.PURPOSES)
            self.assertEqual(list(r)[:len(bh.COLUMNS)], bh.COLUMNS)

    def test_update_adds_only_commits_it_has_not_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "h.csv"
            first = bh.update(path)
            self.assertGreater(first, 500)
            self.assertEqual(bh.update(path), 0)

    def test_an_old_kind_area_file_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "h.csv"
            path.write_text("hash,date_et,kind,area,confidence,subject,files,added,removed\nabc,2026-11-01 10:00,feat,ui,high,s,,1,0\n")
            self.assertGreater(bh.update(path), 500)
            self.assertIn("purpose", bh.read_csv(path)[0])


class SummaryTests(unittest.TestCase):
    rows = [row("01", "new", "risk", "desk/a.py;desk/b.py", added=10, removed=2), row("01", "fix", "risk", "desk/a.py", added=1, removed=1),
            row("02", "fix", "overhead", "desk/a.py", added=3), row("02", "new", "research", "research/f.py", "Sector blocks", why="not adopted"),
            row("03", "improve", "execution", "execution/d.py", added=5)]

    def test_commits_per_day_by_purpose(self):
        days = bh.per_day(self.rows)
        self.assertEqual(days["2026-11-01"], {"risk": 2})
        self.assertEqual(days["2026-11-02"], {"overhead": 1, "research": 1})

    def test_effort_by_purpose_shares_and_fix_share(self):
        table = {r["Purpose"]: r for r in bh.by_purpose(self.rows)}
        self.assertAlmostEqual(sum(r["Commits %"] for r in table.values()), 1.0)
        self.assertAlmostEqual(sum(r["Churn %"] for r in table.values()), 1.0)
        self.assertAlmostEqual(table["risk"]["Fix share"], 0.5)
        self.assertAlmostEqual(table["risk"]["Churn %"], 14 / 23)

    def test_effect_split(self):
        risk = next(r for r in bh.by_effect(self.rows) if r["Purpose"] == "risk")
        self.assertEqual((risk["New"], risk["Improve"], risk["Fix"]), (1, 0, 1))

    def test_hotspots_count_fixes_and_say_what_they_served(self):
        top = bh.hotspots(self.rows)[0]
        self.assertEqual((top["File"], top["Commits"], top["Fixes"], top["Mostly"]), ("desk/a.py", 3, 2, "risk"))
        self.assertEqual(top["Last fix"], "2026-11-02")

    def test_research_outcomes_read_the_reason_too(self):
        self.assertTrue(bh.research_outcomes(self.rows)[0]["Research"].startswith("Not adopted · "))
        studies = [r["Research"] for r in bh.research_outcomes(self.rows + [row("04", "new", "research", "research/x.py", "A study")])]
        self.assertIn("Unstated · A study", studies)

    def test_publish_writes_the_history_tiles_and_the_risk_reward_pair_without_pushing(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "h.csv"
            bh.write_csv(self.rows, csv_path)
            tiles = Path(folder) / "tiles"
            tiles.mkdir()
            (tiles / "build-history-areas.json").write_text("{}")   # the old tile goes
            keys = bh.publish(csv_path, push=False, directory=tiles)
            self.assertEqual(len([k for k in keys if k.startswith("build-history")]), 5)
            self.assertEqual(keys[5:], ["build-risk-reward-table", "build-risk-reward-chart"])
            self.assertFalse((tiles / "build-history-areas.json").exists())
            self.assertEqual(len(list(tiles.glob("*.json"))), 7)
            import json
            effects = json.loads((tiles / "build-history-effects.json").read_text())["data"]
            self.assertEqual([t["name"] for t in effects], ["New", "Improve", "Fix"])
            for i in range(len(effects[0]["x"])):   # every purpose's bar is 100%, an absent effect is no segment
                shares = [t["y"][i] for t in effects]
                self.assertNotIn(0, shares)
                self.assertAlmostEqual(sum(v for v in shares if v), 1.0)
            purposes = json.loads((tiles / "build-history-purposes.json").read_text())["data"]
            self.assertEqual(purposes[0]["x"][0] in bh.PURPOSES, True)
            days = json.loads((tiles / "build-history-days.json").read_text())["data"]
            self.assertTrue(all(trace["type"] == "scatter" for trace in days))

if __name__ == "__main__":
    unittest.main()
