"""The build history: commit labels, the committed csv and the dashboard tiles (desk/build_history.py)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import build_history as bh  # noqa: E402


def row(day, kind, area, files="desk/a.py", subject="s", added=1, removed=0, confidence="high"):
    return {"hash": "h" + day + kind + area, "date_et": f"2026-11-{day} 10:00", "kind": kind, "area": area, "confidence": confidence,
            "subject": subject, "files": files, "added": added, "removed": removed}


class ClassifyTests(unittest.TestCase):
    def test_a_labelled_subject_keeps_its_label(self):
        self.assertEqual(bh.classify("x1", "fix(ui): stop a tile growing", ["desk/a.py"]), ("fix", "ui", "high"))

    def test_fix_words_make_a_fix_and_dashboard_files_make_ui(self):
        kind, area, _ = bh.classify("x2", "Fix chart crash on bar traces", ["desk/dashboard_static/app.js", "tests/test_x.py"])
        self.assertEqual((kind, area), ("fix", "ui"))

    def test_daemon_files_and_words(self):
        kind, area, _ = bh.classify("x3", "Take-profits held in daemon memory", ["execution/hl_daemon.py", "tests/test_hl_daemon.py"])
        self.assertEqual((kind, area), ("feat", "daemon"))

    def test_readme_only_is_docs_and_deploy_only_is_infra(self):
        self.assertEqual(bh.classify("x4", "Update notes", ["README.md"])[0], "docs")
        self.assertEqual(bh.classify("x5", "Add a systemd unit", ["deploy/systemd/a.service"])[0], "infra")

    def test_research_only_changes_are_research(self):
        self.assertEqual(bh.classify("x6", "Pump and dump replay", ["research/pump.py", "tests/test_pump.py"])[0], "research")

    def test_a_dashboard_factor_model_is_not_research(self):
        kind, _, _ = bh.classify("x7", "Dashboard options: factor models side by side", ["desk/dashboard_static/app.js", "desk/books.py"])
        self.assertEqual(kind, "feat")

    def test_manual_overrides_win_and_say_so(self):
        bh.OVERRIDES["zz"] = ("infra", "relay")
        try:
            self.assertEqual(bh.classify("zz", "Fix everything", ["desk/a.py"]), ("infra", "relay", "manual"))
        finally:
            del bh.OVERRIDES["zz"]

    def test_vague_subjects_are_low_confidence(self):
        self.assertEqual(bh.classify("x8", "small fixes", ["desk/overview.py"])[2], "low")

    def test_every_label_is_in_the_vocabulary(self):
        for commit in bh.rows_from(bh.read_git()[-40:]):
            self.assertIn(commit["kind"], bh.KINDS)
            self.assertIn(commit["area"], bh.AREAS)


class FileTests(unittest.TestCase):
    def test_committed_csv_has_a_row_for_each_label_and_valid_values(self):
        rows = bh.read_csv()
        self.assertGreater(len(rows), 400)
        self.assertEqual(len({r["hash"] for r in rows}), len(rows))
        for r in rows:
            self.assertIn(r["kind"], bh.KINDS)
            self.assertIn(r["area"], bh.AREAS)
            self.assertEqual(list(r)[:len(bh.COLUMNS)], bh.COLUMNS)

    def test_update_adds_only_commits_it_has_not_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "h.csv"
            first = bh.update(path)
            self.assertGreater(first, 400)
            self.assertEqual(bh.update(path), 0)


class SummaryTests(unittest.TestCase):
    rows = [row("01", "feat", "ui", "desk/a.py;desk/b.py", added=10, removed=2), row("01", "fix", "ui", "desk/a.py", added=1, removed=1),
            row("02", "fix", "ui", "desk/a.py", added=3), row("02", "research", "factor", "research/f.py", "Sector blocks not adopted"),
            row("03", "feat", "daemon", "execution/d.py", added=5)]

    def test_commits_per_day_by_kind(self):
        days = bh.per_day(self.rows)
        self.assertEqual(days["2026-11-01"], {"feat": 1, "fix": 1})
        self.assertEqual(days["2026-11-02"], {"fix": 1, "research": 1})

    def test_area_table_has_fix_share_and_lines_changed(self):
        ui = next(r for r in bh.by_area(self.rows) if r["Area"] == "ui")
        self.assertEqual(ui["Commits"], 3)
        self.assertAlmostEqual(ui["Fix share"], 2 / 3)
        self.assertEqual(ui["Lines changed"], 12 + 2 + 3)

    def test_hotspots_count_fixes_and_skip_data_files(self):
        top = bh.hotspots(self.rows)[0]
        self.assertEqual((top["File"], top["Commits"], top["Fixes"]), ("desk/a.py", 3, 2))
        self.assertEqual(top["Last fix"], "2026-11-02")

    def test_research_outcomes_flag_the_ones_not_adopted(self):
        self.assertEqual(bh.research_outcomes(self.rows)[0]["Outcome"], "not adopted")

    def test_a_research_commit_without_a_verdict_is_unstated(self):
        outcomes = {r["Research"]: r["Outcome"] for r in bh.research_outcomes(self.rows + [row("04", "research", "signals", "research/x.py", "A study")])}
        self.assertEqual(outcomes["A study"], "unstated")

    def test_publish_writes_four_tiles_without_pushing(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "h.csv"
            bh.write_csv(self.rows, csv_path)
            keys = bh.publish(csv_path, push=False, directory=Path(folder) / "tiles")
            self.assertEqual(len(keys), 4)
            self.assertEqual(len(list((Path(folder) / "tiles").glob("*.json"))), 4)


if __name__ == "__main__":
    unittest.main()
