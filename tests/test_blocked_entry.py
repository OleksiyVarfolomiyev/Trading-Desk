"""The blocked-entry screen: one line and option buttons (desk/app_static/app.js blockedInfo, blockedScreen)."""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP = Path(__file__).resolve().parent.parent / "desk" / "app_static" / "app.js"
SERVER = Path(__file__).resolve().parent.parent / "web" / "server.mjs"
NODE = shutil.which("node")

PLAN = """Desk equity (all venues): $10,158.64
Estimated current stop + 5-day adverse funding risk: $3,701.61 (36% of desk equity)
Aggregate stop-risk cap: $3,352.35 (33% of desk equity)
Per-position risk budget: $406.35 (5% of the 80% usable-equity amount)
xyz:QNT SHORT: quantity=0 - BLOCKED: combined open risk already meets or exceeds the 33% equity cap [directional]
"""


def helper() -> str:
    text = APP.read_text()
    start = text.index("function blockedInfo(planText) {")
    end = text.index("function roomCandidates(")
    return "const isNum = (v) => typeof v === 'number' && Number.isFinite(v);\n" + text[start:end]


class BlockedEntryTests(unittest.TestCase):
    def test_the_failure_branch_uses_the_options_screen_and_the_relay_forwards_flex(self):
        app = APP.read_text()
        self.assertIn("blockedScreen(plan, coin, title, overlay, box, retry)", app)
        self.assertIn("planFlex: true", app)
        self.assertIn("use_flex: true", app)
        self.assertNotIn("...lines.map((line) => el(\"p\", { class: \"action-line\", text: line })), closeButton(overlay));", app)
        server = SERVER.read_text()
        self.assertRegex(server, r"live: false, \.\.\.\(body\.use_flex === true")   # a flexed plan reaches the daemon at the plan stage

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_cap_numbers_and_flex_room_are_read_from_the_planner_text(self):
        script = helper() + f"""
const info = blockedInfo({json.dumps(PLAN)});
const kill = blockedInfo("Kill switch is on: no entries.");
console.log(JSON.stringify({{ info, kill: kill.reason }}));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        info = out["info"]
        self.assertEqual(info["reason"], "cap")
        self.assertAlmostEqual(info["current"], 3701.61)
        self.assertAlmostEqual(info["cap"], 3352.35)
        self.assertAlmostEqual(info["budget"], 406.35)
        self.assertEqual((info["currentPct"], info["capPct"]), (36, 33))
        self.assertAlmostEqual(info["flexRoom"], 3352.35 * 36 / 33 - 3701.61, places=2)   # the flex adds 3 points of desk equity
        self.assertEqual(out["kill"], "kill")


if __name__ == "__main__":
    unittest.main()
