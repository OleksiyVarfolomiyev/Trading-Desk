"""The compact plan screen (desk/app_static/app.js, the COMPACT_PLAN_HELPERS block): one summary line, notice chips, size buttons."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")

ANIME = """Other desk accounts (read 13s ago) (included in equity and the aggregate risk cap):
Estimated current stop + 5-day adverse funding risk: $2,705.23 (26% of desk equity)
Aggregate stop-risk cap: $3,410.93 (33% of desk equity)
ANIME SHORT: quantity=822145, reference=$0.002073, size $1,704 = 44% of the wallet, risk $413 = 12% of the risk cap (risk budget and volatility), no stop or loss guard, take-profit $0.00157136 held by the daemon (sizing level $0.00257464 = 3 x ATR), funding reserve/unit=$0, estimated entry+exit fees=$1.02 — WARNING: leaves $292 of cap headroom, less than one normal trade's $413 risk budget; the desk cannot open another trade until this one shrinks — NOTE: large size ($1.7K): trades above $1.5K lost money in the desk's history [directional]"""

BLOCKED = """Estimated current stop + 5-day adverse funding risk: $3,701.61 (36% of desk equity)
Aggregate stop-risk cap: $3,352.35 (33% of desk equity)
xyz:QNT SHORT: quantity=0, reference=$28.244, size $0 = 0% of the wallet, risk $0 = 0% of the risk cap (risk budget and volatility), no stop, take-profit or loss guard: stocks are held passively — BLOCKED: combined open risk already meets or exceeds the 33% equity cap [directional]"""

FLEX = """Aggregate stop-risk cap: $3,352.35 (33% of desk equity)
SNDK LONG: quantity=14.8, size $1.2K = 31% of the wallet, risk $414 = 12% of the risk cap (risk budget and volatility), no stop — NOTE: weak side: directional longs lost money in the desk's history (profit factor 0.72; shorts 2.42) [directional]
SNDK: FLEX: normal size $400 (12% of cap), flex size $700 (21% of cap)"""


def helpers() -> str:
    return re.search(r"// COMPACT_PLAN_HELPERS_START\n(.*?)// COMPACT_PLAN_HELPERS_END", APP, re.S).group(1)


class PlanScreenTests(unittest.TestCase):
    def test_runaction_uses_the_summary_chips_and_size_buttons(self):
        body = re.search(r"async function runAction\(.*?\n}\n", APP, re.S).group(0)
        for needle in ("planSummary(plan.plan, coin)", "planChips(plan.plan, coin)", "plan-summary", "plan-chips", "plan-sizes", "baseNotional", "chosenLabel"):
            self.assertIn(needle, body)
        self.assertNotIn('class: "action-line notice"', body.split("box.replaceChildren(summaryLine")[1])   # no wall of lines

    def test_single_order_plan_docks_and_uses_direction_glyphs(self):
        body = re.search(r"async function runAction\(.*?\n}\n", APP, re.S).group(0)
        self.assertIn("docked: true", body)
        self.assertIn('long: "▲", short: "▼"', body)
        self.assertIn("of account", body)
        self.assertNotIn('"h3"', body)
        self.assertNotIn('text: "Cancel"', body)

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_the_pasted_anime_plan_reads_as_one_line_and_chips(self):
        script = helpers() + f"""
const anime = {json.dumps(ANIME)}, blocked = {json.dumps(BLOCKED)}, flex = {json.dumps(FLEX)};
console.log(JSON.stringify({{ anime: planSummary(anime, "ANIME"), animeChips: planChips(anime, "ANIME"),
  blocked: planSummary(blocked, "xyz:QNT"), blockedChips: planChips(blocked, "xyz:QNT"),
  flex: planSummary(flex, "SNDK"), flexChips: planChips(flex, "SNDK"), none: planSummary("nothing here", "ANIME") }}));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertTrue(out["anime"]["parsed"])
        self.assertEqual((out["anime"]["walletPct"], out["anime"]["capPct"], out["anime"]["sizeUsd"], out["anime"]["riskUsd"]), (44, 12, 1704, 413))
        self.assertEqual([c["text"] for c in out["animeChips"]], ["Leaves 9% of cap", "Large size"])        # $292 of the $3,410.93 cap, then the size note
        self.assertTrue(out["animeChips"][0]["title"].startswith("leaves $292 of cap headroom"))           # the sentence is the hover
        self.assertEqual(out["animeChips"][0]["kind"], "warn")
        self.assertEqual([c["text"] for c in out["blockedChips"]], ["Cap full"])
        self.assertEqual(out["blocked"]["sizeUsd"], 0)
        self.assertEqual(out["flex"]["sizeUsd"], 1200)                                                        # "$1.2K" reads as 1200
        self.assertEqual([c["text"] for c in out["flexChips"]], ["Weak side"])                               # the FLEX line is the flex buttons' job, not a chip
        self.assertFalse(out["none"]["parsed"])                                                              # unparsable text falls back to the raw line


if __name__ == "__main__":
    unittest.main()
