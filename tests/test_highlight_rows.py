"""Highlight rows: one line each, merged, capped (desk/app_static/app.js, the HIGHLIGHT_SHAPING block)."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "desk/app_static/app.js").read_text()
CSS = (ROOT / "desk/app_static/app.css").read_text()
NODE = shutil.which("node")


def helpers() -> str:
    return re.search(r"// HIGHLIGHT_SHAPING_START\n(.*?)// HIGHLIGHT_SHAPING_END", APP, re.S).group(1)


class HighlightRowTests(unittest.TestCase):
    def test_rows_are_one_line_with_the_sentence_on_hover_and_empty_cards_say_nothing(self):
        self.assertRegex(CSS, r"\.highlights li \.text \{[^}]*white-space: nowrap")
        self.assertIn("text-overflow: ellipsis", CSS)
        self.assertNotIn('class: "empty", text: "Nothing to report"', APP)   # an empty card shows nothing (the printable report keeps its own line)
        self.assertIn("highlightPunctuation(String(whole))", APP)      # the whole sentence is the row's title

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_cap_merge_restatements_and_lists(self):
        script = helpers() + """
const row = (area, text, level = "") => [area, text, level, text];
// a real-like attention card: duplicates, a restatement, long rule lists, several levels
const items = [
  row("ETH bounce after a sell-off: take profits on shorts", "ETH 3% up in 55 min", "watch"),
  row("ETH bounce after a sell-off: take profits on shorts", "ETH 3.1% up in 60 min", "watch"),        // the same alert re-fired
  row("Build-up: TRUMP", "TRUMP short added $36 to $1,303"), row("Build-up: TRUMP", "TRUMP short added $27 to $1,297"),
  row("Tables disagree", "uPnL: strategies 0.68%, accounts -3.8%", "critical"),
  row("Tables disagree", "position weights: crypto and stocks 94%, expected 100%", "critical"),         // a different line: stays
  row("Risk", "Open risk 19%."),                                                                       // the key numbers bar says it
  row("Largest losers", "D crypto: NEAR -13%, SKY -13%"), row("Adverse funding", "ANIME short -63% APR"),
  row("Review: held > 7 days", "ETH (9d): close, or re-tag"), row("HMSTR", "pump and dump: peaked 22% up"),
  row("No historical test", "Regime guard; Margin guard; Entry sizing; Held take-profit"),
  row("No forward test", "Regime guard; Margin guard; Held take-profit"),
];
const capped = shapeHighlights(items, false), open = shapeHighlights(items, true);
const small = shapeHighlights([row("uPnL", "Crypto 6.2%"), row("Largest winners", "USELESS 20%")], false);
console.log(JSON.stringify({ capped: capped.shown.map((r) => [r[0], r[1], r[2]]), hidden: capped.hidden, total: capped.total, openTotal: open.shown.length, openHidden: open.hidden,
  noRisk: !open.shown.some((r) => r[0] === "Risk"), eth: open.shown.filter((r) => /ETH bounce/.test(r[0])).map((r) => r[1]),
  trump: open.shown.filter((r) => /TRUMP/.test(r[0])).map((r) => r[1]), tables: open.shown.filter((r) => r[0] === "Tables disagree").length,
  lists: open.shown.filter((r) => /^No (historical|forward) test$/.test(r[0])).map((r) => r[1]), smallHidden: small.hidden, smallN: small.shown.length }));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out["total"], 10)                                   # 13 rows: 3 merged or dropped
        self.assertEqual(len(out["capped"]), 5)                              # at most five rows a card
        self.assertEqual(out["hidden"], 5)                                   # the rest is one +5 chip
        self.assertEqual([r[2] for r in out["capped"]][:3], ["critical", "critical", "watch"])   # criticals first, then watches
        self.assertEqual((out["openTotal"], out["openHidden"]), (10, 0))     # expanded shows everything
        self.assertTrue(out["noRisk"])                                       # a restated number is dropped
        self.assertEqual(out["eth"], ["ETH 3.1% up in 60 min"])              # re-fired: one row, the latest figure
        self.assertEqual(out["trump"], ["TRUMP short added $27 to $1,297"])
        self.assertEqual(out["tables"], 2)                                   # different lines are both kept
        self.assertEqual(out["lists"], ["4 rules", "3 rules"])               # a rule list reads as its count
        self.assertEqual((out["smallHidden"], out["smallN"]), (0, 2))        # a short card has no chip


if __name__ == "__main__":
    unittest.main()
