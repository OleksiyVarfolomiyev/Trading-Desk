"""An entry's strategy label is chosen on the plan screen and required at the confirm step, not before planning.

The browser and the relay are JavaScript, so these checks read the sources for the guarantees that matter: the old label
screen is gone, the plan is keyed without the label, the relay refuses a confirm without one (leaving the token valid), and
the confirm button waits for the label.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "desk" / "app_static" / "app.js").read_text()
SERVER = (ROOT / "web" / "server.mjs").read_text()


class EntryLabelFlowTests(unittest.TestCase):
    def test_the_label_only_screen_is_gone(self):
        self.assertNotIn("function pickEntry", APP)
        self.assertNotIn("picked: true", APP)
        self.assertNotIn("target.picked", APP)

    def test_the_plan_screen_has_label_chips_and_confirm_waits_for_one(self):
        run = APP[APP.index("async function runAction"):]
        run = run[:run.index("\nasync function ") if "\nasync function " in run[10:] else None]
        self.assertIn('"aria-label": "Strategy label"', run)
        self.assertIn("(!isEntry || Boolean(chosenLabel))", run)   # confirm is disabled until a label is chosen
        self.assertIn('confirm.classList.toggle("is-waiting", !ready)', run)   # waiting, not disabled: a click marks what is missing
        self.assertIn('confirm.classList.contains("is-waiting")', run)
        self.assertIn("never a silent click", run)                # a failing click shows a notice

    def test_the_relay_plans_without_a_label_and_refuses_a_confirm_without_one(self):
        self.assertNotIn("before planning.", SERVER)
        self.assertIn("{ ...request_, label: undefined }", SERVER)   # the plan is keyed without the label
        confirm = SERVER[SERVER.index('    if (stage === "confirm") {'):]
        first = confirm.splitlines()[1]
        self.assertTrue(re.search(r"entry && !label", first), first)  # checked before the token is consumed
        self.assertLess(confirm.index("entry && !label"), confirm.index("pendingActions.delete"))



class RelayStopTests(unittest.TestCase):
    def test_a_stop_waits_for_the_running_action_and_a_page_only_deploy_does_not_restart_the_relay(self):
        self.assertIn('process.on("SIGTERM"', SERVER)
        stop = SERVER[SERVER.index('process.on("SIGTERM"'):]
        self.assertIn("while (actionRunning", stop)
        pull = (ROOT / "deploy" / "pull.sh").read_text()
        self.assertIn("git diff --quiet \"$before\" HEAD -- web/server.mjs", pull)   # the relay restarts only when its own code changed

if __name__ == "__main__":
    unittest.main()
