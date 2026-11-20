"""A published chart's ALL chip is the chosen era (desk/app_static/app.js publishedPeriodDays).

With the top-bar switch on "actively managed" (since 2026-10-16) the ALL chip on a published time chart showed the whole
history from Nov '25: it ignored the era. D, W and M are cut to the era and ALL is the era itself; since inception ALL is
the whole history (0).
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def period_function() -> str:
    return re.search(r"// PUBLISHED_PERIOD_START\n(.*?)// PUBLISHED_PERIOD_END", APP, re.S).group(1)


class PublishedPeriodTests(unittest.TestCase):
    def test_every_use_goes_through_the_era_helper(self):
        self.assertIn("publishedDays(period)", APP)
        chips = re.search(r"function addPeriodChips\(.*?\n}\n", APP, re.S).group(0)
        self.assertIn("applyPublishedPeriod(node, label)", chips)
        self.assertNotIn("rebasePublished(node, days)", chips)   # the raw chip days are not used any more
        self.assertIn("reapplyPublishedPeriods();", re.search(r"function setEra\(.*?\n}\n", APP, re.S).group(0))

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_all_is_the_era_in_the_managed_view_and_the_whole_history_otherwise(self):
        script = period_function() + """
const out = {};
for (const period of ["D", "W", "M", "ALL"]) out[period] = [publishedPeriodDays(period, 35), publishedPeriodDays(period, null), publishedPeriodDays(period, 5)];
console.log(JSON.stringify(out));
"""
        result = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(result["ALL"], [35, 0, 5])   # managed: the era; inception: everything (0); a short era caps it
        self.assertEqual(result["D"], [1, 1, 1])
        self.assertEqual(result["W"], [7, 7, 5])
        self.assertEqual(result["M"], [30, 30, 5])


if __name__ == "__main__":
    unittest.main()
