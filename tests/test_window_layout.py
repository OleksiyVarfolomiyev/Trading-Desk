"""A day or week view of a chart runs over exactly its window (desk/app_static/app.js windowedLayout).

On the "Desk vs benchmarks" chart the D view showed since October: the thin "managed" line is a shape anchored to the
x axis at the managed start, the hourly view copied it, and Plotly's autorange stretched the axis to include it.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def window_function() -> str:
    return re.search(r"// WINDOWED_LAYOUT_START\n(.*?)// WINDOWED_LAYOUT_END", APP, re.S).group(1)


class WindowedLayoutTests(unittest.TestCase):
    def test_the_hourly_views_use_it(self):
        for name in ("drawIntraday", "drawIntradayValue"):
            body = re.search(r"function %s\(.*?\n}\n" % name, APP, re.S).group(0)
            self.assertIn("windowedLayout(node.layout", body, name)
            self.assertNotIn("autorange: true, range: null }),\n    yaxis", body, name)   # no x autorange left behind

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_a_marker_outside_the_window_is_dropped_and_the_range_is_explicit(self):
        script = window_function() + """
const day = 86400000, now = Date.parse("2026-11-20T12:00:00Z");
const layout = { xaxis: { autorange: true, range: null, type: "date" }, shapes: [
  { type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0 },
  { type: "line", xref: "x", yref: "paper", x0: "2026-10-16", x1: "2026-10-16", y0: 0, y1: 1 },
  { type: "line", xref: "x", yref: "paper", x0: "2026-11-20T06:00:00Z", x1: "2026-11-20T06:00:00Z", y0: 0, y1: 1 }] };
const out = windowedLayout(layout, now - day, now);
const result = { shapes: out.shapes.length, kept: out.shapes.map((s) => s.xref + ":" + s.x0), autorange: out.xaxis.autorange,
  range: out.xaxis.range, type: out.xaxis.type };
console.log(JSON.stringify(result));
"""
        out = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout
        import json

        result = json.loads(out)
        self.assertEqual(result["shapes"], 2)                       # the zero line and the in-window marker; October is gone
        self.assertNotIn("x:2026-10-16", result["kept"])
        self.assertFalse(result["autorange"])
        self.assertEqual(result["range"], ["2026-11-19T12:00:00.000Z", "2026-11-20T12:00:00.000Z"])
        self.assertEqual(result["type"], "date")

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_intraday_book_estimates_start_at_zero_and_use_signed_exposure(self):
        core = re.search(r"// INTRADAY_BOOK_START\n(.*?)// INTRADAY_BOOK_END", APP, re.S).group(1)
        script = core + """
const assert = require("node:assert/strict");
const hour = 3600000;
const stocks = [[0, 100], [hour, 105], [2 * hour, 110]];
const crypto = [[0, 10], [hour, 8], [2 * hour, 5]];
const quotes = new Map([["hl:BTC", crypto], ["ib:TEST", stocks]]);
const positions = [["hl", "BTC", "crypto", -100], ["ib", "TEST", "stocks", 110], ["ib", "MISSING", "stocks", 110]];
const [cryptoBook, stocksBook] = intradayBookMoves(positions, quotes, 0, 2 * hour, 1000);
assert.equal(cryptoBook.points[0][1], 0);
assert.equal(cryptoBook.points[2][1], 10);
assert.equal(stocksBook.points[2][1], 1);
assert.equal(stocksBook.coverage, 0.5);
assert.deepEqual(intradayBookMoves(positions, new Map(), 0, 2 * hour, 1000), []);
const liveAt = 2.5 * hour;
const liveQuotes = new Map([["hl:BTC", [...crypto, [liveAt, 4]]]]);
const [liveBook] = intradayBookMoves([["hl", "BTC", "crypto", -100, "perp", 5]], liveQuotes, 0, liveAt, 1000, true);
assert.deepEqual(liveBook.points.at(-1), [liveAt, 12]);
"""
        subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True)

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_long_windows_keep_the_latest_endpoint_for_each_line(self):
        core = re.search(r"// RETURN_WINDOW_START\n(.*?)// RETURN_WINDOW_END", APP, re.S).group(1)
        script = core + """
const assert = require("node:assert/strict");
const now = Date.parse("2026-11-20T20:30:00Z");
const days = ["2026-11-18", "2026-11-19"];
const make = (name) => ({name, x: days, levels: [1, 1.05], y: [0, 5]});
const rows = returnSeriesWithLatest([make("Desk"), make("crypto"), make("stocks"), make("BTC")], now,
  {crypto: {at: now, value: 2, estimate: true, note: "price moves only"}, BTC: {at: now, value: 1.08}});
const output = returnWindow(rows, 7, now).rows;
assert.equal(output[0].x.at(-1), new Date(now).toISOString());
assert.equal(output[1].estimateAt, new Date(now).toISOString());
assert.equal(output[1].y.at(-1), 7.1);
assert.equal(output[2].x.at(-1), days.at(-1));
assert.equal(output[3].y.at(-1), 8);
assert.equal(rows[0].y.at(-1), 5);
"""
        subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
