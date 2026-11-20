"""A tile added back from the Sections menu must appear (desk/app_static/app.js showRequestedCard, cardHiddenWhenEmpty).

The stocks positions tile did not come back after being hidden. A tile hid itself whenever its table was empty at a refresh
(``card.hidden = empty``), whatever the reader asked for, and un-hiding only toggled a class: nothing re-drew the table or
checked the tile sat in a column. A tile the reader asked for now stays (an empty one reads "None open") and is drawn again.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def helpers() -> str:
    return re.search(r"// CARD_UNHIDE_START\n(.*?)// CARD_UNHIDE_END", APP, re.S).group(1)


class CardUnhideTests(unittest.TestCase):
    def test_the_menu_path_marks_attaches_and_redraws_the_tile(self):
        set_hidden = re.search(r"function setHidden\(id, hidden\) \{.*?\n}\n", APP, re.S).group(0)
        self.assertIn("showRequestedCard(card)", set_hidden)
        self.assertIn('delete card.dataset.requested', set_hidden)       # hiding it again withdraws the request
        show = re.search(r"function showRequestedCard\(card\) \{.*?\n}\n", APP, re.S).group(0)
        for part in ('dataset.requested = "1"', "card.hidden = false", "shortestColumn(", "renderTable(id, applyView(", 'classList.add("filled")'):
            self.assertIn(part, show)

    def test_a_requested_tile_is_not_hidden_for_having_no_rows(self):
        render = re.search(r"function renderTable\(id, spec\) \{.*?\n}\n", APP, re.S).group(0)
        self.assertIn("cardHiddenWhenEmpty(empty, requested)", render)
        self.assertNotIn("card.hidden = empty;", render)

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_the_helpers(self):
        script = helpers() + """
console.log(JSON.stringify({
  unrequestedEmpty: cardHiddenWhenEmpty(true, false), requestedEmpty: cardHiddenWhenEmpty(true, true),
  filled: cardHiddenWhenEmpty(false, false), filledRequested: cardHiddenWhenEmpty(false, true),
  shortest: shortestColumn([420, 130, 300]), tie: shortestColumn([100, 100, 100]), one: shortestColumn([55]), none: shortestColumn([]),
}));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertTrue(out["unrequestedEmpty"])        # nobody asked for it and it has nothing to show: it stays out of the way
        self.assertFalse(out["requestedEmpty"])         # asked for: it stays, with a quiet line
        self.assertFalse(out["filled"])
        self.assertFalse(out["filledRequested"])
        self.assertEqual(out["shortest"], 1)
        self.assertEqual(out["tie"], 0)
        self.assertEqual((out["one"], out["none"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
