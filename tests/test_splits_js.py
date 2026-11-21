"""The web build's mirror of desk/splits.py (web/live.js, the SPLITS_HELPERS block) and the chart's (app.js adjustCandleSplits)."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = (ROOT / "web" / "live.js").read_text()
APP = (ROOT / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def run(script: str) -> dict:
    return json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)


class SplitMirrorTests(unittest.TestCase):
    def test_live_js_is_wired_into_prices_candles_and_payload(self):
        self.assertIn("splitSnap(context.mark / context.prevDay)", LIVE)      # the 24h reference price
        self.assertIn("splitAdjust(candleOf[coin], context.mark)", LIVE)      # ATR, moves and returns
        self.assertIn("splits: splitNotes", LIVE)                             # the tile's quiet marker
        self.assertIn("adjustCandleSplits(data)", APP)                        # the chart has no cliff
        self.assertIn("latest.data.splits", APP)                              # 'split 1:10' on the row's hover

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_sndk_candles_and_previous_price_are_put_on_one_scale(self):
        block = re.search(r"// SPLITS_HELPERS_START\n(.*?)// SPLITS_HELPERS_END", LIVE, re.S).group(1)
        script = block + """
const rows = [900, 950, 848, 879, 815, 838, 783, 754].map((c, i) => ({ t: i, h: c * 1.03, l: c * 0.97, c }));
const out = splitAdjust(rows, 85.1);
const crash = splitAdjust([100, 101, 99, 55].map((c, i) => ({ t: i, h: c, l: c, c })), 54);
const halved = splitSnap(0.5), reverse = splitSnap(10.2), none = splitSnap(0.7);
console.log(JSON.stringify({ closes: out.rows.map((r) => Math.round(r.c * 100) / 100), factor: out.factor, text: splitText(out.factor),
  prev: 812.78 * splitSnap(85.102 / 812.78), crash: crash.factor, halved, reverse, none }));
"""
        out = run(script)
        self.assertEqual(out["closes"][0], 90.0)
        self.assertEqual(out["closes"][-1], 75.4)
        self.assertEqual(out["factor"], 0.1)
        self.assertEqual(out["text"], "1:10")
        self.assertAlmostEqual(85.102 / out["prev"] - 1, 0.047, places=3)
        self.assertIsNone(out["crash"])
        self.assertEqual((out["halved"], out["reverse"], out["none"]), (0.5, 10, None))

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_chart_candles_have_no_cliff_after_a_split(self):
        match = re.search(r"function splitSnapChart\(.*?\n}\nfunction adjustCandleSplits\(.*?\n}\n", APP, re.S).group(0)
        script = match + """
const rows = [800, 810, 805, 80.5, 81].map((c, i) => ({ time: i, open: c, high: c, low: c, close: c, volume: 10 }));
const out = adjustCandleSplits(rows);
console.log(JSON.stringify({ closes: out.rows.map((r) => r.close), volumes: out.rows.map((r) => r.volume), factor: out.factor }));
"""
        out = run(script)
        self.assertEqual(out["closes"], [80, 81, 80.5, 80.5, 81])
        self.assertEqual(out["volumes"], [100, 100, 100, 10, 10])
        self.assertEqual(out["factor"], 0.1)

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_hourly_stock_book_uses_split_adjusted_marks(self):
        split = re.search(r"function splitSnapChart\(.*?function adjustHourlySplitMarks\(.*?\n}\n", APP, re.S).group(0)
        book = re.search(r"// INTRADAY_BOOK_START\n(.*?)// INTRADAY_BOOK_END", APP, re.S).group(1)
        out = run(split + book + """
const h = 3600000;
const marks = adjustHourlySplitMarks([[0, 753.69], [h, 91.95], [2*h, 92.261]]);
const positions = [["hl", "xyz:SNDK", "stocks", 300, "tradfi perp", 92.261]];
const [line] = intradayBookMoves(positions, new Map([["hl:xyz:SNDK", marks]]), 0, 2*h, 1000);
console.log(JSON.stringify({marks, first: line.points[0][1], last: line.points.at(-1)[1]}));
""")
        self.assertAlmostEqual(out["marks"][0][1], 753.69 / 8)
        self.assertEqual(out["first"], 0)
        self.assertAlmostEqual(out["last"], 30 * (92.261 - 753.69 / 8) / 92.261)


if __name__ == "__main__":
    unittest.main()
