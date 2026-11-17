import json
import tempfile
import unittest
from pathlib import Path

from desk.dashboard import save_feedback
from research import feedback_summary


class Feedback(unittest.TestCase):
    def test_events_are_saved_one_line_each_and_junk_is_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "feedback.jsonl"
            body = json.dumps({"events": [{"kind": "highlight", "action": "read", "target": "uPnL"},
                                          {"kind": "look", "action": "mode"}, {"nope": 1}, "text"]}).encode()
            self.assertEqual(save_feedback(body, path), 2)
            self.assertEqual(save_feedback(b"not json", path), 0)
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["target"], "uPnL")
            self.assertIn("received", json.loads(lines[0]))

    def test_summary_counts_answers_by_area(self):
        import pandas as pd

        frame = pd.DataFrame([{"kind": "highlight", "action": "read", "target": "uPnL", "at": pd.Timestamp("2026-11-16", tz="UTC")},
                              {"kind": "highlight", "action": "not important", "target": "uPnL", "at": pd.Timestamp("2026-11-16", tz="UTC")},
                              {"kind": "highlight", "action": "read", "target": "Funding", "at": pd.Timestamp("2026-11-16", tz="UTC")}])
        table = feedback_summary.summary(frame)["Highlights by area (what the reader answered)"]
        self.assertEqual(int(table.loc["uPnL", "total"]), 2)
        self.assertEqual(int(table.loc["Funding", "read"]), 1)


if __name__ == "__main__":
    unittest.main()
