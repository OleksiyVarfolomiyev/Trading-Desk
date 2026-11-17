import unittest

from desk import usage_model


def event(kind, action, target):
    return {"kind": kind, "action": action, "target": target}


class Fit(unittest.TestCase):
    def test_an_area_dismissed_repeatedly_and_never_read_goes_quiet(self):
        events = [event("highlight", "not important", "Funding")] * 3 + [event("highlight", "read", "uPnL")] * 4
        model = usage_model.fit(events)
        self.assertTrue(model["areas"]["Funding"]["quiet"])
        self.assertFalse(model["areas"]["uPnL"]["quiet"])
        self.assertEqual(model["quiet"], ["Funding"])

    def test_one_dismissal_moves_nothing(self):
        model = usage_model.fit([event("highlight", "not important", "Funding")])
        self.assertFalse(model["areas"]["Funding"]["quiet"])

    def test_a_read_area_is_not_quiet_even_when_also_dismissed(self):
        events = [event("highlight", "not important", "Risk")] * 3 + [event("highlight", "read", "Risk")] * 6
        self.assertFalse(usage_model.fit(events)["areas"]["Risk"]["quiet"])   # keep 7/11 = 64%

    def test_load_dedupes_events_sent_to_both_files(self):
        import json
        import tempfile
        from pathlib import Path

        line = json.dumps({"at": "t", "kind": "highlight", "action": "read", "target": "x"}) + "\n"
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder, "a.jsonl"), Path(folder, "b.jsonl")
            a.write_text(line); b.write_text(line)
            self.assertEqual(len(usage_model.load_events([a, b])), 1)


class Thumbs(unittest.TestCase):
    def test_a_thumbs_up_outweighs_a_dismissal(self):
        events = [event("highlight", "not important", "Funding")] * 3 + [event("highlight", "useful", "Funding")] * 2
        self.assertFalse(usage_model.fit(events)["areas"]["Funding"]["quiet"])   # keep (4+1)/(4+3+2) = 56%


if __name__ == "__main__":
    unittest.main()
